"""Question boundary and intent detection (PRD §14.3).

The hardest requirement in the product is not "detect questions" — it is *don't
generate on everything*. An assistant that fires on every sentence is worse
than useless during a live interview: it burns budget, buries the useful card,
and trains the candidate to ignore the screen.

So detection fuses five weighted signals into one confidence, and generation is
gated on that confidence plus an utterance class (FR-RT-002). Below the gate the
UI offers a dim "possible question" affordance instead of spending a model call
(FR-RT-006).

The weights are the PRD's, kept as named constants so tuning is a visible act
and the eval harness can attribute a regression to a specific signal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from verity.platform.config import settings

# PRD §14.3 signal weights.
WEIGHT_TRAILING_SILENCE = 0.30
WEIGHT_TERMINAL_PROSODY = 0.15
WEIGHT_INTERROGATIVE = 0.25
WEIGHT_TURN_TAKING = 0.15
WEIGHT_SEMANTIC_COMPLETENESS = 0.15

#: Generation gate (FR-RT-002). Detection surfaces below this; generation does not.
FINALIZE_THRESHOLD = 0.75
DETECT_THRESHOLD = 0.55

#: Silence that reads as "your turn" without cutting off a thinking pause.
SILENCE_MS_FOR_BOUNDARY = 600
#: Continued speech inside this window extends the same question. A mid-sentence
#: thinking pause ("tell me about a time when you… had to ship under pressure")
#: is common and routinely runs past a second; below that the two halves become
#: two questions and the candidate is answered on the first half alone.
ACCUMULATION_WINDOW_MS = settings.question_accumulation_window_ms

#: Above this rate the speaker is rattling through; shorten the window so we do
#: not merge two questions into one (PRD §35 edge case 16).
RAPID_SPEECH_WPM = 190
RAPID_ACCUMULATION_WINDOW_MS = ACCUMULATION_WINDOW_MS * 2 // 3


class UtteranceClass(StrEnum):
    QUESTION = "question"
    FOLLOW_UP = "follow_up"
    MULTI_PART_QUESTION = "multi_part_question"
    RHETORICAL = "rhetorical"
    SMALL_TALK = "small_talk"
    STATEMENT = "statement"
    TECHNICAL_PROMPT = "technical_prompt"
    CODING_PROMPT = "coding_prompt"
    LOGISTICS = "logistics"
    INTERRUPTION = "interruption"


#: Classes worth spending a generation on (FR-RT-002).
GENERATIVE_CLASSES: frozenset[str] = frozenset(
    {
        UtteranceClass.QUESTION,
        UtteranceClass.FOLLOW_UP,
        UtteranceClass.MULTI_PART_QUESTION,
        UtteranceClass.TECHNICAL_PROMPT,
        UtteranceClass.CODING_PROMPT,
    }
)

_INTERROGATIVE_OPENERS = (
    "what",
    "how",
    "why",
    "when",
    "where",
    "who",
    "which",
    "can you",
    "could you",
    "would you",
    "will you",
    "do you",
    "did you",
    "have you",
    "are you",
    "is there",
    "tell me",
    "walk me",
    "describe",
    "explain",
    "give me",
    "talk me",
    "share",
)

_IMPERATIVE_REQUESTS = (
    "tell me about",
    "walk me through",
    "describe a time",
    "give me an example",
    "explain how",
    "talk about",
    "take me through",
)

_SMALL_TALK = (
    "how are you",
    "nice to meet",
    "good morning",
    "good afternoon",
    "thanks for",
    "can you hear me",
    "let me know if",
    "hope you",
    "how's your day",
)

_LOGISTICS = (
    "next steps",
    "get back to you",
    "recruiter will",
    "schedule",
    "salary",
    "compensation",
    "notice period",
    "start date",
    "any questions for me",
)

_CODING_SIGNALS = (
    "write a function",
    "implement",
    "leetcode",
    "time complexity",
    "big o",
    "array",
    "linked list",
    "binary tree",
    "algorithm",
    "code this",
    "pseudocode",
)

_TECHNICAL_SIGNALS = (
    "design a",
    "architecture",
    "how would you scale",
    "trade-off",
    "tradeoff",
    "database",
    "latency",
    "throughput",
    "system design",
    "distributed",
)

_RHETORICAL = ("right?", "you know?", "make sense?", "isn't it?", "yeah?", "okay?")

_FOLLOW_UP_MARKERS = (
    "and what about",
    "you mentioned",
    "going back to",
    "on that",
    "following up",
    "can you expand",
    "say more",
    "why that",
    "what else",
)

#: A question mark inside the text, not just at the end, suggests two asks.
_MULTI_PART_SPLIT = re.compile(r"(?<=[?])\s+(?=[A-Z])")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


@dataclass(slots=True)
class Signals:
    trailing_silence: float = 0.0
    terminal_prosody: float = 0.0
    interrogative: float = 0.0
    turn_taking: float = 0.0
    semantic_completeness: float = 0.0

    def to_json(self) -> dict[str, float]:
        return {
            "trailing_silence": round(self.trailing_silence, 3),
            "terminal_prosody": round(self.terminal_prosody, 3),
            "interrogative": round(self.interrogative, 3),
            "turn_taking": round(self.turn_taking, 3),
            "semantic_completeness": round(self.semantic_completeness, 3),
        }


@dataclass(slots=True)
class Detection:
    content: str
    confidence: float
    signals: Signals
    utterance_class: str
    sub_parts: list[str] = field(default_factory=list)
    #: True when confidence and class both clear the bar for a model call.
    should_generate: bool = False
    reason: str = ""

    @property
    def is_finalized(self) -> bool:
        return self.confidence >= FINALIZE_THRESHOLD

    @property
    def is_detected(self) -> bool:
        return self.confidence >= DETECT_THRESHOLD


def classify(content: str, *, previous_question: str | None = None) -> str:
    """Rule-based utterance classification.

    Deliberately not a model call: this runs on every interviewer utterance in a
    live session, and the PRD's budget for it is 120 ms (§21.3). Rules are
    instant, free and auditable; a small model would be none of those at this
    call rate.
    """
    text = content.strip().lower()
    if not text:
        return UtteranceClass.STATEMENT

    if any(signal in text for signal in _CODING_SIGNALS):
        return UtteranceClass.CODING_PROMPT
    if any(signal in text for signal in _LOGISTICS):
        return UtteranceClass.LOGISTICS
    if any(signal in text for signal in _SMALL_TALK):
        return UtteranceClass.SMALL_TALK

    # A trailing tag like "right?" is checked before the generic question test,
    # since it ends in a question mark but expects no real answer.
    if any(text.endswith(tag) for tag in _RHETORICAL) and len(text.split()) < 12:
        return UtteranceClass.RHETORICAL

    parts = split_multi_part(content)
    if len(parts) > 1:
        return UtteranceClass.MULTI_PART_QUESTION

    if any(marker in text for marker in _FOLLOW_UP_MARKERS) and previous_question:
        return UtteranceClass.FOLLOW_UP

    if any(signal in text for signal in _TECHNICAL_SIGNALS):
        return UtteranceClass.TECHNICAL_PROMPT

    if _looks_interrogative(text):
        return UtteranceClass.QUESTION

    return UtteranceClass.STATEMENT


def split_multi_part(content: str) -> list[str]:
    """Decompose a multi-part ask (FR-RT-003)."""
    candidates = [p.strip() for p in _MULTI_PART_SPLIT.split(content.strip()) if p.strip()]
    if len(candidates) > 1:
        return candidates

    # "Tell me about X, and how did you handle Y?" is two asks in one sentence.
    if content.count("?") <= 1 and " and " in content.lower():
        halves = re.split(
            r",?\s+and\s+(?=how|what|why|when|who|which|can|could|did|do)\b",
            content.strip(),
            flags=re.IGNORECASE,
        )
        cleaned = [h.strip() for h in halves if len(h.strip().split()) >= 3]
        if len(cleaned) > 1:
            return cleaned
    return [content.strip()]


def _looks_interrogative(text: str) -> bool:
    if text.endswith("?"):
        return True
    if any(text.startswith(opener) for opener in _INTERROGATIVE_OPENERS):
        return True
    return any(request in text for request in _IMPERATIVE_REQUESTS)


def score_signals(
    *,
    content: str,
    silence_ms: int,
    ends_with_terminal_punctuation: bool,
    candidate_channel_active: bool,
    speech_rate_wpm: float | None = None,
    endpointed: bool = False,
) -> Signals:
    signals = Signals()

    # An STT provider emitting a *final* transcript has itself decided the
    # utterance ended, which is the same evidence trailing silence gives. Text
    # mode carries no audio, so without this every text session would
    # systematically under-detect and rarely finalize.
    signals.trailing_silence = 1.0 if endpointed else min(1.0, silence_ms / SILENCE_MS_FOR_BOUNDARY)
    signals.terminal_prosody = 1.0 if ends_with_terminal_punctuation else 0.0
    signals.interrogative = 1.0 if _looks_interrogative(content.strip().lower()) else 0.0
    # The candidate starting to speak is strong evidence the ask landed.
    signals.turn_taking = 1.0 if candidate_channel_active else 0.0
    signals.semantic_completeness = _semantic_completeness(content, speech_rate_wpm)
    return signals


def _semantic_completeness(content: str, speech_rate_wpm: float | None) -> float:
    """Is this a self-contained request, or a fragment mid-thought?"""
    words = content.strip().split()
    if len(words) < 3:
        return 0.0

    score = min(1.0, len(words) / 12)

    # Trailing conjunctions and fillers mean the speaker has not finished.
    # Terminal punctuation is stripped before the check: an STT flush at a
    # thinking pause comes back as "…when you had to." — the model guessed a
    # full stop mid-sentence, and trusting it scores the fragment as a complete
    # question and answers half of what was asked.
    if words[-1].lower().rstrip(",.!?;:") in {
        "and",
        "or",
        "but",
        "so",
        "because",
        "with",
        "the",
        "a",
        "to",
        "of",
        "um",
        "uh",
    }:
        score *= 0.25

    if speech_rate_wpm and speech_rate_wpm > RAPID_SPEECH_WPM:
        # Rapid speech means a pause is less meaningful, so lean harder on text.
        score *= 0.85
    return round(min(1.0, score), 3)


def fuse(signals: Signals) -> float:
    return round(
        min(
            1.0,
            WEIGHT_TRAILING_SILENCE * signals.trailing_silence
            + WEIGHT_TERMINAL_PROSODY * signals.terminal_prosody
            + WEIGHT_INTERROGATIVE * signals.interrogative
            + WEIGHT_TURN_TAKING * signals.turn_taking
            + WEIGHT_SEMANTIC_COMPLETENESS * signals.semantic_completeness,
        ),
        4,
    )


def detect(
    *,
    content: str,
    silence_ms: int = 0,
    candidate_channel_active: bool = False,
    speech_rate_wpm: float | None = None,
    previous_question: str | None = None,
    endpointed: bool = False,
) -> Detection:
    stripped = content.strip()
    signals = score_signals(
        content=stripped,
        silence_ms=silence_ms,
        ends_with_terminal_punctuation=stripped.endswith((".", "?", "!")),
        candidate_channel_active=candidate_channel_active,
        speech_rate_wpm=speech_rate_wpm,
        endpointed=endpointed,
    )
    confidence = fuse(signals)
    utterance_class = classify(stripped, previous_question=previous_question)
    sub_parts = (
        split_multi_part(stripped) if utterance_class == UtteranceClass.MULTI_PART_QUESTION else []
    )

    generative = utterance_class in GENERATIVE_CLASSES
    should_generate = generative and confidence >= FINALIZE_THRESHOLD

    if not generative:
        reason = f"class '{utterance_class}' is not worth a generation"
    elif confidence < FINALIZE_THRESHOLD:
        reason = f"confidence {confidence} below gate {FINALIZE_THRESHOLD}"
    else:
        reason = "finalized"

    return Detection(
        content=stripped,
        confidence=confidence,
        signals=signals,
        utterance_class=utterance_class,
        sub_parts=sub_parts,
        should_generate=should_generate,
        reason=reason,
    )


def accumulation_window_ms(speech_rate_wpm: float | None) -> int:
    if speech_rate_wpm and speech_rate_wpm > RAPID_SPEECH_WPM:
        return RAPID_ACCUMULATION_WINDOW_MS
    return ACCUMULATION_WINDOW_MS


def is_superseded_by(previous: str, incoming: str) -> bool:
    """A new finalized question replaces a very recent, distinct one (§14.2)."""
    if not previous:
        return False
    previous_norm = previous.strip().lower()
    incoming_norm = incoming.strip().lower()
    if incoming_norm.startswith(previous_norm):
        return False  # continuation, not replacement
    overlap = set(previous_norm.split()) & set(incoming_norm.split())
    return len(overlap) < max(2, len(previous_norm.split()) // 3)
