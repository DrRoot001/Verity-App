"""Buffering rules for batch transcription (PRD §23).

Whisper on Groq is a batch endpoint, not a stream, so the provider decides when
an utterance is finished. That decision is the whole latency/accuracy tradeoff
of live audio: flush too early and the model transcribes half a sentence and
guesses the rest; flush too late and the candidate waits on guidance while the
interviewer has already moved on.

These tests pin the boundary conditions. The network call itself is not
exercised here — it is verified against the live provider separately.
"""

from __future__ import annotations

import array

import pytest

from verity.realtime.stt import SAMPLE_RATE, GroqWhisperSTT, wav_bytes

pytestmark = pytest.mark.asyncio


def speech(ms: int, amplitude: int = 9000) -> bytes:
    """Loud enough to read as speech to the energy gate."""
    samples = SAMPLE_RATE * ms // 1000
    return array.array("h", [amplitude if i % 2 else -amplitude for i in range(samples)]).tobytes()


def silence(ms: int) -> bytes:
    return b"\x00\x00" * (SAMPLE_RATE * ms // 1000)


def provider() -> GroqWhisperSTT:
    return GroqWhisperSTT("test-key")


async def test_speech_alone_does_not_flush() -> None:
    """Someone mid-sentence must not be cut off."""
    stt = provider()

    assert await stt.transcribe("system", speech(300), 300) == []


async def test_a_short_blip_then_silence_is_not_worth_transcribing() -> None:
    """Below the minimum there is not enough signal to be a question."""
    stt = provider()

    assert await stt.transcribe("system", speech(100), 100) == []
    assert await stt.transcribe("system", silence(800), 900) == []


async def test_pure_silence_never_calls_the_provider() -> None:
    """A quiet room would otherwise bill for every buffer."""
    stt = provider()

    for step in range(1, 40):
        assert await stt.transcribe("system", silence(500), step * 500) == []


async def test_channels_buffer_independently() -> None:
    """Otherwise the candidate's words and the interviewer's merge into one."""
    stt = provider()

    await stt.transcribe("mic", speech(200), 200)
    await stt.transcribe("system", speech(200), 200)

    assert set(stt._buffers) == {"mic", "system"}
    assert stt._buffers["mic"] == stt._buffers["system"]


# ── WAV container ───────────────────────────────────────────────────


def test_the_wav_header_declares_16khz_mono_pcm16() -> None:
    """Whisper endpoints want a container; a wrong header is silent garbage."""
    import struct

    header = wav_bytes(speech(100))[:44]

    assert header[:4] == b"RIFF"
    assert header[8:12] == b"WAVE"
    channels, rate = struct.unpack("<HI", header[22:28])
    assert channels == 1
    assert rate == SAMPLE_RATE
    assert struct.unpack("<H", header[34:36])[0] == 16  # bits per sample


def test_the_declared_sizes_match_the_payload() -> None:
    import struct

    pcm = speech(250)
    wav = wav_bytes(pcm)

    assert struct.unpack("<I", wav[4:8])[0] == 36 + len(pcm)
    assert struct.unpack("<I", wav[40:44])[0] == len(pcm)
    assert len(wav) == 44 + len(pcm)


async def test_enough_speech_then_a_pause_does_flush() -> None:
    """The normal case: someone finished asking, so transcribe it."""
    stt = provider()
    await stt.transcribe("system", speech(600), 600)

    # Provider is unreachable in tests, so reaching the network is the signal.
    with pytest.raises(Exception, match="Transcription is unavailable|rejected"):
        await stt.transcribe("system", silence(800), 1400)


async def test_a_monologue_is_flushed_before_the_ceiling_is_exceeded() -> None:
    """Someone who never pauses must still produce text on the way through."""
    stt = provider()

    with pytest.raises(Exception, match="Transcription is unavailable|rejected"):
        for step in range(1, 30):
            await stt.transcribe("system", speech(500), step * 500)
