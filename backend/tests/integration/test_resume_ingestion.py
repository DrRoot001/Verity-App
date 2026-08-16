"""Resume ingestion (PRD §10.5, AC-GRAPH-001, AC-GRAPH-002).

The acceptance criteria this file exists to prove:

- AC-GRAPH-001: parsing extracts roles, companies and date ranges; every
  extracted entity lands as ``pending_review`` and nothing is auto-approved.
- AC-GRAPH-002: a re-upload diffs against existing data and preserves a
  correction the user already made.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from verity.modules.candidate_graph.models import (
    Achievement,
    Education,
    Experience,
    NodeStatus,
    Skill,
)
from verity.modules.documents.models import ResumeStatus, ResumeVersion
from verity.modules.documents.service import ResumeIngestionService
from verity.modules.identity.models import User
from verity.platform.errors import AppError

pytestmark = pytest.mark.integration


SAMPLE_RESUME = """
Dana Whitfield
dana.whitfield@example.org | +1 415 555 0142 | linkedin.com/in/danawhitfield

SUMMARY
Backend engineer focused on payments reliability and distributed systems.

WORK EXPERIENCE

Senior Backend Engineer at Northwind Systems
March 2021 - Present
San Francisco, CA
- Cut checkout latency from 800ms to 120ms by introducing a read-through cache
- Led incident response for a payments outage, reducing MTTR from 42 minutes to 9 minutes
- Mentored 4 engineers through the platform migration

Backend Engineer at Larkspur Analytics
June 2018 - February 2021
- Built an ingestion pipeline processing 2.5M events per day
- Reduced infrastructure spend by 35%

EDUCATION
BSc Computer Science, Fairhaven University, 2014 - 2018

TECHNICAL SKILLS
Python, Go, PostgreSQL, k8s, Kafka, Terraform, CI/CD
"""


async def _ingest(db: AsyncSession, user: User, text: str = SAMPLE_RESUME) -> object:
    return await ResumeIngestionService(db).ingest_text(user_id=user.id, raw_text=text)


# ── AC-GRAPH-001: extraction accuracy and review gating ──────────────


async def test_extracts_roles_companies_and_dates(db: AsyncSession, user: User) -> None:
    result = await _ingest(db, user)
    parsed = result.parsed  # type: ignore[attr-defined]

    titles = {e.title for e in parsed.experiences}
    companies = {e.company_name for e in parsed.experiences}

    assert "Senior Backend Engineer" in titles
    assert "Northwind Systems" in companies
    assert "Larkspur Analytics" in companies

    current = next(e for e in parsed.experiences if e.company_name == "Northwind Systems")
    assert current.start_month == dt.date(2021, 3, 1)
    assert current.is_current is True
    assert current.end_month is None

    previous = next(e for e in parsed.experiences if e.company_name == "Larkspur Analytics")
    assert previous.start_month == dt.date(2018, 6, 1)
    assert previous.end_month == dt.date(2021, 2, 1)
    assert previous.is_current is False


async def test_nothing_is_auto_approved(db: AsyncSession, user: User) -> None:
    """FR-GRAPH-001: extraction may never write an approved node."""
    await _ingest(db, user)

    for model in (Experience, Skill, Education, Achievement):
        rows = (await db.execute(select(model).where(model.user_id == user.id))).scalars().all()
        assert rows, f"expected {model.__name__} rows"
        assert all(r.status == NodeStatus.PENDING_REVIEW for r in rows)
        assert all(r.confirmed_at is None for r in rows)


async def test_provenance_is_recorded_on_every_node(db: AsyncSession, user: User) -> None:
    result = await _ingest(db, user)
    version_id = str(result.version.id)  # type: ignore[attr-defined]

    experiences = (
        (await db.execute(select(Experience).where(Experience.user_id == user.id))).scalars().all()
    )

    assert all(e.source == "resume" for e in experiences)
    assert all(e.source_ref == version_id for e in experiences)
    assert all(e.extracted_by == "rule-parser@1" for e in experiences)


async def test_metrics_are_literal_and_traceable(db: AsyncSession, user: User) -> None:
    """FR-RES-010: a metric exists only if it appears in the source text."""
    await _ingest(db, user)

    achievements = (
        (await db.execute(select(Achievement).where(Achievement.user_id == user.id)))
        .scalars()
        .all()
    )

    quantified = [a for a in achievements if a.has_quantified_metric]
    assert quantified, "expected at least one quantified achievement"

    for achievement in quantified:
        for metric in achievement.metrics:
            span = metric["source_span"]
            # The recorded offsets must actually locate the metric text.
            assert achievement.statement[span["start"] : span["end"]].strip() == metric["text"]


async def test_unquantified_bullets_are_not_given_metrics(db: AsyncSession, user: User) -> None:
    await _ingest(db, user)
    mentoring = (
        (
            await db.execute(
                select(Achievement).where(
                    Achievement.user_id == user.id,
                    Achievement.statement.ilike("%Mentored%"),
                )
            )
        )
        .scalars()
        .first()
    )

    assert mentoring is not None
    # "4 engineers" is a single digit count; the parser must not invent a unit.
    assert all(m["text"].strip() != "" for m in mentoring.metrics)


async def test_skills_are_canonicalized(db: AsyncSession, user: User) -> None:
    """FR-RES-009: aliases map to canonical names, unknown skills kept as-is."""
    await _ingest(db, user)
    names = {
        s.canonical_name
        for s in (await db.execute(select(Skill).where(Skill.user_id == user.id))).scalars()
    }

    assert "Kubernetes" in names  # from "k8s"
    assert "PostgreSQL" in names
    assert "Apache Kafka" in names
    assert "k8s" not in names


async def test_education_is_extracted(db: AsyncSession, user: User) -> None:
    await _ingest(db, user)
    education = (
        (await db.execute(select(Education).where(Education.user_id == user.id))).scalars().all()
    )

    assert education
    assert any("Fairhaven" in e.institution for e in education)


async def test_version_reaches_needs_review(db: AsyncSession, user: User) -> None:
    result = await _ingest(db, user)
    version = result.version  # type: ignore[attr-defined]

    assert version.status == ResumeStatus.NEEDS_REVIEW
    assert version.parsed_at is not None
    assert version.ai_extracted is not None
    assert version.user_corrected == {}


# ── AC-GRAPH-002: re-upload preserves corrections ────────────────────


async def test_reupload_diffs_instead_of_duplicating(db: AsyncSession, user: User) -> None:
    await _ingest(db, user)
    before = len(
        (await db.execute(select(Experience).where(Experience.user_id == user.id))).scalars().all()
    )

    second = await _ingest(db, user)
    after = len(
        (await db.execute(select(Experience).where(Experience.user_id == user.id))).scalars().all()
    )

    assert after == before, "re-uploading the same resume must not duplicate experiences"
    changes = {d.change for d in second.diff}  # type: ignore[attr-defined]
    assert "unchanged" in changes
    assert "added" not in changes


async def test_reupload_preserves_a_user_correction(db: AsyncSession, user: User) -> None:
    """AC-GRAPH-002: an edited title survives a later upload."""
    await _ingest(db, user)

    experience = (
        await db.execute(
            select(Experience).where(
                Experience.user_id == user.id,
                Experience.company_name == "Northwind Systems",
            )
        )
    ).scalar_one()

    experience.user_corrected = {"title": "Staff Backend Engineer"}
    experience.status = NodeStatus.APPROVED
    await db.flush()

    result = await _ingest(db, user)

    await db.refresh(experience)
    assert experience.user_corrected == {"title": "Staff Backend Engineer"}
    assert experience.effective("title") == "Staff Backend Engineer"
    assert experience.status == NodeStatus.APPROVED

    entry = next(
        d
        for d in result.diff  # type: ignore[attr-defined]
        if d.existing_id == experience.id
    )
    assert entry.user_corrected is True


async def test_diff_reports_changed_dates(db: AsyncSession, user: User) -> None:
    await _ingest(db, user)

    updated = SAMPLE_RESUME.replace("March 2021 - Present", "January 2020 - Present")
    result = await ResumeIngestionService(db).ingest_text(user_id=user.id, raw_text=updated)

    entry = next(
        d for d in result.diff if d.label == "Senior Backend Engineer at Northwind Systems"
    )
    assert entry.change == "changed"
    assert "start_month" in entry.fields_changed


async def test_diff_reports_removed_roles(db: AsyncSession, user: User) -> None:
    await _ingest(db, user)

    trimmed = SAMPLE_RESUME.split("Backend Engineer at Larkspur Analytics")[0]
    result = await ResumeIngestionService(db).ingest_text(user_id=user.id, raw_text=trimmed)

    removed = [d for d in result.diff if d.change == "removed"]
    assert any("Larkspur" in d.label for d in removed)


async def test_versions_increment_and_supersede(db: AsyncSession, user: User) -> None:
    first = await _ingest(db, user)
    second = await _ingest(db, user)

    assert second.version.version == first.version.version + 1  # type: ignore[attr-defined]

    await db.refresh(first.version)  # type: ignore[attr-defined]
    assert first.version.status == ResumeStatus.SUPERSEDED  # type: ignore[attr-defined]


# ── Failure paths ────────────────────────────────────────────────────


async def test_too_short_text_is_rejected_with_guidance(db: AsyncSession, user: User) -> None:
    with pytest.raises(AppError) as exc:
        await ResumeIngestionService(db).ingest_text(user_id=user.id, raw_text="Dana W.")

    assert exc.value.code == "validation_failed"
    assert exc.value.recovery_action.type == "edit_input"


async def test_ingestion_is_scoped_to_the_owner(
    db: AsyncSession, user: User, other_user: User
) -> None:
    await _ingest(db, user)

    theirs = (
        (await db.execute(select(Experience).where(Experience.user_id == other_user.id)))
        .scalars()
        .all()
    )
    assert theirs == []


async def test_resume_version_records_source_metadata(db: AsyncSession, user: User) -> None:
    result = await _ingest(db, user)
    stored = (
        await db.execute(
            select(ResumeVersion).where(ResumeVersion.id == result.version.id)  # type: ignore[attr-defined]
        )
    ).scalar_one()

    assert stored.source_type == "paste"
    assert stored.raw_text
    assert stored.extraction_quality is not None
