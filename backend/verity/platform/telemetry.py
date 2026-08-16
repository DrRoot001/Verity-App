"""Tracing and metrics (PRD §30).

OpenTelemetry provides the span tree; Prometheus provides the histograms behind
the dashboards in PRD §30.3. Both are no-ops until configured, so local
development and tests do not require a collector.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from prometheus_client import CollectorRegistry, Counter, Histogram

from verity.platform.config import settings

REGISTRY = CollectorRegistry(auto_describe=True)

# ── API metrics (PRD §30.2) ──────────────────────────────────────────
http_requests = Counter(
    "verity_http_requests_total",
    "HTTP requests by route class and status.",
    labelnames=("method", "route", "status"),
    registry=REGISTRY,
)
http_latency = Histogram(
    "verity_http_request_duration_seconds",
    "HTTP request latency.",
    labelnames=("method", "route"),
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0),
    registry=REGISTRY,
)

# ── Realtime metrics (PRD §32.1 budgets) ─────────────────────────────
_LATENCY_BUCKETS = (0.05, 0.1, 0.2, 0.3, 0.5, 0.8, 1.2, 2.0, 3.0, 5.0, 8.0)

realtime_stage_latency = Histogram(
    "verity_realtime_stage_duration_seconds",
    "Per-stage realtime pipeline latency.",
    labelnames=("stage",),
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)
guidance_e2e_latency = Histogram(
    "verity_guidance_e2e_duration_seconds",
    "Question finalization to first rendered answer direction (primary SLO).",
    buckets=_LATENCY_BUCKETS,
    registry=REGISTRY,
)

# ── AI metrics (PRD §33) ─────────────────────────────────────────────
ai_tokens = Counter(
    "verity_ai_tokens_total",
    "Token consumption by model and direction.",
    labelnames=("provider", "model", "task_class", "direction"),
    registry=REGISTRY,
)
ai_cost_usd = Counter(
    "verity_ai_cost_usd_total",
    "Estimated AI spend.",
    labelnames=("provider", "model", "feature"),
    registry=REGISTRY,
)
grounding_violations = Counter(
    "verity_grounding_violations_total",
    "Candidate-fact claims rejected by the grounding validator (PRD §12.6).",
    labelnames=("prompt_id",),
    registry=REGISTRY,
)
provider_fallbacks = Counter(
    "verity_provider_fallbacks_total",
    "Fallback activations by provider and reason (PRD §20.3).",
    labelnames=("kind", "from_provider", "reason"),
    registry=REGISTRY,
)


def configure_tracing() -> None:
    """Install the tracer provider. No-op unless OTel is enabled."""
    if not settings.otel_enabled:
        return
    resource = Resource.create(
        {
            "service.name": settings.service_name,
            "deployment.environment": str(settings.env),
            "cloud.region": settings.region,
        }
    )
    provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(TraceIdRatioBased(settings.trace_sample_ratio)),
    )
    if settings.otel_exporter_otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint))
        )
    trace.set_tracer_provider(provider)


def get_tracer(name: str) -> trace.Tracer:
    return trace.get_tracer(name)


@contextmanager
def stage_span(stage: str, **attributes: Any) -> Iterator[trace.Span]:
    """Time a realtime pipeline stage into both a span and a histogram.

    The fixed stage names form the span tree described in PRD §30.1, which is
    what makes per-question latency attribution possible.
    """
    tracer = get_tracer("verity.realtime")
    with (
        tracer.start_as_current_span(stage, attributes=attributes) as span,
        realtime_stage_latency.labels(stage=stage).time(),
    ):
        yield span
