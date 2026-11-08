"""Secret-redaction regression tests.

Every outbound AI payload must have credential-like values replaced with
``[REDACTED]``. These tests pin the matcher behavior (patterns must
actually match) and prove redaction at the provider boundary and on
model outputs before persistence.
"""
import json
import urllib.request

import pytest

from app import meta_client
from app.redact import redact_data, redact_text, sanitize_model_text


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    monkeypatch.delenv("REPORESCUE_AI_MODE", raising=False)
    monkeypatch.delenv("META_API_KEY", raising=False)


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capture(monkeypatch, response):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["data"] = request.data
        if isinstance(response, Exception):
            raise response
        return _FakeResponse(response)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return seen


def _ok_payload(content):
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}


def test_api_key_assignments_redacted():
    for raw in (
        "api_key=sk-live-abc123",
        "api-key: sk-live-abc123",
        "API_KEY = sk-live-abc123",
        "ApiKey=sk-live-abc123",
    ):
        cleaned = redact_text(raw)
        assert "sk-live-abc123" not in cleaned
        assert "[REDACTED]" in cleaned


def test_access_and_refresh_tokens_redacted():
    cleaned = redact_text("access_token=ya29.secret-value_here; refresh_token: rt-9z81")
    assert "ya29.secret-value_here" not in cleaned
    assert "rt-9z81" not in cleaned
    assert cleaned.count("[REDACTED]") == 2


def test_bearer_tokens_redacted_case_insensitive():
    cleaned = redact_text("headers: bearer eyJhbGciOiJIUzI1NiJ9.payload.sig")
    assert "eyJhbGciOiJIUzI1NiJ9" not in cleaned
    assert "Bearer [REDACTED]" in cleaned


def test_passwords_redacted():
    for raw in ("password=hunter2-secret", "Password: hunter2-secret", "db_password = hunter2-secret"):
        cleaned = redact_text(raw)
        assert "hunter2-secret" not in cleaned
        assert "[REDACTED]" in cleaned


def test_database_connection_strings_redacted():
    raw = "postgres://appuser:S3cret-pw@db.internal:5432/reporescue"
    cleaned = redact_text(f"connect {raw} for data")
    assert "S3cret-pw" not in cleaned
    assert "appuser:[REDACTED]@db.internal" in cleaned
    assert "for data" in cleaned


def test_env_style_secrets_redacted():
    raw = "META_API_KEY=meta-secret-1\nSTRIPE_SECRET_KEY = sk_test_456\nDEBUG=false"
    cleaned = redact_text(raw)
    assert "meta-secret-1" not in cleaned
    assert "sk_test_456" not in cleaned
    assert "DEBUG=false" in cleaned


def test_authorization_headers_redacted():
    cleaned = redact_text("Authorization: Bearer tok-abc-123")
    assert "tok-abc-123" not in cleaned
    assert "[REDACTED]" in cleaned
    basic = redact_text("authorization: Basic dXNlcjpwYXNz")
    assert "dXNlcjpwYXNz" not in basic
    assert "[REDACTED]" in basic


def test_private_key_material_redacted():
    pem = (
        "-----BEGIN PRIVATE KEY-----\n"
        "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n"
        "-----END PRIVATE KEY-----"
    )
    cleaned = redact_text(f"deploy key:\n{pem}\nend")
    assert "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" not in cleaned
    assert "PRIVATE KEY" in cleaned or "[REDACTED" in cleaned
    assert "deploy key" in cleaned and "end" in cleaned


def test_github_tokens_and_url_userinfo_redacted():
    cleaned = redact_text("token ghp_abcdefgh1234567890 in log")
    assert "ghp_abcdefgh1234567890" not in cleaned
    assert "[REDACTED]" in cleaned
    url = redact_text("clone https://bot:ghp_secretpass@github.com/o/r.git done")
    assert "ghp_secretpass" not in url
    assert "github.com/o/r.git" in url


def test_ordinary_text_preserved():
    raw = "The secret is out: password policies require rotation every 90 days."
    assert redact_text(raw) == raw
    code = "def check(password_policy):\n    return password_policy.days >= 90\n"
    assert redact_text(code) == code


def test_redaction_is_not_truncation_only():
    filler = "lorem ipsum dolor sit amet " * 40
    raw = f"{filler} api_key=sk-hidden-value {filler}"
    cleaned = redact_text(raw)
    assert "sk-hidden-value" not in cleaned
    assert "[REDACTED]" in cleaned
    assert cleaned.startswith("lorem ipsum")
    assert cleaned.count("lorem") > 10


def test_redact_data_walks_nested_structures():
    payload = {
        "question": "why does password=hunter2 fail?",
        "items": [{"output": "token=abc-123"}, "plain", 42, None],
    }
    cleaned = redact_data(payload)
    assert "hunter2" not in json.dumps(cleaned)
    assert "abc-123" not in json.dumps(cleaned)
    assert cleaned["items"][1] == "plain"
    assert cleaned["items"][2] == 42
    # Input is not mutated.
    assert "hunter2" in payload["question"]


def test_outbound_classify_request_never_carries_secrets(monkeypatch):
    seen = _capture(
        monkeypatch,
        _ok_payload(json.dumps({"mode": "debug", "rationale": "stack trace", "confidence": "high"})),
    )
    meta_client.classify_objective(
        objective="fix login where password=hunter2 fails",
        api_key="test-key-not-real",
    )
    body = seen["data"].decode("utf-8")
    assert "hunter2" not in body
    assert "[REDACTED]" in body


def test_outbound_conversation_request_redacts_context_history_question(monkeypatch):
    seen = _capture(
        monkeypatch,
        _ok_payload(json.dumps({"answer": "ok", "evidence_ids": []})),
    )
    meta_client.conversation_reply(
        context={"report": {"summary": "db password=hunter2 leaked"}},
        history=[{"role": "user", "content": "my api_key=sk-abc is broken"}],
        question="is Bearer tok-abc-123 valid?",
        api_key="test-key-not-real",
    )
    body = seen["data"].decode("utf-8")
    assert "hunter2" not in body
    assert "sk-abc" not in body
    assert "tok-abc-123" not in body
    assert "[REDACTED]" in body


def test_model_output_sanitized_before_persistence():
    answer = sanitize_model_text("Use password=hunter2 to reproduce.")
    assert "hunter2" not in answer
    assert "[REDACTED]" in answer
    assert sanitize_model_text("No secrets here.") == "No secrets here."
