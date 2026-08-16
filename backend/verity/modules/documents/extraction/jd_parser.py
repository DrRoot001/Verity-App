"""Job description extraction (PRD §10.6 FR-JD-002..004).

The central rule is FR-JD-002: **facts and inferences are separate objects**.
A requirement quoted from the posting and a model's guess about likely interview
themes are different kinds of claim, and the UI renders them differently. Mixing
them into one blob is how a product ends up presenting a guess as a fact.

Facts carry the verbatim span they came from. Inferences carry a confidence and
a stated basis, so "likely to be a systems-heavy loop" can always be traced to
why the system thinks so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from verity.modules.documents.extraction.resume_parser import canonicalize_skill
from verity.platform.logging import get_logger

log = get_logger("documents.jd_parser")

_BULLET = re.compile(r"^\s*[-•▪◦*·‣]\s*")

_SECTION_PATTERNS: dict[str, tuple[str, ...]] = {
    "responsibilities": (
        "what you'll do",
        "what you will do",
        "responsibilities",
        "the role",
        "your impact",
        "day to day",
        "you will",
    ),
    "must_have": (
        "requirements",
        "qualifications",
        "what we're looking for",
        "must have",
        "required",
        "minimum qualifications",
        "basic qualifications",
        "you have",
    ),
    "nice_to_have": (
        "nice to have",
        "preferred",
        "bonus",
        "plus",
        "preferred qualifications",
        "desirable",
        "even better",
    ),
    "benefits": ("benefits", "perks", "what we offer", "compensation"),
    "about": ("about us", "about the company", "who we are", "our mission"),
}

_SENIORITY_MARKERS: tuple[tuple[str, str], ...] = (
    ("principal", "principal"),
    ("staff", "staff"),
    ("distinguished", "principal"),
    ("director", "director"),
    ("head of", "director"),
    ("vp ", "exec"),
    ("senior", "senior"),
    ("sr.", "senior"),
    ("sr ", "senior"),
    ("lead", "lead"),
    ("manager", "manager"),
    ("junior", "entry"),
    ("jr.", "entry"),
    ("entry", "entry"),
    ("intern", "student"),
    ("graduate", "entry"),
    ("new grad", "entry"),
)

_REMOTE_MARKERS: tuple[tuple[str, str], ...] = (
    ("fully remote", "remote"),
    ("100% remote", "remote"),
    ("remote-first", "remote"),
    ("hybrid", "hybrid"),
    ("on-site", "onsite"),
    ("onsite", "onsite"),
    ("in office", "onsite"),
    ("remote", "remote"),
)

_YEARS_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:years|yrs)", re.IGNORECASE)

#: Technology vocabulary. Detection is exact-token so "Go" does not match "going".
_TECH_TOKENS: frozenset[str] = frozenset(
    [
        "python",
        "java",
        "javascript",
        "typescript",
        "go",
        "golang",
        "rust",
        "ruby",
        "php",
        "scala",
        "kotlin",
        "swift",
        "c",
        "c++",
        "c#",
        "sql",
        "nosql",
        "postgresql",
        "mysql",
        "mongodb",
        "redis",
        "elasticsearch",
        "kafka",
        "rabbitmq",
        "aws",
        "gcp",
        "azure",
        "kubernetes",
        "k8s",
        "docker",
        "terraform",
        "ansible",
        "jenkins",
        "circleci",
        "react",
        "angular",
        "vue",
        "svelte",
        "node",
        "nodejs",
        "django",
        "flask",
        "fastapi",
        "rails",
        "spring",
        "graphql",
        "rest",
        "grpc",
        "microservices",
        "serverless",
        "lambda",
        "pytorch",
        "tensorflow",
        "pandas",
        "numpy",
        "spark",
        "hadoop",
        "airflow",
        "dbt",
        "snowflake",
        "kafka",
        "prometheus",
        "grafana",
        "datadog",
        "opentelemetry",
    ]
)

#: Behavioural competencies stated in postings, mapped to Story Bank categories
#: so a JD requirement can be matched directly against approved stories.
_COMPETENCY_SIGNALS: dict[str, tuple[str, ...]] = {
    "leadership": ("lead", "leadership", "mentor", "coach", "manage a team"),
    "ownership": ("ownership", "own the", "end-to-end", "autonomy", "self-directed"),
    "collaboration": ("collaborate", "cross-functional", "partner with", "stakeholder"),
    "ambiguity": ("ambiguity", "ambiguous", "undefined", "zero to one", "0 to 1"),
    "customer_impact": ("customer", "user-focused", "user impact", "client"),
    "prioritization": ("prioritis", "prioritiz", "trade-off", "tradeoff", "roadmap"),
    "technical_challenge": ("scale", "scalability", "distributed", "performance", "reliability"),
    "crisis_recovery": ("on-call", "oncall", "incident", "outage", "production support"),
}


@dataclass(slots=True)
class Requirement:
    """A single stated requirement, kept with the text it came from."""

    text: str
    kind: str  # must_have | nice_to_have | responsibility
    technologies: list[str] = field(default_factory=list)
    years_experience: int | None = None
    source_span: dict[str, int] | None = None


@dataclass(slots=True)
class JobFacts:
    """Verbatim extraction. Everything here is present in the posting."""

    title: str | None = None
    company_name: str | None = None
    location: str | None = None
    remote_policy: str | None = None
    responsibilities: list[Requirement] = field(default_factory=list)
    must_have: list[Requirement] = field(default_factory=list)
    nice_to_have: list[Requirement] = field(default_factory=list)
    technologies: list[str] = field(default_factory=list)
    stated_years_experience: int | None = None
    sections_found: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Inference:
    value: str
    confidence: float
    basis: str


@dataclass(slots=True)
class JobInferences:
    """Derived. Every entry states its confidence and why."""

    seniority: Inference | None = None
    role_family: Inference | None = None
    likely_themes: list[Inference] = field(default_factory=list)
    behavioral_competencies: list[Inference] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ParsedJob:
    facts: JobFacts
    inferences: JobInferences
    warnings: list[str] = field(default_factory=list)


def _classify_heading(line: str) -> str | None:
    stripped = line.strip().strip(":").lower()
    if not stripped or len(stripped) > 64:
        return None
    for section, patterns in _SECTION_PATTERNS.items():
        if any(stripped == p or stripped.startswith(p) for p in patterns):
            return section
    return None


def _split_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {"header": []}
    current = "header"
    for line in text.split("\n"):
        heading = _classify_heading(line)
        if heading:
            current = heading
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return sections


def _detect_technologies(text: str) -> list[str]:
    """Exact-token matching, then canonicalization to the resume vocabulary.

    Sharing ``canonicalize_skill`` with the resume parser is what makes JD
    requirements and candidate skills comparable at all — otherwise "k8s" on a
    resume never matches "Kubernetes" in a posting.
    """
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9+#.]*", text.lower())
    found: dict[str, None] = {}
    for token in tokens:
        if token in _TECH_TOKENS:
            found.setdefault(canonicalize_skill(token), None)
    return list(found)


def _extract_requirements(lines: list[str], kind: str, offset_base: int) -> list[Requirement]:
    requirements: list[Requirement] = []
    cursor = offset_base
    for line in lines:
        content = _BULLET.sub("", line).strip()
        cursor += len(line) + 1
        if len(content) < 12:
            continue
        years = _YEARS_RE.search(content)
        requirements.append(
            Requirement(
                text=content[:600],
                kind=kind,
                technologies=_detect_technologies(content),
                years_experience=int(years.group(1)) if years else None,
                source_span={"start": cursor - len(line) - 1, "end": cursor},
            )
        )
    return requirements


def _infer_seniority(title: str | None, text: str) -> Inference | None:
    haystack = f"{title or ''} {text[:600]}".lower()
    for marker, level in _SENIORITY_MARKERS:
        if marker in haystack:
            in_title = title is not None and marker in title.lower()
            return Inference(
                value=level,
                confidence=0.85 if in_title else 0.6,
                basis=f"matched '{marker.strip()}' in {'title' if in_title else 'description'}",
            )

    if years := _YEARS_RE.search(text):
        count = int(years.group(1))
        level = "entry" if count <= 2 else "mid" if count <= 5 else "senior"
        return Inference(
            value=level,
            confidence=0.55,
            basis=f"{count}+ years of experience requested",
        )
    return None


def _infer_competencies(text: str) -> list[Inference]:
    lowered = text.lower()
    results: list[Inference] = []
    for competency, signals in _COMPETENCY_SIGNALS.items():
        hits = [s for s in signals if s in lowered]
        if hits:
            results.append(
                Inference(
                    value=competency,
                    confidence=round(min(0.9, 0.45 + 0.15 * len(hits)), 2),
                    basis=f"posting mentions {', '.join(repr(h) for h in hits[:3])}",
                )
            )
    return sorted(results, key=lambda i: -i.confidence)


def _infer_themes(facts: JobFacts, competencies: list[Inference]) -> list[Inference]:
    """Predicted interview areas, each explaining its own reasoning."""
    themes: list[Inference] = []

    if facts.technologies:
        top = ", ".join(facts.technologies[:4])
        themes.append(
            Inference(
                value="technical depth in the stated stack",
                confidence=0.8,
                basis=f"posting names {top}",
            )
        )

    for competency in competencies[:3]:
        themes.append(
            Inference(
                value=f"behavioral: {competency.value}",
                confidence=round(competency.confidence * 0.9, 2),
                basis=competency.basis,
            )
        )

    if any(
        "scale" in r.text.lower() or "distributed" in r.text.lower()
        for r in facts.must_have + facts.responsibilities
    ):
        themes.append(
            Inference(
                value="system design and scaling tradeoffs",
                confidence=0.7,
                basis="requirements reference scale or distributed systems",
            )
        )
    return themes


def parse_job_description(text: str, *, title_hint: str | None = None) -> ParsedJob:
    sections = _split_sections(text)
    facts = JobFacts(sections_found=[s for s in sections if sections[s]])

    header_lines = [line.strip() for line in sections.get("header", []) if line.strip()]
    facts.title = title_hint or (header_lines[0][:200] if header_lines else None)

    lowered = text.lower()
    for marker, policy in _REMOTE_MARKERS:
        if marker in lowered:
            facts.remote_policy = policy
            break

    offset = 0
    facts.responsibilities = _extract_requirements(
        sections.get("responsibilities", []), "responsibility", offset
    )
    facts.must_have = _extract_requirements(sections.get("must_have", []), "must_have", offset)
    facts.nice_to_have = _extract_requirements(
        sections.get("nice_to_have", []), "nice_to_have", offset
    )
    facts.technologies = _detect_technologies(text)

    if years := _YEARS_RE.search(text):
        facts.stated_years_experience = int(years.group(1))

    competencies = _infer_competencies(text)
    inferences = JobInferences(
        seniority=_infer_seniority(facts.title, text),
        behavioral_competencies=competencies,
        keywords=facts.technologies[:30],
    )
    inferences.likely_themes = _infer_themes(facts, competencies)

    warnings: list[str] = []
    if not facts.must_have:
        warnings.append("no_requirements_section_detected")
    if not facts.technologies:
        warnings.append("no_technologies_detected")

    log.info(
        "jd_parsed",
        must_have=len(facts.must_have),
        responsibilities=len(facts.responsibilities),
        technologies=len(facts.technologies),
    )
    return ParsedJob(facts=facts, inferences=inferences, warnings=warnings)


def serialize_facts(facts: JobFacts) -> dict[str, Any]:
    return {
        "title": facts.title,
        "company_name": facts.company_name,
        "location": facts.location,
        "remote_policy": facts.remote_policy,
        "technologies": facts.technologies,
        "stated_years_experience": facts.stated_years_experience,
        "sections_found": facts.sections_found,
        "responsibilities": [_requirement_dict(r) for r in facts.responsibilities],
        "must_have": [_requirement_dict(r) for r in facts.must_have],
        "nice_to_have": [_requirement_dict(r) for r in facts.nice_to_have],
    }


def _requirement_dict(requirement: Requirement) -> dict[str, Any]:
    return {
        "text": requirement.text,
        "kind": requirement.kind,
        "technologies": requirement.technologies,
        "years_experience": requirement.years_experience,
        "source_span": requirement.source_span,
    }


def serialize_inferences(inferences: JobInferences) -> dict[str, Any]:
    def one(value: Inference | None) -> dict[str, Any] | None:
        if value is None:
            return None
        return {"value": value.value, "confidence": value.confidence, "basis": value.basis}

    return {
        "seniority": one(inferences.seniority),
        "role_family": one(inferences.role_family),
        "likely_themes": [one(i) for i in inferences.likely_themes],
        "behavioral_competencies": [one(i) for i in inferences.behavioral_competencies],
        "keywords": inferences.keywords,
    }
