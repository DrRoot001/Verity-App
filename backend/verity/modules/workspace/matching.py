"""Candidate/JD match with evidence binding (PRD §10.6 FR-JD-005).

A match here is not a similarity score. Each requirement is resolved to
*specific approved graph nodes* that support it, so the UI can show "strong —
because of these two roles" rather than an unexplained percentage. Requirements
with no supporting evidence become gaps, and gaps are what the preparation
engine turns into ranked tasks (PRD §10.8).

The overall score is a weighted roll-up whose formula is exposed to the UI
(FR-JD-005 requires the weighting be visible on hover), so it must stay simple
and explainable rather than tuned.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from verity.modules.candidate_graph.models import (
    Achievement,
    Experience,
    NodeStatus,
    Skill,
)
from verity.modules.documents.extraction.resume_parser import canonicalize_skill
from verity.modules.documents.models import Job
from verity.platform.logging import get_logger

log = get_logger("workspace.matching")

MatchStatus = Literal["strong", "partial", "gap"]

#: Weights for the overall roll-up. Must-haves dominate because a missing
#: must-have is what actually loses an interview loop.
WEIGHT_MUST_HAVE = 0.65
WEIGHT_NICE_TO_HAVE = 0.15
WEIGHT_TECHNOLOGY = 0.20

_STATUS_VALUE: dict[str, float] = {"strong": 1.0, "partial": 0.5, "gap": 0.0}

_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "with",
        "by",
        "from",
        "as",
        "is",
        "are",
        "be",
        "been",
        "being",
        "you",
        "your",
        "our",
        "we",
        "their",
        "have",
        "has",
        "had",
        "will",
        "would",
        "should",
        "can",
        "could",
        "must",
        "experience",
        "years",
        "work",
        "working",
        "ability",
        "strong",
        "excellent",
        "good",
        "knowledge",
        "understanding",
        "proficiency",
        "familiarity",
        "plus",
        "using",
        "use",
        "used",
    ]
)

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#.]*")


@dataclass(slots=True)
class EvidenceRef:
    id: str
    type: str
    label: str


@dataclass(slots=True)
class RequirementMatch:
    requirement: str
    kind: str
    status: MatchStatus
    evidence: list[EvidenceRef] = field(default_factory=list)
    rationale: str = ""
    matched_terms: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MatchResult:
    overall_score: float = 0.0
    requirements: list[RequirementMatch] = field(default_factory=list)
    technology_coverage: dict[str, bool] = field(default_factory=dict)
    strengths: list[RequirementMatch] = field(default_factory=list)
    gaps: list[RequirementMatch] = field(default_factory=list)
    #: The weighting, echoed so the UI can explain the number (FR-JD-005).
    formula: dict[str, float] = field(
        default_factory=lambda: {
            "must_have": WEIGHT_MUST_HAVE,
            "nice_to_have": WEIGHT_NICE_TO_HAVE,
            "technology_coverage": WEIGHT_TECHNOLOGY,
        }
    )
    computed_without_jd: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "overall_score": self.overall_score,
            "requirements": [asdict(r) for r in self.requirements],
            "technology_coverage": self.technology_coverage,
            "strengths": [asdict(r) for r in self.strengths],
            "gaps": [asdict(r) for r in self.gaps],
            "formula": self.formula,
            "computed_without_jd": self.computed_without_jd,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> MatchResult:
        if not payload:
            return cls()

        def rebuild(rows: list[dict[str, Any]]) -> list[RequirementMatch]:
            return [
                RequirementMatch(
                    requirement=r["requirement"],
                    kind=r["kind"],
                    status=r["status"],
                    evidence=[EvidenceRef(**e) for e in r.get("evidence", [])],
                    rationale=r.get("rationale", ""),
                    matched_terms=r.get("matched_terms", []),
                )
                for r in rows
            ]

        return cls(
            overall_score=payload.get("overall_score", 0.0),
            requirements=rebuild(payload.get("requirements", [])),
            technology_coverage=payload.get("technology_coverage", {}),
            strengths=rebuild(payload.get("strengths", [])),
            gaps=rebuild(payload.get("gaps", [])),
            formula=payload.get("formula", {}),
            computed_without_jd=payload.get("computed_without_jd", False),
        )


def _significant_terms(text: str) -> set[str]:
    return {
        t.lower() for t in _TOKEN_RE.findall(text) if len(t) > 2 and t.lower() not in _STOPWORDS
    }


@dataclass(slots=True)
class _CandidateIndex:
    """Approved evidence, indexed once per match run."""

    skills: dict[str, EvidenceRef]
    experiences: list[tuple[EvidenceRef, set[str]]]
    achievements: list[tuple[EvidenceRef, set[str]]]

    def all_terms(self) -> set[str]:
        terms = set(self.skills)
        for _, t in self.experiences:
            terms |= t
        for _, t in self.achievements:
            terms |= t
        return terms


async def _build_index(session: AsyncSession, user_id: uuid.UUID) -> _CandidateIndex:
    """Only approved nodes. A gap must never be filled by an unreviewed guess."""
    skills: dict[str, EvidenceRef] = {}
    for skill in (
        await session.execute(
            select(Skill).where(
                Skill.user_id == user_id,
                Skill.status == NodeStatus.APPROVED,
                Skill.deleted_at.is_(None),
            )
        )
    ).scalars():
        skills[skill.canonical_name.lower()] = EvidenceRef(
            id=str(skill.id), type="skill", label=skill.canonical_name
        )

    experiences: list[tuple[EvidenceRef, set[str]]] = []
    for experience in (
        await session.execute(
            select(Experience).where(
                Experience.user_id == user_id,
                Experience.status == NodeStatus.APPROVED,
                Experience.deleted_at.is_(None),
            )
        )
    ).scalars():
        blob = " ".join(
            [
                experience.title,
                experience.company_name,
                experience.description or "",
                *(experience.bullets or []),
            ]
        )
        experiences.append(
            (
                EvidenceRef(
                    id=str(experience.id),
                    type="experience",
                    label=f"{experience.title} at {experience.company_name}",
                ),
                _significant_terms(blob),
            )
        )

    achievements: list[tuple[EvidenceRef, set[str]]] = []
    for achievement in (
        await session.execute(
            select(Achievement).where(
                Achievement.user_id == user_id,
                Achievement.status == NodeStatus.APPROVED,
                Achievement.deleted_at.is_(None),
            )
        )
    ).scalars():
        achievements.append(
            (
                EvidenceRef(
                    id=str(achievement.id),
                    type="achievement",
                    label=achievement.statement[:120],
                ),
                _significant_terms(achievement.statement),
            )
        )

    return _CandidateIndex(skills=skills, experiences=experiences, achievements=achievements)


def _match_requirement(requirement: str, kind: str, index: _CandidateIndex) -> RequirementMatch:
    terms = _significant_terms(requirement)
    if not terms:
        return RequirementMatch(
            requirement=requirement,
            kind=kind,
            status="gap",
            rationale="No comparable terms in this requirement.",
        )

    evidence: list[EvidenceRef] = []
    matched: set[str] = set()

    for term in terms:
        canonical = canonicalize_skill(term).lower()
        if hit := (index.skills.get(term) or index.skills.get(canonical)):
            if hit not in evidence:
                evidence.append(hit)
            matched.add(term)

    for ref, exp_terms in index.experiences + index.achievements:
        overlap = terms & exp_terms
        # Two overlapping significant terms is the threshold for "this record
        # is about the same thing"; one is too often an incidental word.
        if len(overlap) >= 2:
            if ref not in evidence:
                evidence.append(ref)
            matched |= overlap

    coverage = len(matched) / len(terms)
    if coverage >= 0.5 and evidence:
        status: MatchStatus = "strong"
        rationale = f"Covered by {len(evidence)} approved record(s)."
    elif evidence:
        status = "partial"
        rationale = f"Partially covered ({int(coverage * 100)}% of key terms)."
    else:
        status = "gap"
        rationale = "No approved experience matches this requirement."

    return RequirementMatch(
        requirement=requirement,
        kind=kind,
        status=status,
        evidence=evidence[:4],
        rationale=rationale,
        matched_terms=sorted(matched)[:8],
    )


async def compute_match(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    job: Job | None,
    opportunity_technologies: list[str],
    must_have: list[str],
    nice_to_have: list[str],
) -> MatchResult:
    index = await _build_index(session, user_id)

    if job is None and not must_have:
        # PRD §8.2 skip path: with no JD there is nothing to match against, and
        # inventing a score would be worse than admitting the gap.
        return MatchResult(computed_without_jd=True)

    requirements = [_match_requirement(r, "must_have", index) for r in must_have]
    requirements += [_match_requirement(r, "nice_to_have", index) for r in nice_to_have]

    candidate_terms = index.all_terms()
    coverage = {
        tech: (tech.lower() in index.skills or tech.lower() in candidate_terms)
        for tech in opportunity_technologies
    }

    must_scores = [_STATUS_VALUE[r.status] for r in requirements if r.kind == "must_have"]
    nice_scores = [_STATUS_VALUE[r.status] for r in requirements if r.kind == "nice_to_have"]

    def mean(values: list[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    tech_score = (sum(coverage.values()) / len(coverage)) if coverage else 0.0

    # Weights are redistributed across the components that exist, so a posting
    # with no "nice to have" section is not silently penalised.
    components = [
        (WEIGHT_MUST_HAVE, mean(must_scores), bool(must_scores)),
        (WEIGHT_NICE_TO_HAVE, mean(nice_scores), bool(nice_scores)),
        (WEIGHT_TECHNOLOGY, tech_score, bool(coverage)),
    ]
    total_weight = sum(w for w, _, present in components if present)
    overall = (
        sum(w * v for w, v, present in components if present) / total_weight
        if total_weight
        else 0.0
    )

    result = MatchResult(
        overall_score=round(overall * 100, 1),
        requirements=requirements,
        technology_coverage=coverage,
        strengths=[r for r in requirements if r.status == "strong"],
        gaps=[r for r in requirements if r.status == "gap"],
        computed_without_jd=job is None,
    )

    log.info(
        "match_computed",
        requirements=len(requirements),
        gaps=len(result.gaps),
        score=result.overall_score,
    )
    return result
