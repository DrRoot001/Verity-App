"""Delivery metrics from the transcript (PRD §13.3).

Every number here is *measured* from what was said — words per minute, filler
rate, answer length, STAR completeness. FR-MOCK-021 forbids inferring
confidence, emotion or personality, so none of that is computed: a transcript
does not contain it, and a plausible-looking score would be invented.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

_WORD_RE = re.compile(r"[A-Za-z0-9']+")

#: Multi-word fillers are checked before single words so "you know" is not
#: counted as two separate hits.
_FILLER_PHRASES = ("you know", "i mean", "sort of", "kind of", "or something")
_FILLER_WORDS = frozenset(
    {"um", "uh", "erm", "like", "basically", "actually", "literally", "right", "so"}
)

#: STAR components, detected by the language people actually use aloud.
_STAR_SIGNALS: dict[str, tuple[str, ...]] = {
    "situation": ("we were", "at the time", "the context", "back when", "our team was", "when i"),
    "task": ("i was asked", "my job", "i needed to", "the goal", "i was responsible", "my role"),
    "action": ("i ", "we ", "so i", "then i", "i decided", "i built", "i led"),
    "result": (
        "as a result",
        "which meant",
        "ended up",
        "we saw",
        "reduced",
        "increased",
        "improved",
        "shipped",
        "delivered",
        "the outcome",
    ),
}


@dataclass(slots=True)
class DeliveryMetrics:
    word_count: int
    duration_seconds: int
    words_per_minute: float | None
    filler_count: int
    filler_rate_per_100: float
    star_present: dict[str, bool]
    star_completeness: float

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


def analyze(answer: str, duration_seconds: int | None) -> DeliveryMetrics:
    words = _WORD_RE.findall(answer)
    word_count = len(words)
    lowered = answer.lower()

    filler_count = 0
    for phrase in _FILLER_PHRASES:
        filler_count += lowered.count(phrase)
        lowered = lowered.replace(phrase, " ")
    filler_count += sum(1 for w in _WORD_RE.findall(lowered) if w in _FILLER_WORDS)

    # Below ~5 seconds the rate is dominated by measurement noise, so report
    # nothing rather than a wild number.
    wpm = (
        round(word_count / (duration_seconds / 60), 1)
        if duration_seconds and duration_seconds >= 5
        else None
    )

    star = {
        component: any(signal in answer.lower() for signal in signals)
        for component, signals in _STAR_SIGNALS.items()
    }

    return DeliveryMetrics(
        word_count=word_count,
        duration_seconds=duration_seconds or 0,
        words_per_minute=wpm,
        filler_count=filler_count,
        filler_rate_per_100=round(filler_count / word_count * 100, 2) if word_count else 0.0,
        star_present=star,
        star_completeness=round(sum(star.values()) / len(star), 2),
    )


def aggregate(metrics: list[DeliveryMetrics]) -> dict[str, Any]:
    if not metrics:
        return {}

    rated = [m for m in metrics if m.words_per_minute is not None]
    total_words = sum(m.word_count for m in metrics)

    return {
        "answers": len(metrics),
        "average_words_per_minute": (
            round(sum(m.words_per_minute or 0 for m in rated) / len(rated), 1) if rated else None
        ),
        "average_answer_words": round(total_words / len(metrics), 1),
        "filler_rate_per_100": (
            round(sum(m.filler_count for m in metrics) / total_words * 100, 2)
            if total_words
            else 0.0
        ),
        "star_completeness": round(sum(m.star_completeness for m in metrics) / len(metrics), 2),
        "answers_missing_result": sum(1 for m in metrics if not m.star_present.get("result")),
    }
