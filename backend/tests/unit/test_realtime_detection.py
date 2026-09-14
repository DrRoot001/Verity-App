"""Question detection, memory and context budgeting (PRD §14.3, §14.6, §20.4).

The requirement these protect is not "detect questions" — it is *don't generate
on everything*. An assistant that fires on every sentence burns budget, buries
the useful card and trains the candidate to ignore the screen.
"""

from __future__ import annotations

import pytest

from verity.realtime import protocol
from verity.realtime.detector import (
    DETECT_THRESHOLD,
    FINALIZE_THRESHOLD,
    UtteranceClass,
    accumulation_window_ms,
    classify,
    detect,
    fuse,
    is_superseded_by,
    score_signals,
    split_multi_part,
)
from verity.realtime.memory import VERBATIM_TURN_LIMIT, ConversationMemory
from verity.realtime.orchestrator import (
    ALLOCATIONS,
    Priority,
    assemble,
    estimate_tokens,
    static_prefix,
)

# ── Classification ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("Tell me about a time you handled a production incident.", UtteranceClass.QUESTION),
        ("What was your role on that project?", UtteranceClass.QUESTION),
        ("How would you design a rate limiter?", UtteranceClass.TECHNICAL_PROMPT),
        ("Write a function that reverses a linked list.", UtteranceClass.CODING_PROMPT),
        ("So, how are you today?", UtteranceClass.SMALL_TALK),
        ("The recruiter will be in touch about next steps.", UtteranceClass.LOGISTICS),
        ("That's a pretty common pattern, right?", UtteranceClass.RHETORICAL),
        ("We built this team about three years ago.", UtteranceClass.STATEMENT),
    ],
)
def test_utterances_are_classified(utterance: str, expected: str) -> None:
    assert classify(utterance) == expected


def test_follow_ups_need_a_prior_question() -> None:
    text = "You mentioned caching — can you expand on that?"
    assert classify(text, previous_question="How did you cut latency?") == UtteranceClass.FOLLOW_UP


def test_multi_part_questions_are_decomposed() -> None:
    """FR-RT-003: two asks must not be answered as one blob."""
    utterance = "What was the hardest part? How did you handle the rollback?"
    parts = split_multi_part(utterance)

    assert len(parts) == 2
    assert classify(utterance) == UtteranceClass.MULTI_PART_QUESTION


def test_conjoined_questions_in_one_sentence_are_split() -> None:
    parts = split_multi_part("Tell me about the migration and how did you test it?")
    assert len(parts) == 2


# ── Signal fusion ────────────────────────────────────────────────────


def test_a_complete_question_with_silence_finalizes() -> None:
    result = detect(
        content="Tell me about a time you led an incident response.",
        silence_ms=700,
        candidate_channel_active=True,
    )

    assert result.is_finalized
    assert result.should_generate
    assert result.utterance_class == UtteranceClass.QUESTION


def test_a_trailing_fragment_does_not_finalize() -> None:
    """Mid-thought speech must not trigger a generation."""
    result = detect(content="And then we decided to", silence_ms=200)

    assert not result.is_finalized
    assert not result.should_generate


def test_small_talk_never_generates_however_confident() -> None:
    """FR-RT-002: class gates generation independently of confidence."""
    result = detect(content="How are you today?", silence_ms=1500, candidate_channel_active=True)

    assert result.confidence >= FINALIZE_THRESHOLD
    assert result.should_generate is False
    assert "not worth a generation" in result.reason


def test_rhetorical_tags_do_not_generate() -> None:
    result = detect(
        content="That's how most teams do it, right?", silence_ms=800, candidate_channel_active=True
    )
    assert result.should_generate is False


def test_borderline_detections_surface_without_generating() -> None:
    """FR-RT-006: show a 'possible question' rather than spend a model call.

    A complete-sounding ask that has not yet earned a boundary — no terminal
    punctuation, a short pause, the candidate not yet speaking — should appear
    in the UI but must not trigger a generation.
    """
    result = detect(content="What was the hardest part of that project", silence_ms=450)

    assert DETECT_THRESHOLD <= result.confidence < FINALIZE_THRESHOLD
    assert result.is_detected
    assert not result.should_generate


def test_a_short_fragment_does_not_even_surface() -> None:
    """Three words and a half-pause is someone thinking, not asking."""
    result = detect(content="What about scale", silence_ms=300)

    assert result.confidence < DETECT_THRESHOLD
    assert not result.is_detected


def test_every_signal_is_recorded_for_diagnosis() -> None:
    result = detect(content="Why did you choose Postgres?", silence_ms=700)
    signals = result.signals.to_json()

    assert set(signals) == {
        "trailing_silence",
        "terminal_prosody",
        "interrogative",
        "turn_taking",
        "semantic_completeness",
    }
    assert all(0.0 <= v <= 1.0 for v in signals.values())


def test_candidate_speaking_raises_confidence() -> None:
    """The candidate starting to speak is strong evidence the ask landed."""
    without = detect(content="What did you learn?", silence_ms=400)
    with_turn = detect(content="What did you learn?", silence_ms=400, candidate_channel_active=True)

    assert with_turn.confidence > without.confidence


def test_fused_confidence_is_bounded() -> None:
    signals = score_signals(
        content="Tell me about a hard decision you made.",
        silence_ms=5000,
        ends_with_terminal_punctuation=True,
        candidate_channel_active=True,
    )
    assert 0.0 <= fuse(signals) <= 1.0


def test_rapid_speech_shortens_the_accumulation_window() -> None:
    """PRD §35 edge case 16: don't merge two questions from a fast speaker."""
    assert accumulation_window_ms(220) < accumulation_window_ms(140)


def test_a_distinct_new_question_supersedes_the_previous_one() -> None:
    assert is_superseded_by("Tell me about your last role.", "How would you shard a database?")


def test_a_continuation_does_not_supersede() -> None:
    assert not is_superseded_by(
        "Tell me about your last role", "Tell me about your last role and what you owned"
    )


# ── Conversation memory (PRD §14.6) ──────────────────────────────────


def test_memory_keeps_a_bounded_verbatim_window() -> None:
    memory = ConversationMemory()
    for i in range(VERBATIM_TURN_LIMIT + 5):
        memory.add_turn("interviewer", f"turn {i}", at_ms=i * 1000)

    assert len(memory.verbatim) == VERBATIM_TURN_LIMIT
    # Evicted turns are folded into the summary, never silently dropped.
    assert "turn 0" in memory.rolling_summary


def test_memory_prevents_recommending_a_story_twice() -> None:
    """FR-COP-010."""
    memory = ConversationMemory()
    memory.record_story_use("story_42")
    memory.record_story_use("story_42")

    assert memory.stories_used == ["story_42"]


def test_memory_detects_a_repeated_question() -> None:
    memory = ConversationMemory()
    memory.record_question("q1", "Tell me about a production incident you handled", None, 0)

    assert memory.has_asked_similar("Tell me about a production incident you handled recently")
    assert not memory.has_asked_similar("How do you approach system design?")


def test_memory_round_trips_through_json() -> None:
    memory = ConversationMemory()
    memory.add_turn("interviewer", "What did you own?", 1000)
    memory.record_question("q1", "What did you own?", "behavioral", 1000)
    memory.record_claim("Cut latency 60%", "ach_1", 2000)
    memory.label_speaker("spk_1", "Hiring Manager")

    restored = ConversationMemory.from_json(memory.to_json())

    assert restored.questions_asked == memory.questions_asked
    assert restored.claims_made == memory.claims_made
    assert restored.speakers["spk_1"]["label"] == "Hiring Manager"
    assert [t.content for t in restored.verbatim] == ["What did you own?"]


# ── Context budgeting (PRD §20.4) ────────────────────────────────────


def _bundle() -> tuple[dict[str, object], dict[str, object]]:
    candidate = {
        "experiences": [
            {"id": "e1", "title": "Backend Engineer", "company": "Northwind", "is_current": True}
        ],
        "story_index": [
            {"id": "s1", "title": "Payments outage", "categories": ["crisis_recovery"]},
            {"id": "s2", "title": "Team migration", "categories": ["leadership"]},
        ],
    }
    opportunity = {
        "role_title": "Senior Backend Engineer",
        "company_name": "Northwind",
        "seniority": "senior",
        "must_have": ["5+ years backend", "Kubernetes in production"],
        "likely_themes": [{"value": "incident response", "confidence": 0.8, "basis": "JD"}],
    }
    return candidate, opportunity


def test_the_current_question_is_never_dropped() -> None:
    """Priority 0 survives even an absurdly small budget."""
    candidate, opportunity = _bundle()
    assembled = assemble(
        question="What was the hardest incident you handled?",
        bundle_candidate=candidate,
        bundle_opportunity=opportunity,
        evidence=[
            {"id": "a1", "type": "achievement", "label": "MTTR 42m to 9m", "text": "x" * 4000}
        ],
        memory=ConversationMemory(),
        token_budget=2_100,
    )

    labels = [s.label for s in assembled.sections]
    assert "Current question" in labels
    assert "hardest incident" in assembled.render()


def test_low_priority_content_is_dropped_first() -> None:
    candidate, opportunity = _bundle()
    memory = ConversationMemory()
    memory.rolling_summary = "y" * 6000

    assembled = assemble(
        question="Tell me about a migration.",
        bundle_candidate=candidate,
        bundle_opportunity=opportunity,
        evidence=[{"id": "a1", "type": "achievement", "label": "L", "text": "z" * 12_000}],
        memory=memory,
        token_budget=3_000,
    )

    assert assembled.truncated_at is not None
    priorities = [s.priority for s in assembled.sections]
    assert priorities == sorted(priorities), "sections stay in priority order"


def test_truncation_is_recorded_so_a_thin_answer_is_explainable() -> None:
    candidate, opportunity = _bundle()
    assembled = assemble(
        question="Q?",
        bundle_candidate=candidate,
        bundle_opportunity=opportunity,
        evidence=[{"id": "a1", "type": "achievement", "label": "L", "text": "z" * 30_000}],
        memory=ConversationMemory(),
        token_budget=2_400,
    )

    payload = assembled.to_json()
    assert payload["truncated_at"] or payload["dropped"]
    assert payload["total_tokens"] > 0


def test_already_used_stories_are_excluded_from_context() -> None:
    candidate, opportunity = _bundle()
    memory = ConversationMemory()
    memory.record_story_use("s1")

    assembled = assemble(
        question="Tell me about a crisis.",
        bundle_candidate=candidate,
        bundle_opportunity=opportunity,
        evidence=[],
        memory=memory,
    )

    rendered = assembled.render()
    assert "Team migration" in rendered
    assert "Payments outage" not in rendered


def test_context_stays_within_its_budget() -> None:
    candidate, opportunity = _bundle()
    budget = 8_000
    assembled = assemble(
        question="Q?",
        bundle_candidate=candidate,
        bundle_opportunity=opportunity,
        evidence=[
            {"id": f"a{i}", "type": "achievement", "label": "L", "text": "w" * 2000}
            for i in range(20)
        ],
        memory=ConversationMemory(),
        token_budget=budget,
    )
    assert assembled.total_tokens <= budget


def test_allocations_cover_every_priority() -> None:
    assert set(ALLOCATIONS) == set(Priority)


def test_static_prefix_is_stable_for_caching() -> None:
    """§20.4: the cache-eligible prefix must be byte-identical across calls."""
    _, opportunity = _bundle()
    assert static_prefix(opportunity, "balanced") == static_prefix(opportunity, "balanced")
    assert static_prefix(opportunity, "concise") != static_prefix(opportunity, "balanced")


def test_prefix_states_the_grounding_contract() -> None:
    _, opportunity = _bundle()
    prefix = static_prefix(opportunity, "balanced")
    assert "Never invent" in prefix
    assert "candidate_fact" in prefix


def test_token_estimate_is_conservative() -> None:
    """Overestimating costs a little context; underestimating truncates live."""
    text = "a" * 360
    assert estimate_tokens(text) >= 100


# ── Wire protocol (PRD §26) ──────────────────────────────────────────


def test_audio_frames_round_trip() -> None:
    encoded = protocol.encode_audio_frame("system", 123_456, b"\x01\x02\x03")
    frame = protocol.decode_audio_frame(encoded)

    assert frame.channel == "system"
    assert frame.timestamp_ms == 123_456
    assert frame.payload == b"\x01\x02\x03"


def test_a_truncated_audio_frame_is_rejected() -> None:
    with pytest.raises(ValueError, match="shorter than its header"):
        protocol.decode_audio_frame(b"\x00\x01")


def test_an_unknown_channel_is_rejected() -> None:
    bad = bytes([9]) + (0).to_bytes(8, "big") + b"payload"
    with pytest.raises(ValueError, match="unknown audio channel"):
        protocol.decode_audio_frame(bad)


def test_every_envelope_carries_ordering_metadata() -> None:
    envelope = protocol.build(
        "01a00000-0000-7000-8000-000000000000",
        protocol.ServerEvent.STT_FINAL,
        protocol.TranscriptPayload(
            channel="system",
            speaker_role="interviewer",
            content="Tell me about yourself.",
            start_ms=0,
            end_ms=1500,
        ),
        seq=7,
    )

    assert envelope.seq == 7
    assert envelope.v == protocol.PROTOCOL_VERSION
    assert envelope.event_id.startswith("evt_")
    assert envelope.payload["content"] == "Tell me about yourself."


# ── Mid-sentence pauses (the interviewer is still talking) ───────────


def test_a_pause_fragment_does_not_earn_a_generation() -> None:
    """Whisper punctuates a thinking pause with a full stop it invented.

    Trusting that period scores half a question as a whole one, and the
    candidate gets answered on the first half of what was asked.
    """
    half = detect(
        content="So tell me about a time when you had to.",
        silence_ms=700,
        endpointed=True,
    )
    assert not half.should_generate

    whole = detect(
        content="So tell me about a time when you had to handle a difficult stakeholder.",
        silence_ms=700,
        endpointed=True,
    )
    assert whole.should_generate


def test_the_window_tolerates_a_real_thinking_pause() -> None:
    """The STT flush spends 700 ms before this window opens."""
    assert accumulation_window_ms(None) + 700 >= 2_000
    # A fast talker still gets a shorter window, or two questions merge into one.
    assert accumulation_window_ms(250) < accumulation_window_ms(None)
