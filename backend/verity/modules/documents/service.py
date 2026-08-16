"""Resume ingestion and graph projection (PRD §10.5, §12.2).

The pipeline is: validate → extract text → score quality → parse → project into
pending-review graph nodes. Nothing is written as approved; the user reviews
(FR-GRAPH-001).

The subtle requirement is FR-RES-006: re-uploading a resume must never silently
overwrite an edit the user made. ``diff_against_graph`` computes what changed
and marks entries the user has already corrected, so the review UI can present
a merge rather than a replacement.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from verity.modules.candidate_graph.models import (
    Achievement,
    CandidateProfile,
    Education,
    Experience,
    NodeStatus,
    ProvenanceSource,
    Skill,
)
from verity.modules.documents.extraction.resume_parser import ParsedResume, parse_resume
from verity.modules.documents.extraction.text import (
    QUALITY_THRESHOLD,
    extract_text,
    validate_upload,
)
from verity.modules.documents.models import Resume, ResumeStatus, ResumeVersion, SourceType
from verity.platform.errors import AppError, ErrorCode, RecoveryAction
from verity.platform.logging import get_logger
from verity.platform.storage import StorageBucket, storage

log = get_logger("documents.service")

PARSER_VERSION = "rule-parser@1"


@dataclass(slots=True)
class DiffEntry:
    entity_type: str
    change: str  # added | changed | unchanged | removed
    label: str
    existing_id: uuid.UUID | None = None
    user_corrected: bool = False
    fields_changed: list[str] = field(default_factory=list)


@dataclass(slots=True)
class IngestionResult:
    version: ResumeVersion
    parsed: ParsedResume
    diff: list[DiffEntry]
    created_node_ids: list[uuid.UUID] = field(default_factory=list)


class ResumeIngestionService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ── Upload ───────────────────────────────────────────────────────

    async def ingest_upload(
        self,
        *,
        user_id: uuid.UUID,
        data: bytes,
        filename: str | None,
        content_type: str | None = None,
        label: str | None = None,
        resume_id: uuid.UUID | None = None,
    ) -> IngestionResult:
        fmt = validate_upload(data, declared_content_type=content_type)

        stored = await storage.put(
            StorageBucket.RESUMES,
            data,
            content_type=content_type or "application/octet-stream",
            owner_id=user_id,
        )

        resume = await self._resolve_resume(user_id, resume_id, label or filename or "Resume")
        version = await self._new_version(
            user_id=user_id,
            resume=resume,
            source_type=SourceType.UPLOAD,
            storage_key=stored.key,
            filename=filename,
            byte_size=len(data),
            checksum=hashlib.sha256(data).digest(),
            mime_type=content_type,
        )

        extracted = extract_text(data, fmt)
        version.raw_text = extracted.text
        version.page_count = extracted.page_count
        version.extraction_quality = extracted.quality
        version.extraction_warnings = list(extracted.warnings)

        if not extracted.is_usable:
            # A dead end here would strand the user, so the failure carries the
            # alternative path rather than just an error (PRD §34.2).
            version.status = ResumeStatus.FAILED_UNREADABLE
            version.failure_code = "low_extraction_quality"
            await self._session.flush()
            raise AppError(
                ErrorCode.EXTRACTION_FAILED,
                "We couldn't read enough text from that file. "
                "It may be a scan or an image-only PDF.",
                detail={
                    "quality": extracted.quality,
                    "threshold": QUALITY_THRESHOLD,
                    "warnings": extracted.warnings,
                    "resume_version_id": str(version.id),
                },
                recovery_action=RecoveryAction(
                    type="edit_input", target="/profile/import", label="Paste your resume text"
                ),
            )

        return await self._parse_and_project(user_id=user_id, version=version)

    async def ingest_text(
        self,
        *,
        user_id: uuid.UUID,
        raw_text: str,
        label: str | None = None,
        resume_id: uuid.UUID | None = None,
    ) -> IngestionResult:
        """Pasted-text path. The fallback whenever a file cannot be read."""
        if len(raw_text.strip()) < 100:
            raise AppError.validation(
                "That's too short to parse. Paste the full resume text.",
                fields={"raw_text": "at least 100 characters"},
            )

        resume = await self._resolve_resume(user_id, resume_id, label or "Pasted resume")
        version = await self._new_version(
            user_id=user_id, resume=resume, source_type=SourceType.PASTE
        )
        version.raw_text = raw_text
        version.page_count = max(1, len(raw_text) // 3000)
        version.extraction_quality = 1.0  # No extraction step to be uncertain about.
        return await self._parse_and_project(user_id=user_id, version=version)

    # ── Parsing and projection ───────────────────────────────────────

    async def _parse_and_project(
        self, *, user_id: uuid.UUID, version: ResumeVersion
    ) -> IngestionResult:
        version.status = ResumeStatus.PARSING
        parsed = parse_resume(version.raw_text or "")

        version.ai_extracted = _serialize(parsed)
        version.parsed_at = dt.datetime.now(dt.UTC)

        diff = await self.diff_against_graph(user_id, parsed)
        created = await self._project_to_graph(user_id, version, parsed, diff)

        version.status = ResumeStatus.NEEDS_REVIEW
        await self._session.flush()

        log.info(
            "resume_ingested",
            resume_version_id=str(version.id),
            experiences=len(parsed.experiences),
            created_nodes=len(created),
        )
        return IngestionResult(version=version, parsed=parsed, diff=diff, created_node_ids=created)

    async def diff_against_graph(self, user_id: uuid.UUID, parsed: ParsedResume) -> list[DiffEntry]:
        """Compare a parse against what the user already has.

        Matching is on (company, title) rather than free text: those are the
        fields a person recognises as "the same job", and they survive rewording
        of the surrounding bullets.
        """
        existing = list(
            (
                await self._session.execute(
                    select(Experience).where(
                        Experience.user_id == user_id, Experience.deleted_at.is_(None)
                    )
                )
            ).scalars()
        )
        by_key = {(e.company_name.strip().lower(), e.title.strip().lower()): e for e in existing}

        entries: list[DiffEntry] = []
        seen_keys: set[tuple[str, str]] = set()

        for exp in parsed.experiences:
            key = (exp.company_name.strip().lower(), exp.title.strip().lower())
            seen_keys.add(key)
            match = by_key.get(key)
            label = f"{exp.title} at {exp.company_name}"

            if match is None:
                entries.append(DiffEntry(entity_type="experience", change="added", label=label))
                continue

            changed_fields = [
                name
                for name, incoming, current in (
                    ("start_month", exp.start_month, match.start_month),
                    ("end_month", exp.end_month, match.end_month),
                    ("is_current", exp.is_current, match.is_current),
                )
                if incoming is not None and incoming != current
            ]
            if set(exp.bullets) != set(match.bullets or []):
                changed_fields.append("bullets")

            entries.append(
                DiffEntry(
                    entity_type="experience",
                    change="changed" if changed_fields else "unchanged",
                    label=label,
                    existing_id=match.id,
                    # The flag the review UI needs to avoid clobbering an edit.
                    user_corrected=bool(match.user_corrected),
                    fields_changed=changed_fields,
                )
            )

        for key, row in by_key.items():
            if key not in seen_keys:
                entries.append(
                    DiffEntry(
                        entity_type="experience",
                        change="removed",
                        label=f"{row.title} at {row.company_name}",
                        existing_id=row.id,
                        user_corrected=bool(row.user_corrected),
                    )
                )
        return entries

    async def _project_to_graph(
        self,
        user_id: uuid.UUID,
        version: ResumeVersion,
        parsed: ParsedResume,
        diff: list[DiffEntry],
    ) -> list[uuid.UUID]:
        """Create pending-review nodes for genuinely new entities only.

        Existing nodes are left untouched: the review UI drives merges
        explicitly so a re-upload can never overwrite a correction
        (FR-RES-006, PP6).
        """
        created: list[uuid.UUID] = []
        added_labels = {d.label for d in diff if d.change == "added"}

        await self._ensure_profile(user_id, parsed)

        for exp in parsed.experiences:
            if f"{exp.title} at {exp.company_name}" not in added_labels:
                continue

            experience = Experience(
                user_id=user_id,
                company_name=exp.company_name,
                title=exp.title,
                start_month=exp.start_month,
                end_month=exp.end_month,
                is_current=exp.is_current,
                date_confidence=exp.date_confidence,
                location=exp.location,
                bullets=list(exp.bullets),
                status=NodeStatus.PENDING_REVIEW,
                source=ProvenanceSource.RESUME,
                source_ref=str(version.id),
                extracted_by=PARSER_VERSION,
                confidence=exp.confidence,
            )
            self._session.add(experience)
            await self._session.flush()
            created.append(experience.id)

            for achievement in exp.achievements:
                row = Achievement(
                    user_id=user_id,
                    experience_id=experience.id,
                    statement=achievement.statement,
                    metrics=[
                        {"text": m.text, "source_span": {"start": m.start, "end": m.end}}
                        for m in achievement.metrics
                    ],
                    has_quantified_metric=achievement.has_quantified_metric,
                    status=NodeStatus.PENDING_REVIEW,
                    source=ProvenanceSource.RESUME,
                    source_ref=str(version.id),
                    extracted_by=PARSER_VERSION,
                )
                self._session.add(row)
                await self._session.flush()
                created.append(row.id)

        created.extend(await self._project_skills(user_id, version, parsed))
        created.extend(await self._project_education(user_id, version, parsed))
        return created

    async def _project_skills(
        self, user_id: uuid.UUID, version: ResumeVersion, parsed: ParsedResume
    ) -> list[uuid.UUID]:
        if not parsed.skills:
            return []
        existing = {
            name.lower()
            for name in (
                await self._session.execute(
                    select(Skill.canonical_name).where(Skill.user_id == user_id)
                )
            ).scalars()
        }
        created: list[uuid.UUID] = []
        for skill_name in parsed.skills:
            if skill_name.lower() in existing:
                continue
            existing.add(skill_name.lower())
            skill = Skill(
                user_id=user_id,
                canonical_name=skill_name,
                raw_name=skill_name,
                status=NodeStatus.PENDING_REVIEW,
                source=ProvenanceSource.RESUME,
                source_ref=str(version.id),
                extracted_by=PARSER_VERSION,
            )
            self._session.add(skill)
            await self._session.flush()
            created.append(skill.id)
        return created

    async def _project_education(
        self, user_id: uuid.UUID, version: ResumeVersion, parsed: ParsedResume
    ) -> list[uuid.UUID]:
        existing = {
            name.lower()
            for name in (
                await self._session.execute(
                    select(Education.institution).where(
                        Education.user_id == user_id, Education.deleted_at.is_(None)
                    )
                )
            ).scalars()
        }
        created: list[uuid.UUID] = []
        for entry in parsed.education:
            if entry.institution.lower() in existing:
                continue
            existing.add(entry.institution.lower())
            row = Education(
                user_id=user_id,
                institution=entry.institution,
                degree=entry.degree,
                field=entry.field_of_study,
                start_year=entry.start_year,
                end_year=entry.end_year,
                status=NodeStatus.PENDING_REVIEW,
                source=ProvenanceSource.RESUME,
                source_ref=str(version.id),
                extracted_by=PARSER_VERSION,
            )
            self._session.add(row)
            await self._session.flush()
            created.append(row.id)
        return created

    async def _ensure_profile(self, user_id: uuid.UUID, parsed: ParsedResume) -> CandidateProfile:
        profile = (
            await self._session.execute(
                select(CandidateProfile).where(CandidateProfile.user_id == user_id)
            )
        ).scalar_one_or_none()

        if profile is None:
            profile = CandidateProfile(user_id=user_id)
            self._session.add(profile)

        # Only fill blanks. A headline the user wrote outranks a parsed one.
        if parsed.headline and not profile.headline:
            profile.headline = parsed.headline
        if parsed.summary and not profile.summary:
            profile.summary = parsed.summary
        await self._session.flush()
        return profile

    # ── Version helpers ──────────────────────────────────────────────

    async def _resolve_resume(
        self, user_id: uuid.UUID, resume_id: uuid.UUID | None, label: str
    ) -> Resume:
        if resume_id is not None:
            resume = (
                await self._session.execute(
                    select(Resume).where(
                        Resume.id == resume_id,
                        Resume.user_id == user_id,
                        Resume.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if resume is None:
                raise AppError.not_found("Resume")
            return resume

        # With no explicit target, a re-upload is a new version of the primary
        # resume. Creating a fresh Resume per upload would make is_primary
        # meaningless and scatter the user's history across parallel records.
        primary = (
            await self._session.execute(
                select(Resume).where(
                    Resume.user_id == user_id,
                    Resume.is_primary,
                    Resume.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if primary is not None:
            return primary

        resume = Resume(user_id=user_id, label=label[:160], is_primary=True)
        self._session.add(resume)
        await self._session.flush()
        return resume

    async def _new_version(
        self,
        *,
        user_id: uuid.UUID,
        resume: Resume,
        source_type: SourceType,
        storage_key: str | None = None,
        filename: str | None = None,
        byte_size: int | None = None,
        checksum: bytes | None = None,
        mime_type: str | None = None,
    ) -> ResumeVersion:
        next_version = (
            await self._session.execute(
                select(func.coalesce(func.max(ResumeVersion.version), 0) + 1).where(
                    ResumeVersion.resume_id == resume.id
                )
            )
        ).scalar_one()

        # Older versions stay queryable but stop being the current one.
        for previous in (
            await self._session.execute(
                select(ResumeVersion).where(
                    ResumeVersion.resume_id == resume.id,
                    ResumeVersion.status.in_([ResumeStatus.READY, ResumeStatus.NEEDS_REVIEW]),
                )
            )
        ).scalars():
            previous.status = ResumeStatus.SUPERSEDED

        version = ResumeVersion(
            user_id=user_id,
            resume_id=resume.id,
            version=int(next_version),
            source_type=source_type,
            storage_key=storage_key,
            original_filename=filename[:255] if filename else None,
            byte_size=byte_size,
            checksum_sha256=checksum,
            mime_type=mime_type,
            status=ResumeStatus.EXTRACTING,
        )
        self._session.add(version)
        await self._session.flush()
        return version


def _serialize(parsed: ParsedResume) -> dict[str, Any]:
    """Snapshot of the extraction layer, kept verbatim for later diffing."""
    payload = asdict(parsed)
    for experience in payload.get("experiences", []):
        for key in ("start_month", "end_month"):
            if isinstance(experience.get(key), dt.date):
                experience[key] = experience[key].isoformat()
    return payload
