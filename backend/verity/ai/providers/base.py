"""Provider protocols (PRD §20.2, §38 provider abstraction).

Every external AI capability enters the system through one of these protocols.
``verity/ai/providers`` is the only package permitted to import a vendor SDK —
enforced by an import-linter contract (NFR-AI-001), so swapping a vendor cannot
leak into application code.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class TaskClass(StrEnum):
    """Routing keys from PRD §20.1. Application code names a task, not a model."""

    UTTERANCE_CLASSIFY = "utterance_classify"
    BOUNDARY_SCORE = "boundary_score"
    QUESTION_CLASSIFY = "question_classify"
    INTENT_TO_RETRIEVAL_QUERY = "intent_to_retrieval_query"
    ANSWER_DIRECTION = "answer_direction"
    COPILOT_ANSWER = "copilot_answer"
    MOCK_INTERVIEWER_TURN = "mock_interviewer_turn"
    RUBRIC_EVALUATE_ANSWER = "rubric_evaluate_answer"
    RESUME_EXTRACT = "resume_extract"
    JD_EXTRACT = "jd_extract"
    STORY_GENERATE = "story_generate"
    SYSTEM_DESIGN_GUIDANCE = "system_design_guidance"
    COMPLEX_CODING = "complex_coding"
    SESSION_REPORT = "session_report"
    WEAKNESS_ANALYSIS = "weakness_analysis"
    ROLLING_SUMMARY = "rolling_summary"
    EMBEDDINGS = "embeddings"


@dataclass(frozen=True, slots=True)
class Message:
    role: str  # system | user | assistant
    content: str


@dataclass(frozen=True, slots=True)
class CompletionRequest:
    task_class: TaskClass
    messages: Sequence[Message]
    max_output_tokens: int = 1024
    temperature: float = 0.2
    json_schema: dict[str, Any] | None = None
    #: Marks the stable prefix eligible for provider-side prompt caching
    #: (PRD §20.4). Measured hit rate is a tracked metric.
    cache_prefix_messages: int = 0
    timeout_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0


@dataclass(frozen=True, slots=True)
class Completion:
    text: str
    usage: Usage
    model: str
    provider: str
    finish_reason: str = "stop"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Delta:
    """One streamed increment. ``text`` is empty on the terminal delta."""

    text: str
    is_final: bool = False
    usage: Usage | None = None


@dataclass(frozen=True, slots=True)
class Capabilities:
    streaming: bool
    json_schema: bool
    tools: bool
    max_context_tokens: int
    languages: frozenset[str] = frozenset({"en"})


@runtime_checkable
class LLMProvider(Protocol):
    name: str

    async def complete(self, request: CompletionRequest) -> Completion: ...

    def stream(self, request: CompletionRequest) -> AsyncIterator[Delta]: ...

    def capabilities(self) -> Capabilities: ...


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    vectors: list[list[float]]
    model: str
    provider: str
    usage: Usage


@runtime_checkable
class EmbeddingProvider(Protocol):
    name: str
    dimensions: int
    model: str

    async def embed(self, texts: Sequence[str]) -> EmbeddingResult: ...


@dataclass(frozen=True, slots=True)
class SttConfig:
    language: str = "en-US"
    sample_rate: int = 16_000
    interim_results: bool = True
    diarize: bool = False
    vocabulary_hints: Sequence[str] = ()


@dataclass(frozen=True, slots=True)
class SttCapabilities:
    diarization: bool
    partial_results: bool
    punctuation: bool
    languages: frozenset[str]
    typical_partial_latency_ms: int


@runtime_checkable
class STTProvider(Protocol):
    name: str

    def capabilities(self) -> SttCapabilities: ...
