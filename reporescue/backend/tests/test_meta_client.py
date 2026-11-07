import io
import json
import urllib.error
import urllib.request

import pytest

from app import meta_client
from app.meta_client import (
    MetaAPIError,
    MetaConfigError,
    MetaRateLimitError,
    MetaRefusalError,
    MetaResponseError,
)

FAKE_KEY = "test-key-not-real"

VALID_OUTPUT = {
    "diagnosis": "final_price returns a negative value when discount exceeds price.",
    "file": "src/pricing.py",
    "before": "    return price - discount",
    "after": "    return max(0, price - discount)",
    "reason": "Clamp the final price at zero.",
}


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    monkeypatch.delenv("REPORESCUE_AI_MODE", raising=False)
    monkeypatch.delenv("META_API_KEY", raising=False)


@pytest.fixture
def workspace(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "pricing.py").write_text(
        "def final_price(price, discount):\n    return price - discount\n",
        encoding="utf-8",
    )
    return tmp_path


def _baseline():
    return {"passed": False, "exit_code": 1, "output": "FAILED tests/test_pricing.py"}


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ok_payload(content, finish_reason="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}


def _capture(monkeypatch, response):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["data"] = request.data
        seen["auth"] = request.get_header("Authorization")
        # urllib stores this header capitalized as "Content-type"; the wire
        # value is identical since HTTP header names are case-insensitive.
        seen["content_type"] = request.get_header("Content-type")
        seen["method"] = request.get_method()
        seen["timeout"] = timeout
        if isinstance(response, Exception):
            raise response
        return _FakeResponse(response)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return seen


def test_stub_propose_patch_is_unchanged():
    proposal = meta_client.propose_patch(["src/pricing.py"])
    assert proposal.file == "src/pricing.py"
    assert proposal.before == "    return price - discount"
    assert "max(0" in proposal.after


def test_ai_mode_defaults_to_stub():
    assert meta_client.get_ai_mode() == "stub"


def test_ai_mode_accepts_meta_case_insensitive(monkeypatch):
    monkeypatch.setenv("REPORESCUE_AI_MODE", " META ")
    assert meta_client.get_ai_mode() == "meta"


def test_ai_mode_rejects_unknown_value(monkeypatch):
    monkeypatch.setenv("REPORESCUE_AI_MODE", "bogus")
    with pytest.raises(MetaConfigError, match="expected 'stub' or 'meta'"):
        meta_client.get_ai_mode()


def test_api_key_missing_raises_without_exposing_value():
    with pytest.raises(MetaConfigError) as exc_info:
        meta_client.get_api_key()
    assert "META_API_KEY" in str(exc_info.value)
    assert FAKE_KEY not in str(exc_info.value)


def test_diagnose_success_sends_bounded_request(monkeypatch, workspace):
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    before = (workspace / "src" / "pricing.py").read_text(encoding="utf-8")
    seen = _capture(monkeypatch, _ok_payload(json.dumps(VALID_OUTPUT)))

    diagnosis, proposal = meta_client.diagnose_and_propose(
        workspace=workspace, baseline=_baseline()
    )

    assert diagnosis == VALID_OUTPUT["diagnosis"]
    assert proposal.to_dict() == {
        key: VALID_OUTPUT[key] for key in ("file", "before", "after", "reason")
    }
    assert seen["url"] == meta_client.API_URL
    assert seen["auth"] == f"Bearer {FAKE_KEY}"
    payload = json.loads(seen["data"].decode("utf-8"))
    # Exact key set: the live endpoint rejected max_tokens/temperature/
    # response_format with HTTP 400. Do not re-add them without a live check.
    assert set(payload) == {
        "model",
        "messages",
        "max_completion_tokens",
        "reasoning_effort",
    }
    assert payload["model"] == "muse-spark-1.3"
    assert payload["reasoning_effort"] == "minimal"
    assert payload["max_completion_tokens"] == meta_client.MAX_REPAIR_TOKENS
    assert "return price - discount" in payload["messages"][1]["content"]
    assert "FAILED tests/test_pricing.py" in payload["messages"][1]["content"]
    assert (workspace / "src" / "pricing.py").read_text(encoding="utf-8") == before


def test_diagnose_parses_fenced_json(monkeypatch, workspace):
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    fenced = "```json\n" + json.dumps(VALID_OUTPUT) + "\n```"
    _capture(monkeypatch, _ok_payload(fenced))

    _, proposal = meta_client.diagnose_and_propose(
        workspace=workspace, baseline=_baseline()
    )
    assert proposal.file == "src/pricing.py"


def test_diagnose_parses_json_embedded_in_prose(monkeypatch, workspace):
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    content = "Here is my analysis:\n" + json.dumps(VALID_OUTPUT) + "\nHope this helps."
    _capture(monkeypatch, _ok_payload(content))

    diagnosis, proposal = meta_client.diagnose_and_propose(
        workspace=workspace, baseline=_baseline()
    )
    assert diagnosis == VALID_OUTPUT["diagnosis"]
    assert proposal.file == "src/pricing.py"


def _http_error(code, body, headers=None):
    return urllib.error.HTTPError(
        meta_client.API_URL, code, "error", headers or {}, io.BytesIO(body)
    )


def test_diagnose_rate_limit_is_distinct(monkeypatch, workspace):
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    _capture(monkeypatch, _http_error(429, b'{"error": {"message": "slow down"}}'))

    with pytest.raises(MetaRateLimitError, match="HTTP 429") as exc_info:
        meta_client.diagnose_and_propose(workspace=workspace, baseline=_baseline())
    assert "slow down" in str(exc_info.value)
    assert FAKE_KEY not in str(exc_info.value)


def test_diagnose_http_error_reports_status(monkeypatch, workspace):
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    _capture(monkeypatch, _http_error(401, b"unauthorized"))

    with pytest.raises(MetaAPIError, match="HTTP 401") as exc_info:
        meta_client.diagnose_and_propose(workspace=workspace, baseline=_baseline())
    assert exc_info.value.status_code == 401
    assert FAKE_KEY not in str(exc_info.value)


def test_diagnose_request_shape(monkeypatch, workspace):
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    seen = _capture(monkeypatch, _ok_payload(json.dumps(VALID_OUTPUT)))

    meta_client.diagnose_and_propose(workspace=workspace, baseline=_baseline())

    assert seen["method"] == "POST"
    assert seen["url"] == meta_client.API_URL
    assert seen["content_type"] == "application/json"
    assert seen["auth"] == f"Bearer {FAKE_KEY}"
    assert seen["timeout"] == meta_client.REQUEST_TIMEOUT_SECONDS
    json.loads(seen["data"].decode("utf-8"))


def test_diagnose_html_error_reports_text_not_markup(monkeypatch, workspace):
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    body = (
        b"<!DOCTYPE html><html><head><title>Bad Request</title></head>"
        b"<body><h1>4xx Client Error</h1><p>Unsupported parameter.</p></body></html>"
    )
    _capture(
        monkeypatch,
        _http_error(400, body, {"Content-Type": "text/html; charset=utf-8"}),
    )

    with pytest.raises(MetaAPIError, match=r"HTTP 400, text/html") as exc_info:
        meta_client.diagnose_and_propose(workspace=workspace, baseline=_baseline())
    message = str(exc_info.value)
    assert exc_info.value.status_code == 400
    assert "4xx Client Error" in message
    assert "Unsupported parameter." in message
    assert "<h1>" not in message
    assert "<!DOCTYPE" not in message
    assert FAKE_KEY not in message


def test_diagnose_json_error_prefers_envelope_message(monkeypatch, workspace):
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    body = json.dumps({"error": {"message": "Model not available."}}).encode("utf-8")
    _capture(
        monkeypatch, _http_error(400, body, {"Content-Type": "application/json"})
    )

    with pytest.raises(MetaAPIError, match="Model not available."):
        meta_client.diagnose_and_propose(workspace=workspace, baseline=_baseline())


def test_diagnose_redacts_key_from_error_body(monkeypatch, workspace):
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    body = f"rejected request for key {FAKE_KEY} at edge".encode("utf-8")
    _capture(monkeypatch, _http_error(400, body))

    with pytest.raises(MetaAPIError) as exc_info:
        meta_client.diagnose_and_propose(workspace=workspace, baseline=_baseline())
    assert FAKE_KEY not in str(exc_info.value)
    assert "[REDACTED]" in str(exc_info.value)


def test_diagnose_truncates_long_error_body(monkeypatch, workspace):
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    _capture(monkeypatch, _http_error(400, b"z" * 3000))

    with pytest.raises(MetaAPIError) as exc_info:
        meta_client.diagnose_and_propose(workspace=workspace, baseline=_baseline())
    detail = str(exc_info.value).split("Details: ", 1)[1]
    assert len(detail) <= meta_client.ERROR_DETAIL_CHARS


def test_diagnose_network_failure_has_no_status(monkeypatch, workspace):
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    _capture(monkeypatch, urllib.error.URLError("connection refused"))

    with pytest.raises(MetaAPIError, match="could not reach") as exc_info:
        meta_client.diagnose_and_propose(workspace=workspace, baseline=_baseline())
    assert exc_info.value.status_code is None


def test_diagnose_rejects_invalid_json(monkeypatch, workspace):
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    _capture(monkeypatch, _ok_payload("not json at all"))

    with pytest.raises(MetaResponseError, match="not valid JSON"):
        meta_client.diagnose_and_propose(workspace=workspace, baseline=_baseline())


def test_diagnose_rejects_missing_keys(monkeypatch, workspace):
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    _capture(monkeypatch, _ok_payload(json.dumps({"diagnosis": "x"})))

    with pytest.raises(MetaResponseError, match="missing keys"):
        meta_client.diagnose_and_propose(workspace=workspace, baseline=_baseline())


def test_diagnose_rejects_non_string_values(monkeypatch, workspace):
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    bad = dict(VALID_OUTPUT, after={"code": "x"})
    _capture(monkeypatch, _ok_payload(json.dumps(bad)))

    with pytest.raises(MetaResponseError, match="must be a string"):
        meta_client.diagnose_and_propose(workspace=workspace, baseline=_baseline())


def test_diagnose_rejects_empty_choices(monkeypatch, workspace):
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    _capture(monkeypatch, {"choices": []})

    with pytest.raises(MetaResponseError, match="no choices"):
        meta_client.diagnose_and_propose(workspace=workspace, baseline=_baseline())


def test_diagnose_truncates_large_context(monkeypatch, workspace):
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    (workspace / "src" / "big.py").write_text("x = 1\n" * 20000, encoding="utf-8")
    seen = _capture(monkeypatch, _ok_payload(json.dumps(VALID_OUTPUT)))

    meta_client.diagnose_and_propose(workspace=workspace, baseline=_baseline())

    payload = json.loads(seen["data"].decode("utf-8"))
    user_content = payload["messages"][1]["content"]
    assert len(user_content) < meta_client.MAX_CONTEXT_CHARS + 8000
    assert "[truncated" in user_content


def test_diagnose_extracts_text_content_blocks(monkeypatch, workspace):
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    blocks = [
        {"type": "reasoning", "text": "internal deliberation"},
        {"type": "text", "text": json.dumps(VALID_OUTPUT)},
    ]
    _capture(monkeypatch, _ok_payload(blocks))

    diagnosis, proposal = meta_client.diagnose_and_propose(
        workspace=workspace, baseline=_baseline()
    )
    assert diagnosis == VALID_OUTPUT["diagnosis"]
    assert proposal.file == "src/pricing.py"


def test_extract_content_joins_text_blocks():
    content, finish = meta_client._extract_content(
        {
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "text", "text": "hello"},
                            {"type": "tool_use", "name": "ignored"},
                            {"type": "text", "text": "world"},
                        ]
                    },
                    "finish_reason": "stop",
                }
            ]
        }
    )
    assert content == "hello\nworld"
    assert finish == "stop"


def test_extract_content_rejects_non_text_blocks():
    with pytest.raises(MetaResponseError, match="no usable text"):
        meta_client._extract_content(
            {
                "choices": [
                    {
                        "message": {"content": [{"type": "image"}]},
                        "finish_reason": "stop",
                    }
                ]
            }
        )


def test_extract_content_refusal_raises():
    with pytest.raises(MetaRefusalError, match="refused"):
        meta_client._extract_content(
            {
                "choices": [
                    {
                        "message": {"refusal": "I cannot help.", "content": ""},
                        "finish_reason": "stop",
                    }
                ]
            }
        )


def test_extract_content_filter_raises():
    with pytest.raises(MetaRefusalError, match="content filtering"):
        meta_client._extract_content(
            {
                "choices": [
                    {
                        "message": {"content": ""},
                        "finish_reason": "content_filter",
                    }
                ]
            }
        )


def test_extract_content_empty_truncated_names_token_limit():
    with pytest.raises(MetaResponseError, match="truncated by the output-token limit"):
        meta_client._extract_content(
            {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
        )


def test_extract_content_missing_message_keeps_legacy_message():
    with pytest.raises(MetaResponseError, match="no message content"):
        meta_client._extract_content({"choices": [{"finish_reason": "stop"}]})


def test_extract_content_error_envelope_raises_api_error():
    with pytest.raises(MetaAPIError, match="error envelope"):
        meta_client._extract_content({"error": {"message": "Model overloaded."}})


def test_diagnose_refusal_raises_without_retry(monkeypatch, workspace):
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    _capture(
        monkeypatch,
        {
            "choices": [
                {
                    "message": {"refusal": "No.", "content": ""},
                    "finish_reason": "stop",
                }
            ]
        },
    )
    with pytest.raises(MetaRefusalError):
        meta_client.diagnose_and_propose(
            workspace=workspace, baseline=_baseline()
        )


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (None, "api_error"),
        (408, "api_error"),
        (500, "api_error"),
        (503, "api_error"),
        (400, "api_rejected"),
        (401, "provider_auth"),
        (403, "provider_auth"),
        (404, "model_not_found"),
        (422, "api_rejected"),
    ],
)
def test_api_error_reason(status, reason):
    assert (
        meta_client.api_error_reason(MetaAPIError("x", status_code=status))
        == reason
    )


def test_api_error_reason_rate_limit():
    assert (
        meta_client.api_error_reason(MetaRateLimitError("slow")) == "rate_limit"
    )


def test_classify_success_sends_bounded_request(monkeypatch):
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    seen = _capture(
        monkeypatch,
        _ok_payload(
            json.dumps(
                {
                    "mode": "debug",
                    "confidence": "high",
                    "rationale": "A bug is described.",
                    "question": None,
                }
            )
        ),
    )
    result = meta_client.classify_objective(objective="the total is incorrect")
    assert result == {
        "selected_mode": "debug",
        "rationale": "A bug is described.",
        "confidence": "high",
    }
    assert seen["url"] == meta_client.API_URL
    payload = json.loads(seen["data"].decode("utf-8"))
    assert set(payload) == {
        "model",
        "messages",
        "max_completion_tokens",
        "reasoning_effort",
    }
    assert payload["max_completion_tokens"] == meta_client.MAX_CLASSIFY_TOKENS
    assert "the total is incorrect" in payload["messages"][1]["content"]
    assert FAKE_KEY not in json.dumps(payload)


def test_classify_clarification(monkeypatch):
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    _capture(
        monkeypatch,
        _ok_payload(
            json.dumps(
                {
                    "mode": None,
                    "confidence": "none",
                    "rationale": "Too vague.",
                    "question": "What failed?",
                }
            )
        ),
    )
    result = meta_client.classify_objective(objective="look")
    assert result["selected_mode"] is None
    assert result["reason"] == "needs_clarification"
    assert result["question"] == "What failed?"


@pytest.mark.parametrize(
    "bad",
    [
        {"mode": "bogus", "confidence": "high", "rationale": "x", "question": None},
        {"mode": "debug", "confidence": "bogus", "rationale": "x", "question": None},
        {"mode": "debug", "confidence": "high", "rationale": "", "question": None},
        {"mode": "debug", "confidence": "high", "question": None},
        {"mode": None, "confidence": "high", "rationale": "x", "question": "q?"},
        {"mode": None, "confidence": "none", "rationale": "x", "question": None},
    ],
)
def test_classify_rejects_invalid_shapes(monkeypatch, bad):
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)
    _capture(monkeypatch, _ok_payload(json.dumps(bad)))
    with pytest.raises(MetaResponseError):
        meta_client.classify_objective(objective="x")
