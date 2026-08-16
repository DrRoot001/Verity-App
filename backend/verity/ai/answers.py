"""Copilot answer schema, response modes and grounding (PRD §14.4-14.5, §12.6).

Grounded Candidate Mode is enforced at the *schema* level, not by asking a model
nicely: a key point may only be typed ``candidate_fact`` if it carries
``evidence_ids``, and the validator re-checks each fact's entities and numerals
against the evidence text. Anything unsupported is downgraded to guidance rather
than rendered as the candidate's history.

The validator is deliberately pure Python. It runs on every generation inside a
2-second end-to-end budget (§32.1), so a second model call to check the first
would spend the entire budget on verification.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from verity.platform.telemetry import grounding_violations


class ClaimType(StrEnum):
    """What kind of claim a key point is making (PRD §12.6)."""

    CANDIDATE_FACT = "candidate_fact"
    GUIDANCE = "guidance"
    GENERAL_KNOWLEDGE = "general_knowledge"


class ResponseMode(StrEnum):
    CONCISE = "concise"
    BALANCED = "balanced"
    DETAILED = "detailed"
    TALKING_POINTS = "talking_points"
    STAR = "star"
    EXECUTIVE = "executive"
    TECHNICAL = "technical"


@dataclass(frozen=True, slots=True)
class ModeSpec:
    """PRD §14.5. Caps are enforced server-side, not requested politely."""

    direction_sentences: int
    max_points: int
    max_words_per_point: int
    max_evidence: int
    include_structure: bool
    include_expanded: bool


MODES: dict[str, ModeSpec] = {
    ResponseMode.CONCISE: ModeSpec(1, 3, 12, 2, False, False),
    ResponseMode.BALANCED: ModeSpec(2, 4, 18, 3, True, False),
    ResponseMode.DETAILED: ModeSpec(2, 5, 25, 4, True, True),
    ResponseMode.TALKING_POINTS: ModeSpec(0, 5, 10, 3, False, False),
    ResponseMode.STAR: ModeSpec(1, 4, 20, 3, True, False),
    ResponseMode.EXECUTIVE: ModeSpec(1, 3, 16, 2, True, False),
    ResponseMode.TECHNICAL: ModeSpec(1, 5, 25, 3, True, True),
}

#: Auto-selected per question class unless the user pins a mode (FR-COP-003).
MODE_BY_CLASS: dict[str, str] = {
    "question": ResponseMode.BALANCED,
    "follow_up": ResponseMode.CONCISE,
    "multi_part_question": ResponseMode.BALANCED,
    "technical_prompt": ResponseMode.TECHNICAL,
    "coding_prompt": ResponseMode.TECHNICAL,
    "logistics": ResponseMode.CONCISE,
}

BEHAVIORAL_HINTS = ("tell me about a time", "describe a situation", "give me an example")


def select_mode(utterance_class: str, question: str, pinned: str | None = None) -> str:
    if pinned:
        return pinned
    lowered = question.lower()
    if any(hint in lowered for hint in BEHAVIORAL_HINTS):
        return ResponseMode.STAR
    return MODE_BY_CLASS.get(utterance_class, ResponseMode.BALANCED)


ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["answer_direction", "key_points"],
    "properties": {
        "answer_direction": {"type": "string"},
        "key_points": {
            "type": "array",
            "minItems": 2,
            "items": {
                "type": "object",
                "required": ["text", "claim_type"],
                "properties": {
                    "text": {"type": "string"},
                    "claim_type": {
                        "type": "string",
                        "enum": ["candidate_fact", "guidance", "general_knowledge"],
                    },
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "structure": {"type": "string"},
        "gaps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"need": {"type": "string"}, "ask_user": {"type": "string"}},
            },
        },
    },
}


@dataclass(slots=True)
class KeyPoint:
    text: str
    claim_type: str
    evidence_ids: list[str] = field(default_factory=list)
    downgraded_from: str | None = None
    downgrade_reason: str | None = None


@dataclass(slots=True)
class Answer:
    answer_direction: str
    key_points: list[KeyPoint]
    structure: str | None = None
    gaps: list[dict[str, str]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "answer_direction": self.answer_direction,
            "key_points": [
                {
                    "text": p.text,
                    "claim_type": p.claim_type,
                    "evidence_ids": p.evidence_ids,
                    **(
                        {"downgraded_from": p.downgraded_from, "reason": p.downgrade_reason}
                        if p.downgraded_from
                        else {}
                    ),
                }
                for p in self.key_points
            ],
            "structure": self.structure,
            "gaps": self.gaps,
            "evidence": self.evidence,
        }

    @property
    def cited_evidence_ids(self) -> list[str]:
        seen: dict[str, None] = {}
        for point in self.key_points:
            for eid in point.evidence_ids:
                seen.setdefault(eid, None)
        return list(seen)


@dataclass(slots=True)
class GroundingReport:
    checked: int = 0
    downgraded: int = 0
    violations: list[dict[str, str]] = field(default_factory=list)
    validator_version: str = "grounding@1"

    def to_json(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "downgraded": self.downgraded,
            "violations": self.violations,
            "validator_version": self.validator_version,
        }


_NUMERAL = re.compile(r"\d[\d,.]*\s?%?")
_PROPER_NOUN = re.compile(r"\b[A-Z][a-zA-Z]{2,}\b")

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _entities(text: str) -> set[str]:
    """Proper nouns, excluding sentence-initial capitalization.

    A key point almost always opens with a capitalized verb — "Improved",
    "Reduced", "Delivered". Treating those as names would downgrade nearly
    every genuine fact, which is worse than the fabrication it guards against:
    a validator that rejects everything teaches users to ignore it. Position,
    not a word list, is the reliable signal.
    """
    found: set[str] = set()
    for sentence in _SENTENCE_SPLIT.split(text.strip()):
        tokens = sentence.split()
        for index, token in enumerate(tokens):
            if index == 0:
                continue
            match = _PROPER_NOUN.fullmatch(token.strip(".,;:!?()"))
            if match:
                found.add(match.group(0))
    return found


def _numerals(text: str) -> set[str]:
    return {m.strip().rstrip(".") for m in _NUMERAL.findall(text)}


def validate_grounding(
    answer: Answer, evidence: list[dict[str, Any]], *, prompt_id: str = "copilot.answer"
) -> GroundingReport:
    """Downgrade any candidate_fact the evidence does not support (§12.6 step 3).

    Two checks, both cheap: a fact must cite at least one evidence id that
    exists, and every entity and numeral it states must appear in that
    evidence's text. A model that invents "Stripe" or "40%" fails the second
    check even when it cites a real id.
    """
    report = GroundingReport()
    by_id = {str(item.get("id")): item for item in evidence}
    corpus = " ".join(f"{item.get('label', '')} {item.get('text', '')}" for item in evidence)
    corpus_entities = _entities(corpus)
    corpus_numerals = _numerals(corpus)

    for point in answer.key_points:
        if point.claim_type != ClaimType.CANDIDATE_FACT:
            continue
        report.checked += 1

        cited = [eid for eid in point.evidence_ids if eid in by_id]
        if not cited:
            _downgrade(point, report, "no evidence cited", prompt_id)
            continue

        unsupported_entities = _entities(point.text) - corpus_entities
        unsupported_numerals = _numerals(point.text) - corpus_numerals

        if unsupported_entities:
            _downgrade(
                point,
                report,
                f"names not present in evidence: {', '.join(sorted(unsupported_entities))}",
                prompt_id,
            )
        elif unsupported_numerals:
            _downgrade(
                point,
                report,
                f"figures not present in evidence: {', '.join(sorted(unsupported_numerals))}",
                prompt_id,
            )
        else:
            point.evidence_ids = cited

    return report


def _downgrade(point: KeyPoint, report: GroundingReport, reason: str, prompt_id: str) -> None:
    point.downgraded_from = point.claim_type
    point.downgrade_reason = reason
    point.claim_type = ClaimType.GUIDANCE
    point.evidence_ids = []
    report.downgraded += 1
    report.violations.append({"text": point.text[:160], "reason": reason})
    grounding_violations.labels(prompt_id=prompt_id).inc()


def enforce_mode(answer: Answer, mode: str) -> Answer:
    """Apply the mode's caps server-side (FR-COP-002).

    Truncation is at a word boundary — a key point cut mid-word reads as a bug
    to the user in the middle of an interview.
    """
    spec = MODES.get(mode, MODES[ResponseMode.BALANCED])

    if spec.direction_sentences == 0:
        answer.answer_direction = ""
    else:
        sentences = re.split(r"(?<=[.!?])\s+", answer.answer_direction.strip())
        answer.answer_direction = " ".join(sentences[: spec.direction_sentences]).strip()

    trimmed: list[KeyPoint] = []
    for point in answer.key_points[: spec.max_points]:
        words = point.text.split()
        if len(words) > spec.max_words_per_point:
            point.text = " ".join(words[: spec.max_words_per_point]).rstrip(",;:") + "…"
        trimmed.append(point)
    answer.key_points = trimmed

    if not spec.include_structure:
        answer.structure = None
    answer.evidence = answer.evidence[: spec.max_evidence]
    return answer


def parse_answer(payload: dict[str, Any], evidence: list[dict[str, Any]]) -> Answer:
    by_id = {str(item.get("id")): item for item in evidence}
    points: list[KeyPoint] = []

    for raw in payload.get("key_points", []):
        if not isinstance(raw, dict):
            continue
        points.append(
            KeyPoint(
                text=str(raw.get("text", "")).strip(),
                claim_type=str(raw.get("claim_type", ClaimType.GUIDANCE)),
                evidence_ids=[str(e) for e in raw.get("evidence_ids", []) if str(e) in by_id],
            )
        )

    return Answer(
        answer_direction=str(payload.get("answer_direction", "")).strip(),
        key_points=points,
        structure=payload.get("structure"),
        gaps=[g for g in payload.get("gaps", []) if isinstance(g, dict)],
        evidence=[
            {"id": item.get("id"), "label": item.get("label"), "type": item.get("type")}
            for item in evidence
        ],
    )


def no_evidence_answer(question: str, mode: str) -> Answer:
    """What to return when nothing approved matches (AC-COP-002).

    Never a plausible-sounding anecdote: a framework plus an explicit ask, with
    zero candidate_fact claims.
    """
    return Answer(
        answer_direction=(
            "No confirmed experience matches this yet — answer from memory and keep it specific."
        ),
        key_points=[
            KeyPoint("Name the situation and your role in one sentence", ClaimType.GUIDANCE),
            KeyPoint("Say what you personally did, not what the team did", ClaimType.GUIDANCE),
            KeyPoint("Finish with the outcome, with a number if you have one", ClaimType.GUIDANCE),
        ],
        structure="STAR" if mode == ResponseMode.STAR else None,
        gaps=[
            {
                "need": "approved experience for this topic",
                "ask_user": f"Which of your roles best fits: {question[:120]}?",
            }
        ],
    )
