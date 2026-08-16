"""Embedding providers (PRD §20.1 ``embed_primary``).

Two implementations behind one protocol:

``HashedNgramEmbedding``
    A real, deterministic **lexical** embedding — hashed character n-grams with
    sublinear term weighting and L2 normalization. Cosine over these vectors
    measures surface-form similarity. It is not a semantic model and is never
    routed in production; it exists so retrieval, indexing and deletion
    propagation are testable offline and in CI without a network dependency or
    an API key. Its limits are asserted in its own tests.

``OpenAICompatibleEmbedding``
    HTTP client for any provider exposing an OpenAI-compatible ``/embeddings``
    endpoint. Used whenever a key is configured.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Sequence
from typing import Any

import httpx

from verity.ai.providers.base import EmbeddingResult, Usage
from verity.platform.config import settings
from verity.platform.errors import AppError, ErrorCode

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class HashedNgramEmbedding:
    """Deterministic lexical embedding. Development and test routing only."""

    name = "local-hashed-ngram"
    model = "hashed-ngram-v1"

    def __init__(self, dimensions: int | None = None, ngram: int = 4) -> None:
        self.dimensions = dimensions or settings.embedding_dimensions
        self._ngram = ngram

    def _features(self, text: str) -> Counter[str]:
        words = _TOKEN_RE.findall(text.lower())
        features: Counter[str] = Counter(words)
        # Character n-grams give partial credit for morphology and typos, which
        # word tokens alone miss ("kubernetes" vs "kubernetes'").
        for word in words:
            padded = f"^{word}$"
            for i in range(len(padded) - self._ngram + 1):
                features[padded[i : i + self._ngram]] += 1
        return features

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for feature, count in self._features(text).items():
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            # Signed hashing keeps collisions from systematically inflating norms.
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign * (1.0 + math.log(count))

        norm = math.sqrt(sum(v * v for v in vector))
        if norm == 0.0:
            return vector
        return [v / norm for v in vector]

    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        vectors = [self._vector(t) for t in texts]
        approx_tokens = sum(len(t) // 4 for t in texts)
        return EmbeddingResult(
            vectors=vectors,
            model=self.model,
            provider=self.name,
            usage=Usage(input_tokens=approx_tokens),
        )


class OpenAICompatibleEmbedding:
    """Any provider exposing an OpenAI-compatible embeddings endpoint."""

    name = "openai-compatible"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "text-embedding-3-small",
        base_url: str = "https://api.openai.com/v1",
        dimensions: int | None = None,
    ) -> None:
        if not api_key:
            raise AppError.internal("Embedding provider is missing its API key.")
        self.model = model
        self.dimensions = dimensions or settings.embedding_dimensions
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        payload: dict[str, Any] = {
            "model": self.model,
            "input": list(texts),
            "dimensions": self.dimensions,
        }
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(
                    f"{self._base_url}/embeddings",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
        except httpx.TimeoutException as exc:
            raise AppError(ErrorCode.AI_TIMEOUT, "The embedding provider timed out.") from exc
        except httpx.HTTPError as exc:
            raise AppError(
                ErrorCode.AI_PROVIDER_UNAVAILABLE, "The embedding provider is unreachable."
            ) from exc

        if response.status_code == 429:
            raise AppError(ErrorCode.RATE_LIMITED, "The embedding provider is rate limiting us.")
        if response.status_code >= 400:
            raise AppError(
                ErrorCode.AI_PROVIDER_UNAVAILABLE,
                "The embedding provider rejected the request.",
                detail={"status": response.status_code},
            )

        body = response.json()
        vectors = [item["embedding"] for item in sorted(body["data"], key=lambda d: d["index"])]
        usage = body.get("usage", {})
        return EmbeddingResult(
            vectors=vectors,
            model=body.get("model", self.model),
            provider=self.name,
            usage=Usage(input_tokens=int(usage.get("prompt_tokens", 0))),
        )


def build_embedding_provider() -> HashedNgramEmbedding | OpenAICompatibleEmbedding:
    """Select the provider from configuration (PRD §20.1 routing is data)."""
    if settings.embedding_provider == "openai":
        return OpenAICompatibleEmbedding(api_key=settings.openai_api_key.get_secret_value())
    return HashedNgramEmbedding()
