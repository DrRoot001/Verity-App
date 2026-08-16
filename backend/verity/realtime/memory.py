"""Conversation memory (PRD §14.6).

Without this the copilot contradicts the candidate: it recommends a story they
already told, or asserts something incompatible with an answer they gave ten
minutes ago. The memory is deliberately small and structured — a verbatim
window plus a rolling summary plus a few indexes — because it has to fit inside
the realtime token budget alongside everything else (§20.4).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

#: Roughly 90 seconds of two-way speech at conversational pace.
VERBATIM_TURN_LIMIT = 12
#: Compact once the window overflows, so a 75-minute session stays bounded.
SUMMARY_EVERY_N_TURNS = 5
MAX_SUMMARY_CHARS = 1600


@dataclass(slots=True)
class Turn:
    role: str
    content: str
    at_ms: int


@dataclass(slots=True)
class ConversationMemory:
    verbatim: list[Turn] = field(default_factory=list)
    rolling_summary: str = ""
    questions_asked: list[dict[str, Any]] = field(default_factory=list)
    topics_covered: list[str] = field(default_factory=list)
    #: Story ids already used, so the copilot never recommends one twice
    #: (FR-COP-010).
    stories_used: list[str] = field(default_factory=list)
    #: Claims the candidate has made, so guidance cannot contradict them.
    claims_made: list[dict[str, Any]] = field(default_factory=list)
    open_threads: list[dict[str, Any]] = field(default_factory=list)
    speakers: dict[str, dict[str, Any]] = field(default_factory=dict)
    turns_since_summary: int = 0

    # ── Mutation ─────────────────────────────────────────────────────

    def add_turn(self, role: str, content: str, at_ms: int) -> None:
        self.verbatim.append(Turn(role=role, content=content, at_ms=at_ms))
        self.turns_since_summary += 1
        if len(self.verbatim) > VERBATIM_TURN_LIMIT:
            # Compact rather than drop: the oldest turn folds into the summary
            # so nothing is silently forgotten.
            evicted = self.verbatim.pop(0)
            self._fold_into_summary(evicted)

    def record_question(
        self, question_id: str, content: str, category: str | None, at_ms: int
    ) -> None:
        self.questions_asked.append(
            {"id": question_id, "content": content, "category": category, "at_ms": at_ms}
        )
        if category and category not in self.topics_covered:
            self.topics_covered.append(category)

    def record_story_use(self, story_id: str) -> None:
        if story_id not in self.stories_used:
            self.stories_used.append(story_id)

    def record_claim(self, text: str, evidence_id: str | None, at_ms: int) -> None:
        self.claims_made.append({"text": text[:300], "evidence_id": evidence_id, "at_ms": at_ms})

    def open_thread(self, topic: str, raised_by: str) -> None:
        if not any(t["topic"] == topic for t in self.open_threads):
            self.open_threads.append({"topic": topic, "raised_by": raised_by, "status": "open"})

    def close_thread(self, topic: str) -> None:
        for thread in self.open_threads:
            if thread["topic"] == topic:
                thread["status"] = "closed"

    def label_speaker(self, speaker_id: str, label: str) -> None:
        """Labels apply retroactively (FR-RT-021)."""
        self.speakers.setdefault(speaker_id, {"topics": []})["label"] = label

    def _fold_into_summary(self, turn: Turn) -> None:
        fragment = f"{turn.role}: {turn.content.strip()[:180]}"
        combined = f"{self.rolling_summary} | {fragment}" if self.rolling_summary else fragment
        if len(combined) > MAX_SUMMARY_CHARS:
            # Keep the tail: recent context matters more than the opening.
            combined = combined[-MAX_SUMMARY_CHARS:]
        self.rolling_summary = combined

    @property
    def needs_summary_refresh(self) -> bool:
        return self.turns_since_summary >= SUMMARY_EVERY_N_TURNS

    def mark_summarized(self, summary: str) -> None:
        self.rolling_summary = summary[:MAX_SUMMARY_CHARS]
        self.turns_since_summary = 0

    # ── Reads used by the orchestrator ───────────────────────────────

    def recent_turns(self, limit: int = 3) -> list[Turn]:
        return self.verbatim[-limit:]

    def last_question(self) -> str | None:
        return self.questions_asked[-1]["content"] if self.questions_asked else None

    def has_asked_similar(self, content: str) -> bool:
        target = set(content.lower().split())
        for question in self.questions_asked:
            prior = set(str(question["content"]).lower().split())
            if prior and len(target & prior) / len(prior) > 0.7:
                return True
        return False

    # ── Persistence ──────────────────────────────────────────────────

    def to_json(self) -> dict[str, Any]:
        return {
            "verbatim": [
                {"role": t.role, "content": t.content, "at_ms": t.at_ms} for t in self.verbatim
            ],
            "rolling_summary": self.rolling_summary,
            "questions_asked": self.questions_asked,
            "topics_covered": self.topics_covered,
            "stories_used": self.stories_used,
            "claims_made": self.claims_made[-40:],
            "open_threads": self.open_threads,
            "speakers": self.speakers,
            "turns_since_summary": self.turns_since_summary,
            "updated_at": dt.datetime.now(dt.UTC).isoformat(),
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> ConversationMemory:
        return cls(
            verbatim=[
                Turn(role=t["role"], content=t["content"], at_ms=int(t.get("at_ms", 0)))
                for t in payload.get("verbatim", [])
            ],
            rolling_summary=str(payload.get("rolling_summary", "")),
            questions_asked=list(payload.get("questions_asked", [])),
            topics_covered=list(payload.get("topics_covered", [])),
            stories_used=list(payload.get("stories_used", [])),
            claims_made=list(payload.get("claims_made", [])),
            open_threads=list(payload.get("open_threads", [])),
            speakers=dict(payload.get("speakers", {})),
            turns_since_summary=int(payload.get("turns_since_summary", 0)),
        )
