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
    assert payload["max_completion_tokens"] == meta_client.MAX_OUTPUT_TOKENS
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
