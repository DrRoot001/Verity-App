"""Structured resume parsing (PRD §10.5 FR-RES-004..010).

A deterministic parser: section detection, then per-section entity extraction
with date normalization and skill canonicalization. It runs with no network and
no API key, and it is the baseline the LLM extractor is measured against rather
than a placeholder for it — hybrid rule/model parsing is how production resume
pipelines actually work, because rules are exact on the parts that are
regular (dates, headings, contact blocks) and models are better on prose.

The rule that matters most is FR-RES-010: a metric is recorded only when it is
literally present in the source text, with its character offsets. Nothing here
computes, rounds, or infers a number.
"""

from __future__ import annotations

import calendar
import datetime as dt
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from verity.platform.logging import get_logger

log = get_logger("documents.resume_parser")


class Section(StrEnum):
    CONTACT = "contact"
    SUMMARY = "summary"
    EXPERIENCE = "experience"
    EDUCATION = "education"
    SKILLS = "skills"
    PROJECTS = "projects"
    CERTIFICATIONS = "certifications"
    OTHER = "other"


#: Heading synonyms. Ordered longest-first at match time so "work experience"
#: wins over a bare "experience".
_SECTION_HEADINGS: dict[Section, tuple[str, ...]] = {
    Section.EXPERIENCE: (
        "professional experience",
        "work experience",
        "employment history",
        "career history",
        "experience",
        "employment",
    ),
    Section.EDUCATION: ("education", "academic background", "qualifications"),
    Section.SKILLS: (
        "technical skills",
        "core competencies",
        "skills & tools",
        "skills",
        "technologies",
    ),
    Section.PROJECTS: ("selected projects", "projects", "personal projects", "portfolio"),
    Section.CERTIFICATIONS: ("certifications", "licenses", "certificates"),
    Section.SUMMARY: ("summary", "profile", "objective", "about me", "about"),
}

_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})

_PRESENT = ("present", "current", "now", "ongoing", "to date")

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"(?:\+?\d{1,3}[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")
_URL_RE = re.compile(r"(?:https?://|www\.)[\w./\-@%?=&#]+", re.IGNORECASE)

#: Numbers with units or scale that a human would recognise as a result.
_METRIC_RE = re.compile(
    r"""(
        \$\s?\d[\d,.]*\s?(?:k|m|bn|b|million|billion|thousand)?   # money
      | \d[\d,.]*\s?%                                             # percentage
      | \d[\d,.]*\s?(?:x|×)\b                                     # multiple
      | \d[\d,.]*\s?(?:k|m|bn)\b                                  # scale
      | \d[\d,.]*\s?(?:ms|s|sec|secs|seconds|min|mins|minutes|hours|hrs|days|weeks|months)\b
      | \b\d{2,}\b                                                # bare counts
    )""",
    re.IGNORECASE | re.VERBOSE,
)

_DATE_RANGE_RE = re.compile(
    r"""(?P<start>
            (?:[A-Za-z]{3,9}\.?\s+)?\d{4}
        )
        \s*(?:-|–|—|to|until)\s*
        (?P<end>
            (?:[A-Za-z]{3,9}\.?\s+)?\d{4}
          | present|current|now|ongoing|to\s+date
        )""",
    re.IGNORECASE | re.VERBOSE,
)

_BULLET_PREFIX = re.compile(r"^\s*[-•▪◦*·‣]\s*")

#: Canonical skill names with their common aliases (PRD FR-RES-009).
SKILL_ALIASES: dict[str, str] = {
    "k8s": "Kubernetes",
    "kubernetes": "Kubernetes",
    "js": "JavaScript",
    "javascript": "JavaScript",
    "ecmascript": "JavaScript",
    "ts": "TypeScript",
    "typescript": "TypeScript",
    "py": "Python",
    "python": "Python",
    "python3": "Python",
    "postgres": "PostgreSQL",
    "postgresql": "PostgreSQL",
    "psql": "PostgreSQL",
    "gcp": "Google Cloud Platform",
    "aws": "Amazon Web Services",
    "azure": "Microsoft Azure",
    "tf": "Terraform",
    "terraform": "Terraform",
    "ci/cd": "CI/CD",
    "cicd": "CI/CD",
    "ml": "Machine Learning",
    "machine learning": "Machine Learning",
    "golang": "Go",
    "go": "Go",
    "node": "Node.js",
    "nodejs": "Node.js",
    "node.js": "Node.js",
    "react": "React",
    "reactjs": "React",
    "react.js": "React",
    "sql": "SQL",
    "nosql": "NoSQL",
    "docker": "Docker",
    "redis": "Redis",
    "kafka": "Apache Kafka",
    "graphql": "GraphQL",
    "rest": "REST",
}


@dataclass(slots=True)
class ParsedMetric:
    text: str
    #: Character offsets into the source. FR-RES-010 traceability.
    start: int
    end: int


@dataclass(slots=True)
class ParsedAchievement:
    statement: str
    metrics: list[ParsedMetric] = field(default_factory=list)

    @property
    def has_quantified_metric(self) -> bool:
        return bool(self.metrics)


@dataclass(slots=True)
class ParsedExperience:
    company_name: str
    title: str
    start_month: dt.date | None = None
    end_month: dt.date | None = None
    is_current: bool = False
    date_confidence: float | None = None
    location: str | None = None
    bullets: list[str] = field(default_factory=list)
    achievements: list[ParsedAchievement] = field(default_factory=list)
    confidence: float = 0.5


@dataclass(slots=True)
class ParsedEducation:
    institution: str
    degree: str | None = None
    field_of_study: str | None = None
    start_year: int | None = None
    end_year: int | None = None


@dataclass(slots=True)
class ParsedResume:
    full_name: str | None = None
    headline: str | None = None
    #: Redacted before any prompt assembly (PRD FR-PRIV-001).
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    summary: str | None = None
    experiences: list[ParsedExperience] = field(default_factory=list)
    education: list[ParsedEducation] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    projects: list[dict[str, Any]] = field(default_factory=list)
    certifications: list[str] = field(default_factory=list)
    sections_found: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ── Section splitting ────────────────────────────────────────────────


def _match_heading(line: str) -> Section | None:
    """A heading is short, unpunctuated, and matches a known synonym."""
    stripped = line.strip().strip(":").strip()
    if not stripped or len(stripped) > 48 or stripped.endswith("."):
        return None

    lowered = stripped.lower()
    candidates = [
        (heading, section)
        for section, headings in _SECTION_HEADINGS.items()
        for heading in headings
    ]
    for heading, section in sorted(candidates, key=lambda c: -len(c[0])):
        if lowered == heading or lowered.startswith(heading + " "):
            return section

    # ALL-CAPS short lines are headings even when the wording is unusual.
    if stripped.isupper() and 2 < len(stripped) <= 32:
        for heading, section in sorted(candidates, key=lambda c: -len(c[0])):
            if heading in lowered:
                return section
    return None


def split_sections(text: str) -> dict[Section, list[str]]:
    sections: dict[Section, list[str]] = {}
    current = Section.CONTACT
    for line in text.split("\n"):
        heading = _match_heading(line)
        if heading is not None:
            current = heading
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return sections


# ── Dates ────────────────────────────────────────────────────────────


def parse_date_token(token: str) -> tuple[dt.date | None, float]:
    """Return a month-precision date and a confidence.

    Month precision is deliberate: resumes almost never state a day, and
    inventing one would be a fabricated fact (PRD FR-RES-008).
    """
    token = token.strip().rstrip(".")
    if not token:
        return None, 0.0
    if token.lower() in _PRESENT:
        return None, 1.0

    parts = token.replace(",", " ").split()
    year = next((int(p) for p in parts if p.isdigit() and len(p) == 4), None)
    if year is None or not (1950 <= year <= dt.date.today().year + 10):
        return None, 0.0

    for part in parts:
        month = _MONTHS.get(part.lower().rstrip("."))
        if month:
            return dt.date(year, month, 1), 0.95

    # Year only: January is a placeholder, and the confidence says so.
    return dt.date(year, 1, 1), 0.6


def extract_date_range(text: str) -> tuple[dt.date | None, dt.date | None, bool, float]:
    match = _DATE_RANGE_RE.search(text)
    if not match:
        return None, None, False, 0.0

    start, start_conf = parse_date_token(match.group("start"))
    end_token = match.group("end")
    is_current = end_token.lower().strip() in _PRESENT
    end, end_conf = (None, 1.0) if is_current else parse_date_token(end_token)
    return start, end, is_current, round(min(start_conf, end_conf), 3)


# ── Metrics ──────────────────────────────────────────────────────────


def extract_metrics(text: str) -> list[ParsedMetric]:
    """Literal-only metric extraction (FR-RES-010).

    Every metric keeps its offsets, so a generated claim can be traced back to
    the exact characters that justify it.
    """
    metrics: list[ParsedMetric] = []
    for match in _METRIC_RE.finditer(text):
        value = match.group(0).strip()
        # A bare 4-digit number in resume prose is nearly always a year.
        if value.isdigit() and len(value) == 4 and 1950 <= int(value) <= 2100:
            continue
        metrics.append(ParsedMetric(text=value, start=match.start(), end=match.end()))
    return metrics


# ── Experience ───────────────────────────────────────────────────────

_TITLE_HINTS = (
    "engineer",
    "developer",
    "manager",
    "director",
    "designer",
    "analyst",
    "scientist",
    "consultant",
    "lead",
    "architect",
    "product",
    "founder",
    "intern",
    "specialist",
    "administrator",
    "president",
    "officer",
    "head",
)


def _is_date_only(line: str) -> bool:
    """True when a line carries a date range and essentially nothing else."""
    if not _DATE_RANGE_RE.search(line):
        return False
    remainder = _DATE_RANGE_RE.sub("", line).strip(" ,|·–—-\t")
    return len(remainder) <= 3


def _looks_like_role_header(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > 140 or _BULLET_PREFIX.match(line):
        return False
    lowered = stripped.lower()
    return any(hint in lowered for hint in _TITLE_HINTS) or bool(_DATE_RANGE_RE.search(stripped))


def _split_company_and_title(header: str) -> tuple[str, str]:
    """Resumes use 'Title at Company', 'Company — Title', or 'Title, Company'."""
    cleaned = _DATE_RANGE_RE.sub("", header).strip(" ,|–—-·\t")

    for separator in (" at ", " @ "):
        if separator in cleaned.lower():
            idx = cleaned.lower().index(separator)
            return cleaned[idx + len(separator) :].strip(), cleaned[:idx].strip()

    for separator in ("—", "–", " | ", " - ", ","):
        if separator in cleaned:
            left, _, right = cleaned.partition(separator)
            left, right = left.strip(), right.strip()
            if any(h in left.lower() for h in _TITLE_HINTS):
                return right, left
            return left, right

    return "", cleaned


def parse_experiences(lines: list[str]) -> list[ParsedExperience]:
    experiences: list[ParsedExperience] = []
    current: ParsedExperience | None = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        bullet = _BULLET_PREFIX.match(line)
        if bullet and current is not None:
            content = _BULLET_PREFIX.sub("", line).strip()
            if content:
                current.bullets.append(content)
                metrics = extract_metrics(content)
                current.achievements.append(ParsedAchievement(statement=content, metrics=metrics))
            continue

        # A line that is *only* a date range belongs to the role above it. This
        # must be checked before the role-header test, which also matches on the
        # presence of a date and would otherwise swallow the line.
        if current is not None and _is_date_only(stripped):
            start, end, is_current, confidence = extract_date_range(stripped)
            if start or is_current:
                current.start_month = current.start_month or start
                current.end_month = current.end_month or end
                current.is_current = current.is_current or is_current
                current.date_confidence = current.date_confidence or (confidence or None)
            continue

        if _looks_like_role_header(stripped):
            company, title = _split_company_and_title(stripped)
            if not company and not title:
                continue
            start, end, is_current, confidence = extract_date_range(stripped)
            current = ParsedExperience(
                company_name=company or "Unknown",
                title=title or "Unknown",
                start_month=start,
                end_month=end,
                is_current=is_current,
                date_confidence=confidence or None,
                confidence=0.75 if company and title else 0.45,
            )
            experiences.append(current)
        elif current is not None and current.start_month is None:
            # A date frequently sits on the line after the role header.
            start, end, is_current, confidence = extract_date_range(stripped)
            if start or is_current:
                current.start_month, current.end_month = start, end
                current.is_current = is_current
                current.date_confidence = confidence or None
            elif len(stripped) < 80 and not current.bullets:
                current.location = stripped

    return experiences


# ── Skills, education, contact ───────────────────────────────────────


def canonicalize_skill(raw: str) -> str:
    cleaned = raw.strip().strip(".,;:")
    return SKILL_ALIASES.get(cleaned.lower(), cleaned)


def parse_skills(lines: list[str]) -> list[str]:
    skills: list[str] = []
    for line in lines:
        content = _BULLET_PREFIX.sub("", line).strip()
        if not content:
            continue
        # Drop a leading category label ("Languages: Python, Go").
        if ":" in content and len(content.split(":")[0]) < 40:
            content = content.split(":", 1)[1]
        for token in re.split(r"[,;|•·/]| {2,}", content):
            skill = canonicalize_skill(token)
            if 1 < len(skill) <= 60 and not skill.isdigit():
                skills.append(skill)

    seen: dict[str, None] = {}
    for skill in skills:
        seen.setdefault(skill, None)
    return list(seen)


_DEGREE_HINTS = (
    "bachelor",
    "master",
    "phd",
    "doctorate",
    "b.s",
    "bs",
    "ba",
    "b.a",
    "m.s",
    "ms",
    "mba",
    "msc",
    "bsc",
    "associate",
    "diploma",
)


def parse_education(lines: list[str]) -> list[ParsedEducation]:
    entries: list[ParsedEducation] = []
    for line in lines:
        stripped = _BULLET_PREFIX.sub("", line).strip()
        if len(stripped) < 4:
            continue

        years = [int(y) for y in re.findall(r"\b(19|20)\d{2}\b", stripped)] or []
        all_years = [int(y) for y in re.findall(r"\b(?:19|20)\d{2}\b", stripped)]
        lowered = stripped.lower()
        has_degree = any(h in lowered for h in _DEGREE_HINTS)
        has_institution = any(
            w in lowered for w in ("university", "college", "institute", "school", "academy")
        )
        if not (has_degree or has_institution):
            continue

        degree = None
        institution = stripped
        for separator in (",", "—", "–", " - ", "|"):
            if separator in stripped:
                left, _, right = stripped.partition(separator)
                if any(h in left.lower() for h in _DEGREE_HINTS):
                    degree, institution = left.strip(), right.strip()
                else:
                    degree, institution = right.strip(), left.strip()
                break

        institution = re.sub(r"\b(?:19|20)\d{2}\b", "", institution).strip(" ,-–—")
        entries.append(
            ParsedEducation(
                institution=institution or stripped,
                degree=degree,
                start_year=min(all_years) if len(all_years) > 1 else None,
                end_year=max(all_years) if all_years else None,
            )
        )
        del years
    return entries


def parse_contact(lines: list[str]) -> tuple[str | None, list[str], list[str], list[str]]:
    blob = "\n".join(lines)
    emails = _EMAIL_RE.findall(blob)
    phones = [p for p in _PHONE_RE.findall(blob) if len(re.sub(r"\D", "", p)) >= 10]
    urls = _URL_RE.findall(blob)

    name: str | None = None
    for line in lines[:6]:
        stripped = line.strip()
        if not stripped or _EMAIL_RE.search(stripped) or _URL_RE.search(stripped):
            continue
        words = stripped.split()
        if 1 < len(words) <= 5 and all(w[:1].isupper() for w in words if w[:1].isalpha()):
            name = stripped
            break
    return name, emails, phones, urls


# ── Entry point ──────────────────────────────────────────────────────


def parse_resume(text: str) -> ParsedResume:
    sections = split_sections(text)
    result = ParsedResume(sections_found=[str(s) for s in sections])

    name, emails, phones, urls = parse_contact(sections.get(Section.CONTACT, []))
    result.full_name, result.emails, result.phones, result.urls = name, emails, phones, urls

    if summary_lines := sections.get(Section.SUMMARY):
        summary = " ".join(line.strip() for line in summary_lines if line.strip())
        result.summary = summary[:2000] or None
        result.headline = summary.split(".")[0][:300] or None

    result.experiences = parse_experiences(sections.get(Section.EXPERIENCE, []))
    result.education = parse_education(sections.get(Section.EDUCATION, []))
    result.skills = parse_skills(sections.get(Section.SKILLS, []))
    result.certifications = [
        _BULLET_PREFIX.sub("", line).strip()
        for line in sections.get(Section.CERTIFICATIONS, [])
        if line.strip()
    ]

    for line in sections.get(Section.PROJECTS, []):
        content = _BULLET_PREFIX.sub("", line).strip()
        if len(content) > 8:
            result.projects.append({"name": content[:200], "description": content})

    if not result.experiences:
        result.warnings.append("no_experience_detected")
    if not result.skills:
        result.warnings.append("no_skills_detected")
    if Section.EXPERIENCE not in sections:
        result.warnings.append("no_experience_section_heading")

    log.info(
        "resume_parsed",
        experiences=len(result.experiences),
        skills=len(result.skills),
        education=len(result.education),
    )
    return result
