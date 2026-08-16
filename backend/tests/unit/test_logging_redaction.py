"""Log redaction and pseudonymization (PRD §30.1).

Interview content must never reach a log sink. These tests assert the
processors directly, because the guarantee is a privacy control, not a
formatting preference.
"""

from __future__ import annotations

from verity.platform.logging import _inject_context, _redact, pseudonymize, set_request_context


def test_content_keys_are_redacted() -> None:
    event = {
        "event": "suggestion_generated",
        "transcript": "Tell me about a production incident you handled",
        "answer_text": "At my last role I led the response to...",
        "resume_text": "John Doe, Senior Engineer",
        "question_count": 4,
    }
    result = _redact(None, "info", dict(event))

    assert "production incident" not in str(result)
    assert "John Doe" not in str(result)
    assert result["transcript"].startswith("<redacted:")
    assert result["answer_text"].startswith("<redacted:")
    assert result["question_count"] == 4  # non-content fields survive


def test_credentials_are_redacted() -> None:
    event = {"password": "hunter2", "access_token": "eyJhbGc", "api_key": "sk-live-123"}
    result = _redact(None, "info", event)
    for value in result.values():
        assert "hunter2" not in str(value)
        assert "sk-live" not in str(value)


def test_redaction_is_case_insensitive() -> None:
    result = _redact(None, "info", {"Transcript": "sensitive", "API_KEY": "secret"})
    assert result["Transcript"].startswith("<redacted:")
    assert result["API_KEY"].startswith("<redacted:")


def test_non_string_content_is_redacted_without_length_leak() -> None:
    result = _redact(None, "info", {"content": {"nested": "secret"}})
    assert result["content"] == "<redacted>"


def test_pseudonymize_is_stable_and_non_reversible() -> None:
    user_id = "018f3c9e-7a2b-7000-8000-000000000001"
    first = pseudonymize(user_id)
    assert first == pseudonymize(user_id)
    assert user_id not in first
    assert first.startswith("u_")
    assert pseudonymize("other") != first


def test_context_injection_pseudonymizes_user_id() -> None:
    set_request_context(request_id="req_1", session_id="ls_1", user_id="user-abc")
    result = _inject_context(None, "info", {"event": "x"})

    assert result["request_id"] == "req_1"
    assert result["session_id"] == "ls_1"
    assert result["user_ref"] == pseudonymize("user-abc")
    assert "user-abc" not in str(result)
