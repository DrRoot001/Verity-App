"""Copilot answer engine (PRD §14.4, §32.1).

The latency design is the product. A single call to a strong model would put
first paint behind that model's full time-to-first-token, so two calls run
**concurrently**:

* the *fast lane* — a small model producing only ``answer_direction``, targeting
  400 ms TTFT, which is what the user actually reads first;
* the *main lane* — the stronger model producing the full answer object.

Whichever direction arrives first is shown. If the main answer later differs
materially the card updates once, never in a flicker loop. The user sees a
usable direction at ~0.8 s and the complete card by ~2 s (§14.4), instead of
staring at a spinner until the strong model finishes.

Everything after generation is pure Python — grounding validation and mode caps
add microseconds, not a round trip.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from verity.ai.answers import (
    ANSWER_SCHEMA,
    Answer,
    GroundingReport,
    enforce_mode,
    no_evidence_answer,
    parse_answer,
    select_mode,
    validate_grounding,
)
from verity.ai.gateway import AIGateway, build_gateway
from verity.ai.providers.base import Message, TaskClass
from verity.ai.providers.embeddings import build_embedding_provider
from verity.modules.candidate_graph.retrieval import GraphRetrievalService, RetrievalQuery
from verity.modules.sessions.live_models import AiSuggestion, DetectedQuestion, QuestionLifecycle
from verity.modules.workspace.context import ContextBundle
from verity.platform.errors import AppError
from verity.platform.logging import get_logger
from verity.platform.telemetry import guidance_e2e_latency, stage_span
from verity.realtime.memory import ConversationMemory
from verity.realtime.orchestrator import assemble, static_prefix

log = get_logger("sessions.copilot")

#: §32.1 budgets. Exceeding them degrades rather than blocks.
FAST_LANE_TIMEOUT = 3.0
MAIN_LANE_TIMEOUT = 8.0
RETRIEVAL_TIMEOUT = 1.5

EVIDENCE_LIMIT = 8


@dataclass(slots=True)
class GuidanceResult:
    suggestion: AiSuggestion
    answer: Answer
    grounding: GroundingReport
    latency_ms: dict[str, float] = field(default_factory=dict)
    degraded: bool = False
    degraded_reason: str | None = None


class CopilotEngine:
    def __init__(
        self,
        db: AsyncSession,
        *,
        gateway: AIGateway | None = None,
        on_direction: Callable[[str], None] | None = None,
    ) -> None:
        self._db = db
        self._gateway = gateway or build_gateway()
        self._retrieval = GraphRetrievalService(db, build_embedding_provider())
        #: Called with the fast-lane direction the instant it is ready, so the
        #: transport can put it on screen without waiting for the full answer.
        self._on_direction = on_direction

    async def guide(
        self,
        *,
        question: DetectedQuestion,
        bundle: ContextBundle,
        memory: ConversationMemory,
        response_mode: str | None = None,
        revision_reason: str = "initial",
    ) -> GuidanceResult:
        started = time.perf_counter()
        timings: dict[str, float] = {}

        mode = select_mode(question.category or "question", question.content, response_mode)

        # ── Retrieval ────────────────────────────────────────────────
        retrieval_started = time.perf_counter()
        evidence, degraded_retrieval = await self._retrieve(question, bundle)
        timings["retrieval"] = (time.perf_counter() - retrieval_started) * 1000

        if not evidence:
            # AC-COP-002: no approved match means a framework and an explicit
            # ask, never an invented anecdote.
            answer = enforce_mode(no_evidence_answer(question.content, mode), mode)
            suggestion = await self._persist(
                question, answer, GroundingReport(), mode, revision_reason, timings, None
            )
            timings["e2e"] = (time.perf_counter() - started) * 1000
            guidance_e2e_latency.observe(timings["e2e"] / 1000)
            return GuidanceResult(
                suggestion=suggestion,
                answer=answer,
                grounding=GroundingReport(),
                latency_ms=timings,
            )

        context = assemble(
            question=question.content,
            bundle_candidate=bundle.to_json()["candidate"],
            bundle_opportunity=bundle.to_json()["opportunity"],
            evidence=evidence,
            memory=memory,
            notes=bundle.notes,
        )
        prefix = static_prefix(bundle.to_json()["opportunity"], mode)

        # ── Two lanes, concurrently ──────────────────────────────────
        generation_started = time.perf_counter()
        with stage_span("copilot_generate", mode=mode):
            direction, main, degraded_main = await self._race_lanes(prefix, context.render(), mode)
        timings["generation"] = (time.perf_counter() - generation_started) * 1000

        answer = parse_answer(main, evidence, question=question.content)
        # The fast lane already put its line on screen; keep it rather than
        # swapping the text under the reader, unless the main lane has one too.
        answer.answer_direction = answer.answer_direction or direction

        grounding = validate_grounding(
            answer,
            evidence,
            # The role, the company and the question itself are legitimately
            # speakable without being "evidence" — counting them as fabrications
            # would flag every correct answer.
            extra_corpus=f"{prefix} {question.content}",
        )
        answer = enforce_mode(answer, mode)

        # Never recommend a story the candidate already told (FR-COP-010).
        for eid in answer.cited_evidence_ids:
            memory.record_story_use(eid)

        suggestion = await self._persist(
            question, answer, grounding, mode, revision_reason, timings, evidence
        )

        timings["e2e"] = (time.perf_counter() - started) * 1000
        guidance_e2e_latency.observe(timings["e2e"] / 1000)

        log.info(
            "guidance_generated",
            question_id=str(question.id),
            mode=mode,
            evidence=len(evidence),
            downgraded=grounding.downgraded,
            e2e_ms=round(timings["e2e"], 1),
            degraded=degraded_main or degraded_retrieval,
        )

        return GuidanceResult(
            suggestion=suggestion,
            answer=answer,
            grounding=grounding,
            latency_ms=timings,
            degraded=degraded_main or degraded_retrieval,
            degraded_reason="retrieval" if degraded_retrieval else None,
        )

    # ── Lanes ────────────────────────────────────────────────────────

    async def _race_lanes(
        self, prefix: str, context: str, mode: str
    ) -> tuple[str, dict[str, Any], bool]:
        """Run both lanes concurrently and return (direction, answer, degraded).

        ``on_direction`` fires as soon as the fast lane lands — roughly a second
        before the main answer. In a live interview that gap is the difference
        between reading something while the interviewer is still finishing their
        sentence and staring at a spinner.
        """
        fast = asyncio.create_task(self._fast_lane(prefix, context))
        main = asyncio.create_task(self._main_lane(prefix, context, mode))

        if self._on_direction is not None:
            notify = self._on_direction

            def _publish(task: asyncio.Task[str]) -> None:
                if task.cancelled():
                    return
                if task.exception() is None and task.result().strip():
                    notify(task.result().strip())

            fast.add_done_callback(_publish)

        direction = ""
        degraded = False
        try:
            answer = await asyncio.wait_for(main, timeout=MAIN_LANE_TIMEOUT)
        except (TimeoutError, AppError) as exc:
            # The main lane failed; the fast lane's direction plus the retrieved
            # evidence is still genuinely useful (PRD §20.3 degraded mode).
            degraded = True
            log.warning("main_lane_failed", error=type(exc).__name__)
            answer = {"spoken_answer": "", "answer_direction": "", "key_points": []}

        try:
            direction = await asyncio.wait_for(
                fast, timeout=0.01 if not degraded else FAST_LANE_TIMEOUT
            )
        except (TimeoutError, AppError, asyncio.CancelledError):
            fast.cancel()

        return direction, answer, degraded

    async def _fast_lane(self, prefix: str, context: str) -> str:
        """One or two sentences of direction — the first thing the user reads."""
        result = await self._gateway.generate(
            task_class=TaskClass.ANSWER_DIRECTION,
            messages=[
                Message(role="system", content=prefix),
                Message(
                    role="user",
                    content=(
                        f"{context}\n\nWrite the first sentence the candidate should say out "
                        "loud, in their own voice. Just the sentence — no preamble, no advice."
                    ),
                ),
            ],
            max_output_tokens=90,
            temperature=0.3,
            timeout_seconds=FAST_LANE_TIMEOUT,
            feature="copilot_fast_lane",
        )
        return str(result.content).strip()

    async def _main_lane(self, prefix: str, context: str, mode: str) -> dict[str, Any]:
        result = await self._gateway.generate(
            task_class=TaskClass.COPILOT_ANSWER,
            messages=[
                Message(role="system", content=prefix),
                Message(
                    role="user",
                    content=(
                        f"{context}\n\nAnswer in `{mode}` mode. Write spoken_answer as the words "
                        "the candidate says next, in their own voice, using their real details "
                        "from the evidence above. Mark a key point as candidate_fact only when "
                        "that evidence supports it, and cite the evidence ids. Otherwise use "
                        "guidance or general_knowledge."
                    ),
                ),
            ],
            json_schema=ANSWER_SCHEMA,
            # Room for a full spoken answer (detailed mode runs ~7 sentences)
            # plus the key points and JSON overhead. Server-side caps still trim
            # whatever overshoots the mode.
            max_output_tokens=1_000,
            temperature=0.35,
            timeout_seconds=MAIN_LANE_TIMEOUT,
            feature="copilot_answer",
        )
        return dict(result.content)

    # ── Retrieval ────────────────────────────────────────────────────

    async def _retrieve(
        self, question: DetectedQuestion, bundle: ContextBundle
    ) -> tuple[list[dict[str, Any]], bool]:
        try:
            result = await asyncio.wait_for(
                self._retrieval.retrieve(
                    RetrievalQuery(
                        user_id=bundle.user_id, text=question.content, limit=EVIDENCE_LIMIT
                    )
                ),
                timeout=RETRIEVAL_TIMEOUT,
            )
        except (TimeoutError, Exception) as exc:
            log.warning("retrieval_degraded", error=type(exc).__name__)
            return [], True

        return (
            [
                {
                    "id": str(node.entity_id),
                    "type": str(node.entity_type),
                    "label": node.label,
                    "text": node.text,
                }
                for node in result.nodes
            ],
            result.degraded,
        )

    # ── Persistence ──────────────────────────────────────────────────

    async def _persist(
        self,
        question: DetectedQuestion,
        answer: Answer,
        grounding: GroundingReport,
        mode: str,
        revision_reason: str,
        timings: dict[str, float],
        evidence: list[dict[str, Any]] | None,
    ) -> AiSuggestion:
        from sqlalchemy import func, select

        revision = (
            await self._db.execute(
                select(func.coalesce(func.max(AiSuggestion.revision), 0) + 1).where(
                    AiSuggestion.detected_question_id == question.id
                )
            )
        ).scalar_one()

        suggestion = AiSuggestion(
            user_id=question.user_id,
            detected_question_id=question.id,
            session_id=question.session_id,
            response_mode=mode,
            revision=int(revision),
            revision_reason=revision_reason,
            content=answer.to_json(),
            evidence_ids=answer.cited_evidence_ids,
            grounding=grounding.to_json(),
            prompt_ref="copilot.answer@1",
            latency_ms={k: round(v, 1) for k, v in timings.items()},
        )
        self._db.add(suggestion)

        question.lifecycle_state = QuestionLifecycle.DISPLAYED
        await self._db.flush()
        return suggestion


async def adjust(
    db: AsyncSession,
    *,
    suggestion_id: uuid.UUID,
    user_id: uuid.UUID,
    mode: str,
    bundle: ContextBundle,
    memory: ConversationMemory,
) -> GuidanceResult:
    """Shorter / expand / mode switch — a new revision of the same question."""
    from sqlalchemy import select

    suggestion = (
        await db.execute(
            select(AiSuggestion).where(
                AiSuggestion.id == suggestion_id, AiSuggestion.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if suggestion is None:
        raise AppError.not_found("Suggestion")

    question = await db.get(DetectedQuestion, suggestion.detected_question_id)
    if question is None:
        raise AppError.not_found("Question")

    return await CopilotEngine(db).guide(
        question=question,
        bundle=bundle,
        memory=memory,
        response_mode=mode,
        revision_reason="mode_switch",
    )
