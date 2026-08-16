"""Realtime context assembly (PRD §20.4, §16).

Two rules make this the difference between a product that works live and one
that times out:

1. **The full resume and the full JD never enter a realtime prompt**
   (FR-AI-010). Only structured summaries and retrieved records do. The
   ContextBundle is already shaped for this; the assembler's job is to stay
   inside the budget while spending it on the most useful things.
2. **The priority ladder is enforced, not suggested.** When the budget is
   exhausted, content is dropped in reverse priority order and the omission is
   recorded on the suggestion, so a thin answer is explainable after the fact.

The static prefix is assembled first and kept byte-identical across a session
so provider-side prompt caching can hit it (§20.4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from verity.realtime.memory import ConversationMemory

#: Realtime budget for the whole prompt, excluding reserved output (§20.4).
DEFAULT_TOKEN_BUDGET = 12_000
RESERVED_OUTPUT_TOKENS = 1_900

#: Characters per token. Deliberately conservative: overestimating tokens costs
#: a little context, underestimating costs a truncation error mid-interview.
CHARS_PER_TOKEN = 3.6


class Priority(IntEnum):
    """PRD §20.4 ladder. Lower value is dropped last."""

    CURRENT_QUESTION = 0
    RECENT_CONVERSATION = 1
    JD_ESSENTIALS = 2
    APPROVED_FACTS = 3
    STORY_MATCHES = 4
    RESUME_SNIPPETS = 5
    COMPANY_PREP = 6
    OLDER_CONVERSATION = 7


#: Per-priority allocations from PRD §20.4.
ALLOCATIONS: dict[Priority, int] = {
    Priority.CURRENT_QUESTION: 200,
    Priority.RECENT_CONVERSATION: 900,
    Priority.JD_ESSENTIALS: 700,
    Priority.APPROVED_FACTS: 2_500,
    Priority.STORY_MATCHES: 1_800,
    Priority.RESUME_SNIPPETS: 800,
    Priority.COMPANY_PREP: 600,
    Priority.OLDER_CONVERSATION: 400,
}


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / CHARS_PER_TOKEN))


@dataclass(slots=True)
class ContextSection:
    priority: Priority
    label: str
    content: str
    tokens: int = 0

    def __post_init__(self) -> None:
        self.tokens = estimate_tokens(self.content)


@dataclass(slots=True)
class AssembledContext:
    sections: list[ContextSection]
    total_tokens: int
    truncated_at: str | None = None
    dropped: list[str] = field(default_factory=list)

    def render(self) -> str:
        return "\n\n".join(f"## {s.label}\n{s.content}" for s in self.sections if s.content)

    def to_json(self) -> dict[str, Any]:
        return {
            "total_tokens": self.total_tokens,
            "sections": [
                {"priority": int(s.priority), "label": s.label, "tokens": s.tokens}
                for s in self.sections
            ],
            "truncated_at": self.truncated_at,
            "dropped": self.dropped,
        }


def _clip(text: str, token_budget: int) -> str:
    """Trim to budget on a sentence boundary where possible."""
    limit = int(token_budget * CHARS_PER_TOKEN)
    if len(text) <= limit:
        return text
    clipped = text[:limit]
    cut = max(clipped.rfind(". "), clipped.rfind("\n"))
    return clipped[: cut + 1] if cut > limit * 0.6 else clipped


def assemble(
    *,
    question: str,
    bundle_candidate: dict[str, Any],
    bundle_opportunity: dict[str, Any],
    evidence: list[dict[str, Any]],
    memory: ConversationMemory,
    notes: str | None = None,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
) -> AssembledContext:
    """Build the prompt context, highest priority first."""
    candidates: list[ContextSection] = []

    candidates.append(
        ContextSection(Priority.CURRENT_QUESTION, "Current question", question.strip())
    )

    recent = memory.recent_turns(3)
    if recent:
        candidates.append(
            ContextSection(
                Priority.RECENT_CONVERSATION,
                "Recent conversation",
                "\n".join(f"{t.role}: {t.content}" for t in recent),
            )
        )

    must_have = bundle_opportunity.get("must_have", [])[:8]
    themes = [str(t.get("value", "")) for t in bundle_opportunity.get("likely_themes", [])[:4]]
    jd_lines = [
        f"Role: {bundle_opportunity.get('role_title')} at {bundle_opportunity.get('company_name')}",
        f"Seniority: {bundle_opportunity.get('seniority') or 'unstated'}",
    ]
    if must_have:
        jd_lines.append("Key requirements: " + "; ".join(must_have))
    if themes:
        jd_lines.append("Likely themes: " + ", ".join(themes))
    candidates.append(ContextSection(Priority.JD_ESSENTIALS, "Opportunity", "\n".join(jd_lines)))

    # Approved evidence is the highest-value content in the prompt: it is the
    # only thing a candidate_fact may cite (PRD §12.6).
    if evidence:
        lines = []
        for item in evidence:
            label = item.get("label", "")
            detail = item.get("text", "")
            lines.append(f"[{item.get('id')}] ({item.get('type')}) {label}: {detail}".strip())
        candidates.append(
            ContextSection(Priority.APPROVED_FACTS, "Approved candidate evidence", "\n".join(lines))
        )

    stories = bundle_candidate.get("story_index", [])
    unused = [s for s in stories if str(s.get("id")) not in memory.stories_used]
    if unused:
        candidates.append(
            ContextSection(
                Priority.STORY_MATCHES,
                "Approved stories not yet used",
                "\n".join(
                    f"[{s.get('id')}] {s.get('title')} ({', '.join(s.get('categories', []))})"
                    for s in unused[:6]
                ),
            )
        )

    experiences = bundle_candidate.get("experiences", [])
    if experiences:
        candidates.append(
            ContextSection(
                Priority.RESUME_SNIPPETS,
                "Background",
                "\n".join(
                    f"{e.get('title')} at {e.get('company')}"
                    f"{' (current)' if e.get('is_current') else ''}"
                    for e in experiences[:6]
                ),
            )
        )

    if notes:
        candidates.append(ContextSection(Priority.COMPANY_PREP, "Your notes", notes))

    if memory.rolling_summary:
        candidates.append(
            ContextSection(
                Priority.OLDER_CONVERSATION, "Earlier in this interview", memory.rolling_summary
            )
        )

    return _fit(candidates, token_budget - RESERVED_OUTPUT_TOKENS)


def _fit(candidates: list[ContextSection], budget: int) -> AssembledContext:
    ordered = sorted(candidates, key=lambda s: s.priority)
    kept: list[ContextSection] = []
    dropped: list[str] = []
    truncated_at: str | None = None
    used = 0

    for section in ordered:
        allocation = ALLOCATIONS.get(section.priority, 400)
        remaining = budget - used

        if remaining <= 0:
            dropped.append(section.label)
            truncated_at = truncated_at or section.label
            continue

        allowed = min(allocation, remaining)
        if section.tokens > allowed:
            section = ContextSection(
                section.priority, section.label, _clip(section.content, allowed)
            )
            truncated_at = truncated_at or section.label

        kept.append(section)
        used += section.tokens

    return AssembledContext(
        sections=kept, total_tokens=used, truncated_at=truncated_at, dropped=dropped
    )


def static_prefix(bundle_opportunity: dict[str, Any], response_mode: str) -> str:
    """The cache-eligible prefix (§20.4).

    Byte-identical for the life of a session, so provider-side caching can hit
    it on every generation rather than only the first.
    """
    return (
        "You are assisting a candidate during a live interview.\n"
        f"Role: {bundle_opportunity.get('role_title')} at "
        f"{bundle_opportunity.get('company_name')}.\n"
        f"Response mode: {response_mode}.\n"
        "Never invent an employer, project, metric or achievement. Only facts present "
        "in the supplied evidence may be marked as candidate_fact, and each must cite "
        "the evidence id it came from. If evidence is missing, say so and give a "
        "framework or a clarifying question instead."
    )
