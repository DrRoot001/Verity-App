"""Mock interviewer state machine (PRD §13.2).

The conversation is driven by an explicit state machine rather than by letting
the model free-run, because the PRD's guarantees are structural: one question
per turn (FR-MOCK-001), a bounded probe budget (FR-MOCK-002), no repeats
(FR-MOCK-004), and a graceful wrap-up inside the time budget (FR-MOCK-005). A
model asked politely to respect those will eventually not.

The probe decision is a rubric evaluation, not a vibe: an answer is probed when
it measurably lacks a result or ownership signal.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from verity.modules.sessions.models import InterviewerPersona
from verity.platform.logging import get_logger

log = get_logger("sessions.interviewer")


class TurnIntent(StrEnum):
    QUESTION = "question"
    PROBE = "probe"
    REDIRECT = "redirect"
    WRAPUP = "wrapup"


@dataclass(frozen=True, slots=True)
class PersonaProfile:
    """Persona as parameters, so behaviour is predictable across models."""

    probe_budget: int
    probe_threshold: float
    warmth: str
    wrapup_seconds: int


PERSONAS: dict[str, PersonaProfile] = {
    InterviewerPersona.NEUTRAL_EVALUATOR: PersonaProfile(2, 0.6, "neutral", 90),
    InterviewerPersona.FRIENDLY_PEER: PersonaProfile(1, 0.5, "warm", 90),
    # Probes hard and wraps early — the pressure is the point.
    InterviewerPersona.TIME_PRESSURED_MANAGER: PersonaProfile(1, 0.7, "brisk", 150),
    InterviewerPersona.SKEPTICAL_EXPERT: PersonaProfile(3, 0.75, "probing", 90),
    InterviewerPersona.EXECUTIVE_SPONSOR: PersonaProfile(1, 0.6, "outcome-focused", 120),
    InterviewerPersona.STRUCTURED_PANELIST: PersonaProfile(2, 0.6, "methodical", 90),
}

#: Cosine-equivalent similarity above which a question counts as already asked.
REPEAT_SIMILARITY_THRESHOLD = 0.82

#: Rolling rubric score that moves difficulty (PRD FR-MOCK-003).
DIFFICULTY_UP_THRESHOLD = 0.75
DIFFICULTY_DOWN_THRESHOLD = 0.40


@dataclass(slots=True)
class InterviewerState:
    asked: list[str] = field(default_factory=list)
    probes_used_this_question: int = 0
    difficulty: int = 3
    recent_scores: list[float] = field(default_factory=list)
    topics_covered: list[str] = field(default_factory=list)
    question_count: int = 0

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> InterviewerState:
        return cls(
            asked=list(payload.get("asked", [])),
            probes_used_this_question=int(payload.get("probes_used_this_question", 0)),
            difficulty=int(payload.get("difficulty", 3)),
            recent_scores=[float(s) for s in payload.get("recent_scores", [])],
            topics_covered=list(payload.get("topics_covered", [])),
            question_count=int(payload.get("question_count", 0)),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "asked": self.asked,
            "probes_used_this_question": self.probes_used_this_question,
            "difficulty": self.difficulty,
            "recent_scores": self.recent_scores[-6:],
            "topics_covered": self.topics_covered,
            "question_count": self.question_count,
        }


@dataclass(frozen=True, slots=True)
class TurnDecision:
    intent: TurnIntent
    reason: str
    difficulty: int


def is_repeat(candidate: str, asked: list[str]) -> bool:
    """FR-MOCK-004: never ask the same thing twice.

    Sequence similarity stands in for the embedding comparison the PRD
    specifies; it is cheap, deterministic, and catches the rephrasings that
    actually occur in a 30-minute interview.
    """
    normalized = candidate.strip().lower()
    return any(
        difflib.SequenceMatcher(None, normalized, prior.strip().lower()).ratio()
        >= REPEAT_SIMILARITY_THRESHOLD
        for prior in asked
    )


def answer_quality(scores: dict[str, Any]) -> float:
    """Normalize rubric dimensions to 0-1 for probe and difficulty decisions."""
    dimensions = [
        scores.get("relevance"),
        scores.get("structure"),
        scores.get("specificity"),
        scores.get("ownership_signal"),
    ]
    present = [float(d) for d in dimensions if isinstance(d, (int, float))]
    return round(sum(present) / (len(present) * 100), 4) if present else 0.5


def decide_next_turn(
    *,
    state: InterviewerState,
    persona: str,
    last_scores: dict[str, Any] | None,
    elapsed_seconds: int,
    planned_seconds: int,
    max_questions: int,
) -> TurnDecision:
    profile = PERSONAS.get(persona, PERSONAS[InterviewerPersona.NEUTRAL_EVALUATOR])

    if elapsed_seconds >= planned_seconds - profile.wrapup_seconds:
        return TurnDecision(TurnIntent.WRAPUP, "time budget nearly exhausted", state.difficulty)
    if state.question_count >= max_questions:
        return TurnDecision(TurnIntent.WRAPUP, "question budget reached", state.difficulty)

    if last_scores is None:
        return TurnDecision(TurnIntent.QUESTION, "opening question", state.difficulty)

    quality = answer_quality(last_scores)
    difficulty = _adjust_difficulty(state, quality)

    if quality < 0.25:
        return TurnDecision(TurnIntent.REDIRECT, "answer did not address the question", difficulty)

    incomplete = quality < profile.probe_threshold
    if incomplete and state.probes_used_this_question < profile.probe_budget:
        missing = ", ".join(last_scores.get("missing", [])) or "a concrete result"
        return TurnDecision(TurnIntent.PROBE, f"answer lacked {missing}", difficulty)

    return TurnDecision(TurnIntent.QUESTION, "answer was sufficient", difficulty)


def _adjust_difficulty(state: InterviewerState, quality: float) -> int:
    """PRD FR-MOCK-003: two strong answers raise it, one weak answer lowers it."""
    recent = [*state.recent_scores, quality][-2:]
    if len(recent) == 2 and all(s > DIFFICULTY_UP_THRESHOLD for s in recent):
        return min(5, state.difficulty + 1)
    if quality < DIFFICULTY_DOWN_THRESHOLD:
        return max(1, state.difficulty - 1)
    return state.difficulty


def apply_turn(state: InterviewerState, decision: TurnDecision, utterance: str) -> InterviewerState:
    state.difficulty = decision.difficulty
    if decision.intent is TurnIntent.PROBE:
        state.probes_used_this_question += 1
    else:
        state.probes_used_this_question = 0
        state.question_count += 1
    state.asked.append(utterance)
    return state


def record_answer_score(state: InterviewerState, scores: dict[str, Any]) -> InterviewerState:
    state.recent_scores = [*state.recent_scores, answer_quality(scores)][-6:]
    return state
