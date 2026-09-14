"""Grounded Candidate Mode (PRD §12.6, AC-COP-002, AC-GRAPH-010).

This is the product's core promise: Verity does not invent a candidate's
career. The validator is the last line — a model that fabricates an employer or
a metric gets its claim downgraded to guidance rather than rendered as fact.
"""

from __future__ import annotations

from verity.ai.answers import (
    MODES,
    Answer,
    ClaimType,
    KeyPoint,
    ResponseMode,
    enforce_mode,
    no_evidence_answer,
    parse_answer,
    select_mode,
    validate_grounding,
)

EVIDENCE = [
    {
        "id": "ach_1",
        "type": "achievement",
        "label": "Cut deploy time",
        "text": "Cut deploy time from 40 minutes to 9 minutes at Northwind",
    },
    {
        "id": "exp_1",
        "type": "experience",
        "label": "Backend Engineer at Northwind",
        "text": "Backend Engineer at Northwind, owned the deployment pipeline",
    },
]


def _answer(*points: KeyPoint) -> Answer:
    return Answer(answer_direction="Lead with the deployment work.", key_points=list(points))


# ── The validator ────────────────────────────────────────────────────


def test_a_supported_fact_survives() -> None:
    answer = _answer(
        KeyPoint(
            "Cut deploy time from 40 minutes to 9 minutes", ClaimType.CANDIDATE_FACT, ["ach_1"]
        )
    )
    report = validate_grounding(answer, EVIDENCE)

    assert report.downgraded == 0
    assert answer.key_points[0].claim_type == ClaimType.CANDIDATE_FACT
    assert answer.key_points[0].evidence_ids == ["ach_1"]


def test_a_fact_citing_nothing_is_downgraded() -> None:
    answer = _answer(KeyPoint("I led the platform rewrite", ClaimType.CANDIDATE_FACT, []))
    report = validate_grounding(answer, EVIDENCE)

    assert report.downgraded == 1
    assert answer.key_points[0].claim_type == ClaimType.GUIDANCE
    assert answer.key_points[0].downgraded_from == ClaimType.CANDIDATE_FACT
    assert "no evidence cited" in (answer.key_points[0].downgrade_reason or "")


def test_a_fact_citing_a_nonexistent_id_is_downgraded() -> None:
    answer = _answer(KeyPoint("Scaled the billing service", ClaimType.CANDIDATE_FACT, ["ach_999"]))
    report = validate_grounding(answer, EVIDENCE)

    assert report.downgraded == 1
    assert answer.key_points[0].claim_type == ClaimType.GUIDANCE


def test_an_invented_employer_is_caught_even_with_a_real_citation() -> None:
    """The subtle failure: a real evidence id attached to a fabricated company."""
    answer = _answer(
        KeyPoint("Cut deploy time at Stripe by 40 minutes", ClaimType.CANDIDATE_FACT, ["ach_1"])
    )
    report = validate_grounding(answer, EVIDENCE)

    assert report.downgraded == 1
    assert "Stripe" in (answer.key_points[0].downgrade_reason or "")
    assert answer.key_points[0].claim_type == ClaimType.GUIDANCE


def test_an_invented_metric_is_caught() -> None:
    answer = _answer(KeyPoint("Improved deploy time by 95%", ClaimType.CANDIDATE_FACT, ["ach_1"]))
    report = validate_grounding(answer, EVIDENCE)

    assert report.downgraded == 1
    assert "95" in (answer.key_points[0].downgrade_reason or "")


def test_guidance_points_are_never_checked_or_downgraded() -> None:
    answer = _answer(
        KeyPoint("Structure this with STAR", ClaimType.GUIDANCE),
        KeyPoint("Caching is a common approach at scale", ClaimType.GENERAL_KNOWLEDGE),
    )
    report = validate_grounding(answer, EVIDENCE)

    assert report.checked == 0
    assert report.downgraded == 0
    assert [p.claim_type for p in answer.key_points] == [
        ClaimType.GUIDANCE,
        ClaimType.GENERAL_KNOWLEDGE,
    ]


def test_the_report_records_what_was_rejected() -> None:
    answer = _answer(
        KeyPoint("Led the team at Acme", ClaimType.CANDIDATE_FACT, ["exp_1"]),
        KeyPoint(
            "Cut deploy time from 40 minutes to 9 minutes", ClaimType.CANDIDATE_FACT, ["ach_1"]
        ),
    )
    report = validate_grounding(answer, EVIDENCE)

    assert report.checked == 2
    assert report.downgraded == 1
    assert report.violations and "Acme" in report.violations[0]["reason"]
    assert report.validator_version


def test_sentence_starters_are_not_treated_as_invented_names() -> None:
    """'The' and 'Focus' start sentences; they are not fabricated employers."""
    answer = _answer(
        KeyPoint(
            "Focus on the deployment pipeline at Northwind", ClaimType.CANDIDATE_FACT, ["exp_1"]
        )
    )
    report = validate_grounding(answer, EVIDENCE)

    assert report.downgraded == 0


# ── No-evidence path (AC-COP-002) ────────────────────────────────────


def test_no_evidence_yields_a_framework_and_never_a_fact() -> None:
    answer = no_evidence_answer("Tell me about a time you scaled a system.", ResponseMode.STAR)

    assert all(p.claim_type != ClaimType.CANDIDATE_FACT for p in answer.key_points)
    assert answer.gaps, "the user must be told what is missing"
    assert "ask_user" in answer.gaps[0]
    assert answer.cited_evidence_ids == []


# ── The spoken answer (the words the candidate reads aloud) ──────────


def test_spoken_answer_is_cut_on_a_sentence_boundary() -> None:
    """Mid-clause is unspeakable — the user is reading this out loud."""
    answer = _answer(KeyPoint("A point", ClaimType.GUIDANCE))
    answer.spoken_answer = "One. Two. Three. Four. Five. Six. Seven. Eight."
    enforce_mode(answer, ResponseMode.CONCISE)

    assert answer.spoken_answer == "One. Two."


def test_talking_points_mode_has_no_spoken_answer() -> None:
    answer = _answer(KeyPoint("A point", ClaimType.GUIDANCE))
    answer.spoken_answer = "Something to say."
    enforce_mode(answer, ResponseMode.TALKING_POINTS)

    assert answer.spoken_answer == ""


def test_an_invented_figure_in_the_spoken_answer_is_flagged() -> None:
    answer = _answer(KeyPoint("A point", ClaimType.GUIDANCE))
    answer.spoken_answer = "I cut deploy time by 87% at Globex."
    report = validate_grounding(answer, EVIDENCE)

    assert "87%" in report.unverified
    assert "Globex" in report.unverified


def test_the_role_and_question_are_speakable_without_being_evidence() -> None:
    """Otherwise every correct answer trips the flag on the company name."""
    answer = _answer(KeyPoint("A point", ClaimType.GUIDANCE))
    answer.spoken_answer = "At Northwind I owned the deployment pipeline."
    report = validate_grounding(answer, EVIDENCE, extra_corpus="Role: SRE at Initech")

    assert report.unverified == []


# ── Mode enforcement (FR-COP-002) ────────────────────────────────────


def test_concise_mode_caps_points_and_words() -> None:
    answer = _answer(*[KeyPoint(" ".join(["word"] * 40), ClaimType.GUIDANCE) for _ in range(8)])
    answer.answer_direction = "One. Two. Three. Four."
    enforce_mode(answer, ResponseMode.CONCISE)

    cap = MODES[ResponseMode.CONCISE]
    assert len(answer.key_points) <= cap.max_points
    assert all(len(p.text.split()) <= cap.max_words_per_point + 1 for p in answer.key_points)
    assert answer.answer_direction.count(".") == 1


def test_talking_points_mode_drops_the_direction() -> None:
    answer = _answer(KeyPoint("A point", ClaimType.GUIDANCE))
    answer.answer_direction = "Some direction."
    enforce_mode(answer, ResponseMode.TALKING_POINTS)

    assert answer.answer_direction == ""


def test_truncation_lands_on_a_word_boundary() -> None:
    """A point cut mid-word reads as a bug during an interview."""
    long_point = " ".join(f"word{i}" for i in range(20))
    answer = _answer(KeyPoint(long_point, ClaimType.GUIDANCE))
    enforce_mode(answer, ResponseMode.CONCISE)

    text = answer.key_points[0].text
    assert text.endswith("…")
    assert "  " not in text
    assert not text.rstrip("…").endswith(" ")


def test_structure_is_dropped_where_the_mode_excludes_it() -> None:
    answer = _answer(KeyPoint("A point", ClaimType.GUIDANCE))
    answer.structure = "STAR"
    enforce_mode(answer, ResponseMode.CONCISE)

    assert answer.structure is None


# ── Mode selection (FR-COP-003) ──────────────────────────────────────


def test_behavioural_questions_default_to_star() -> None:
    assert select_mode("question", "Tell me about a time you led a migration") == ResponseMode.STAR


def test_technical_prompts_default_to_technical_mode() -> None:
    assert select_mode("technical_prompt", "How would you shard this?") == ResponseMode.TECHNICAL


def test_follow_ups_default_to_concise() -> None:
    assert select_mode("follow_up", "And what happened next?") == ResponseMode.CONCISE


def test_a_pinned_mode_always_wins() -> None:
    pinned = select_mode("technical_prompt", "Tell me about a time...", ResponseMode.EXECUTIVE)
    assert pinned == ResponseMode.EXECUTIVE


# ── Parsing ──────────────────────────────────────────────────────────


def test_parsing_drops_citations_to_unknown_evidence() -> None:
    payload = {
        "answer_direction": "Lead with impact.",
        "key_points": [
            {
                "text": "Cut deploy time",
                "claim_type": "candidate_fact",
                "evidence_ids": ["ach_1", "made_up"],
            },
        ],
    }
    answer = parse_answer(payload, EVIDENCE)

    assert answer.key_points[0].evidence_ids == ["ach_1"]


def test_parsing_survives_malformed_points() -> None:
    payload = {
        "answer_direction": "Direction.",
        "key_points": ["not an object", {"text": "Valid", "claim_type": "guidance"}],
    }
    answer = parse_answer(payload, EVIDENCE)

    assert len(answer.key_points) == 1
    assert answer.key_points[0].text == "Valid"


def test_cited_evidence_is_deduplicated_in_order() -> None:
    answer = _answer(
        KeyPoint("A", ClaimType.CANDIDATE_FACT, ["ach_1"]),
        KeyPoint("B", ClaimType.CANDIDATE_FACT, ["exp_1", "ach_1"]),
    )
    assert answer.cited_evidence_ids == ["ach_1", "exp_1"]


# ── Answer usability (PRD §14.5) ─────────────────────────────────────


def test_a_direction_that_restates_the_question_is_dropped() -> None:
    """The candidate just heard the question; echoing it wastes the top line."""
    question = "Tell me about a time you led incident response on a production outage."
    answer = parse_answer(
        {
            "answer_direction": "Tell me about a time you led incident response on an outage.",
            "key_points": [{"text": "Reduced MTTR to 9 minutes", "claim_type": "guidance"}],
        },
        [],
        question=question,
    )

    assert answer.answer_direction == ""


def test_a_real_direction_survives() -> None:
    question = "Tell me about a time you led incident response on a production outage."
    answer = parse_answer(
        {
            "answer_direction": "Open with the payments outage you ran point on last year.",
            "key_points": [{"text": "Rolled back, then cached", "claim_type": "guidance"}],
        },
        [],
        question=question,
    )

    assert answer.answer_direction.startswith("Open with the payments outage")


def test_a_mode_that_wants_structure_always_gets_one() -> None:
    """A candidate who has gone blank needs a shape more than anything else."""
    answer = enforce_mode(
        Answer(answer_direction="Start with the outage.", key_points=[], structure=None),
        ResponseMode.STAR,
    )

    assert answer.structure == "Situation → Task → Action → Result"


def test_a_mode_that_suppresses_structure_still_suppresses_it() -> None:
    answer = enforce_mode(
        Answer(answer_direction="", key_points=[], structure="Situation → Result"),
        ResponseMode.TALKING_POINTS,
    )

    assert answer.structure is None
