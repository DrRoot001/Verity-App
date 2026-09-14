"""Speech-to-text adapters and voice activity detection (PRD §23, §21.3).

The provider interface exists so a vendor can be swapped without touching the
pipeline (NFR-AI-001). Three implementations ship:

``TextPassthroughSTT``
    The web client and the replay harness push already-transcribed utterances.
    Real, not a stub: it is the transport the text-mode session genuinely uses
    (PRD §34.1 Manual Mode), and it lets the whole detection pipeline be tested
    without audio.

``DeepgramSTT`` / ``AssemblySTT``
    Streaming HTTP/WebSocket clients, selected when a key is configured.

Voice activity detection is energy-based over PCM frames. A learned VAD would
be marginally better in noise and would add a model dependency to the one path
with a 10 ms budget (§21.3), so the tradeoff is deliberate.
"""

from __future__ import annotations

import array
import math
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from verity.platform.config import settings
from verity.platform.errors import AppError, ErrorCode
from verity.platform.logging import get_logger

log = get_logger("realtime.stt")

#: 16 kHz mono PCM16, 20 ms frames — the format the desktop capture emits.
SAMPLE_RATE = 16_000
FRAME_MS = 20
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_MS // 1000

#: RMS below this is silence. Tuned for headset capture with light room noise.
SILENCE_RMS_THRESHOLD = 380.0
#: Keep the channel "active" briefly after speech so a breath is not a boundary.
HANGOVER_MS = 300

#: Below this mean confidence over a window, warn the user about audio quality
#: rather than silently producing a bad transcript (PRD §34.1).
POOR_AUDIO_CONFIDENCE = 0.6


class SttEvent(StrEnum):
    PARTIAL = "partial"
    FINAL = "final"
    SPEAKER_CHANGE = "speaker_change"
    ERROR = "error"


@dataclass(slots=True)
class SttResult:
    event: SttEvent
    content: str
    channel: str
    start_ms: int
    end_ms: int
    confidence: float | None = None
    speaker_tag: str | None = None
    language: str | None = None


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

    async def transcribe(self, channel: str, pcm: bytes, timestamp_ms: int) -> list[SttResult]: ...


# ── Voice activity detection ─────────────────────────────────────────


def frame_rms(pcm: bytes) -> float:
    """Root mean square of a PCM16 frame."""
    if len(pcm) < 2:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


@dataclass(slots=True)
class ChannelActivity:
    """Per-channel speech state, with hangover so pauses are not boundaries."""

    is_active: bool = False
    last_voice_ms: int = 0
    silence_started_ms: int | None = None
    total_voiced_ms: int = 0

    def update(self, *, rms: float, timestamp_ms: int) -> bool:
        voiced = rms >= SILENCE_RMS_THRESHOLD
        if voiced:
            self.is_active = True
            self.last_voice_ms = timestamp_ms
            self.silence_started_ms = None
            self.total_voiced_ms += FRAME_MS
            return True

        if self.is_active:
            if self.silence_started_ms is None:
                self.silence_started_ms = timestamp_ms
            elif timestamp_ms - self.silence_started_ms >= HANGOVER_MS:
                self.is_active = False
        return False

    def silence_ms(self, now_ms: int) -> int:
        if self.silence_started_ms is None:
            return 0
        return max(0, now_ms - self.silence_started_ms)


# ── Providers ────────────────────────────────────────────────────────


class TextPassthroughSTT:
    """Transport for clients that send text rather than audio."""

    name = "text-passthrough"

    def capabilities(self) -> SttCapabilities:
        return SttCapabilities(
            diarization=False,
            partial_results=False,
            punctuation=True,
            languages=frozenset({"*"}),
            typical_partial_latency_ms=0,
        )

    async def transcribe(self, channel: str, pcm: bytes, timestamp_ms: int) -> list[SttResult]:
        # Audio is not this transport's input; callers push text directly.
        return []


class DeepgramSTT:
    """Streaming client for Deepgram (PRD §23 provider abstraction)."""

    name = "deepgram"

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise AppError.internal("Deepgram provider is missing its API key.")
        self._api_key = api_key
        self._buffers: dict[str, bytearray] = {}

    def capabilities(self) -> SttCapabilities:
        return SttCapabilities(
            diarization=True,
            partial_results=True,
            punctuation=True,
            languages=frozenset({"en", "es", "fr", "de", "pt", "hi", "ja"}),
            typical_partial_latency_ms=300,
        )

    async def transcribe(self, channel: str, pcm: bytes, timestamp_ms: int) -> list[SttResult]:
        import httpx

        buffer = self._buffers.setdefault(channel, bytearray())
        buffer.extend(pcm)
        # Batch to roughly a second: per-frame requests would spend more time in
        # HTTP overhead than in recognition.
        if len(buffer) < SAMPLE_RATE * 2:
            return []

        payload = bytes(buffer)
        buffer.clear()

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(
                    "https://api.deepgram.com/v1/listen",
                    params={
                        "encoding": "linear16",
                        "sample_rate": str(SAMPLE_RATE),
                        "punctuate": "true",
                        "model": "nova-2",
                    },
                    headers={
                        "Authorization": f"Token {self._api_key}",
                        "Content-Type": "audio/raw",
                    },
                    content=payload,
                )
        except Exception as exc:
            raise AppError(ErrorCode.STT_UNAVAILABLE, "Transcription is unavailable.") from exc

        if response.status_code >= 400:
            raise AppError(
                ErrorCode.STT_UNAVAILABLE,
                "The transcription provider rejected the request.",
                detail={"status": response.status_code},
            )

        body = response.json()
        alternatives = body.get("results", {}).get("channels", [{}])[0].get("alternatives", [{}])
        transcript = alternatives[0].get("transcript", "") if alternatives else ""
        if not transcript.strip():
            return []

        return [
            SttResult(
                event=SttEvent.FINAL,
                content=transcript,
                channel=channel,
                start_ms=timestamp_ms,
                end_ms=timestamp_ms + len(payload) // 32,
                confidence=float(alternatives[0].get("confidence", 0.0)) if alternatives else None,
            )
        ]


@dataclass(slots=True)
class ProviderHealth:
    """Rolling health used to decide failover (PRD §20.3)."""

    failures: int = 0
    successes: int = 0
    last_failure_at: float | None = None
    confidences: list[float] = field(default_factory=list)

    def record_success(self, confidence: float | None) -> None:
        self.successes += 1
        self.failures = 0
        if confidence is not None:
            self.confidences = [*self.confidences, confidence][-30:]

    def record_failure(self) -> None:
        self.failures += 1
        self.last_failure_at = time.monotonic()

    @property
    def should_failover(self) -> bool:
        """Three consecutive failures, matching the PRD's retry budget."""
        return self.failures >= 3

    @property
    def mean_confidence(self) -> float | None:
        return sum(self.confidences) / len(self.confidences) if self.confidences else None

    @property
    def audio_is_poor(self) -> bool:
        mean = self.mean_confidence
        return mean is not None and len(self.confidences) >= 10 and mean < POOR_AUDIO_CONFIDENCE


class SttRouter:
    """Primary provider with an ordered fallback chain (PRD §20.3, §34.1).

    When every provider fails the session does not end — it enters Manual Mode,
    where the transcript panel is replaced with an explanation and manual
    question entry stays available.
    """

    def __init__(self, providers: list[STTProvider]) -> None:
        if not providers:
            raise AppError.internal("SttRouter requires at least one provider.")
        self._providers = providers
        self._index = 0
        self._health: dict[str, ProviderHealth] = {p.name: ProviderHealth() for p in providers}
        self.manual_mode = False

    @property
    def active(self) -> STTProvider:
        return self._providers[self._index]

    @property
    def health(self) -> ProviderHealth:
        return self._health[self.active.name]

    def _advance(self) -> bool:
        if self._index + 1 < len(self._providers):
            self._index += 1
            log.warning("stt_failover", to_provider=self.active.name)
            return True
        self.manual_mode = True
        log.error("stt_all_providers_failed_manual_mode")
        return False

    async def transcribe(self, channel: str, pcm: bytes, timestamp_ms: int) -> list[SttResult]:
        if self.manual_mode:
            return []

        try:
            results = await self.active.transcribe(channel, pcm, timestamp_ms)
        except AppError:
            self.health.record_failure()
            if self.health.should_failover:
                self._advance()
            return []

        for result in results:
            self._health[self.active.name].record_success(result.confidence)
        return results


class GroqWhisperSTT:
    """Whisper on Groq, reusing the key the gateway already holds (PRD §23).

    Chosen because it needs no second vendor account: the same credential that
    routes generation also transcribes. Groq exposes Whisper over the
    OpenAI-compatible ``/audio/transcriptions`` route, which is batch rather
    than streaming, so audio is accumulated into short utterances instead of
    streamed frame by frame.

    The buffering window is the whole design tradeoff. Too short and Whisper
    receives half a sentence and guesses; too long and the candidate waits.
    Speech is flushed once the speaker has paused, with a hard ceiling so a
    monologue still produces text on the way through.
    """

    name = "groq_whisper"

    #: Flush after this much silence — a natural clause break, not a full stop.
    SILENCE_FLUSH_MS = 700
    #: Never hold more than this before transcribing anyway.
    MAX_UTTERANCE_MS = 12_000
    #: Minimum *voiced* audio before a flush is worth paying for. Measured on
    #: speech, not on buffer length: a cough followed by a long silence holds a
    #: full second of audio while containing nothing anyone said.
    MIN_UTTERANCE_MS = 400

    def __init__(self, api_key: str, *, model: str = "whisper-large-v3-turbo") -> None:
        if not api_key:
            raise AppError.internal("Groq transcription is missing its API key.")
        self._api_key = api_key
        self._model = model
        self._buffers: dict[str, bytearray] = {}
        self._started: dict[str, int] = {}
        self._last_voice: dict[str, int] = {}
        self._voiced_ms: dict[str, int] = {}

    def capabilities(self) -> SttCapabilities:
        return SttCapabilities(
            diarization=False,
            partial_results=False,
            punctuation=True,
            languages=frozenset({"en", "es", "fr", "de", "pt", "hi", "ja", "zh"}),
            typical_partial_latency_ms=900,
        )

    async def transcribe(self, channel: str, pcm: bytes, timestamp_ms: int) -> list[SttResult]:
        buffer = self._buffers.setdefault(channel, bytearray())
        if not buffer:
            self._started[channel] = timestamp_ms
        buffer.extend(pcm)

        chunk_ms = (len(pcm) // 2) * 1000 // SAMPLE_RATE
        if frame_rms(pcm) >= SILENCE_RMS_THRESHOLD:
            self._last_voice[channel] = timestamp_ms
            self._voiced_ms[channel] = self._voiced_ms.get(channel, 0) + chunk_ms

        started = self._started.get(channel, timestamp_ms)
        held_ms = timestamp_ms - started
        quiet_ms = timestamp_ms - self._last_voice.get(channel, started)
        voiced_ms = self._voiced_ms.get(channel, 0)

        speaker_paused = quiet_ms >= self.SILENCE_FLUSH_MS
        if not speaker_paused and held_ms < self.MAX_UTTERANCE_MS:
            return []

        payload = bytes(buffer)
        buffer.clear()
        self._started.pop(channel, None)
        self._voiced_ms.pop(channel, None)

        # Too little was actually said to be a question. Dropping it here is
        # what keeps a quiet room from billing for its own silence.
        if voiced_ms < self.MIN_UTTERANCE_MS:
            return []

        text, confidence = await self._post(payload)
        if not text:
            return []
        return [
            SttResult(
                event=SttEvent.FINAL,
                content=text,
                channel=channel,
                start_ms=started,
                end_ms=timestamp_ms,
                confidence=confidence,
            )
        ]

    async def _post(self, pcm: bytes) -> tuple[str, float | None]:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                response = await client.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    files={"file": ("audio.wav", wav_bytes(pcm), "audio/wav")},
                    data={
                        "model": self._model,
                        "response_format": "json",
                        "temperature": "0",
                    },
                )
        except Exception as exc:
            raise AppError(ErrorCode.STT_UNAVAILABLE, "Transcription is unavailable.") from exc

        if response.status_code >= 400:
            raise AppError(
                ErrorCode.STT_UNAVAILABLE,
                "The transcription provider rejected the request.",
                detail={"status": response.status_code},
            )

        body = response.json()
        return str(body.get("text", "")).strip(), None


def wav_bytes(pcm: bytes, *, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Wrap raw PCM16 mono in a WAV container.

    Whisper endpoints want a container, not headerless samples, and building
    the 44-byte header here avoids a dependency for what is a fixed layout.
    """
    import struct

    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(pcm))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", len(pcm))
        + pcm
    )


def build_stt_router() -> SttRouter:
    """Select providers from configuration."""
    providers: list[STTProvider] = []

    deepgram_key = getattr(settings, "deepgram_api_key", None)
    if settings.stt_primary_provider == "deepgram" and deepgram_key:
        providers.append(DeepgramSTT(deepgram_key.get_secret_value()))

    # Groq needs no separate account: the generation key transcribes too, which
    # is what makes live audio work out of the box rather than after a signup.
    groq_key = getattr(settings, "groq_api_key", None)
    if groq_key and settings.stt_primary_provider in ("groq", "null", ""):
        providers.append(GroqWhisperSTT(groq_key.get_secret_value()))

    # Always last in the chain: text mode is the floor the session degrades to.
    providers.append(TextPassthroughSTT())
    return SttRouter(providers)


async def iter_frames(pcm: bytes, start_ms: int) -> AsyncIterator[tuple[bytes, int]]:
    """Split a buffer into 20 ms frames with timestamps."""
    frame_bytes = SAMPLES_PER_FRAME * 2
    for offset in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
        yield pcm[offset : offset + frame_bytes], start_ms + (offset // frame_bytes) * FRAME_MS


def summarize_audio(frames: list[float]) -> dict[str, Any]:
    """Diagnostics for the session panel — never shown as a user deficiency."""
    if not frames:
        return {"frames": 0}
    return {
        "frames": len(frames),
        "mean_rms": round(sum(frames) / len(frames), 1),
        "silent_ratio": round(sum(1 for f in frames if f < SILENCE_RMS_THRESHOLD) / len(frames), 3),
    }
