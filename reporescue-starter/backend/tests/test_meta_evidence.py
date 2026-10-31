"""Tests for real-repository Meta diagnosis requests and patch-set parsing."""

import io
import json
import urllib.error
import urllib.request

import pytest

from app import meta_client
from app.meta_client import (
    MetaAPIError,
    MetaRateLimitError,
    MetaResponseError,
    build_evidence_messages,
    diagnose_and_propose_from_evidence,
)

FAKE_KEY = "test-key-not-real"

EVIDENCE = {
    "pkg/calc.py": "def add(a, b):\n    return a - b\n",
    "tests/test_calc.py": "def test_add():\n    assert add(2, 3) == 5\n",
}
FAILURES = [
    {
        "test": "tests/test_calc.py::test_add",
        "kind": "failed",
        "message": "assert -1 == 5",
        "traceback": "E   assert -1 == 5",
    }
]

VALID_MULTI = {
    "diagnosis": "add subtracts instead of adding.",
    "reason": "Fix the operator.",
    "edits": [{"file": "pkg/calc.py", "before": "    return a - b", "after": "    return a + b"}],
}
VALID_SINGLE = {
    "diagnosis": "add subtracts instead of adding.",
    "reason": "Fix the operator.",
    "file": "pkg/calc.py",
    "before": "    return a - b",
    "after": "    return a + b",
}


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    monkeypatch.delenv("REPORESCUE_AI_MODE", raising=False)
    monkeypatch.setenv("META_API_KEY", FAKE_KEY)


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
        seen["auth"] = request.get_header("Authorization")
        seen["timeout"] = timeout
        if isinstance(response, Exception):
            raise response
        return _FakeResponse(response)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return seen


def _ok(content):
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}


def _call(**kwargs):
    params = {"evidence_files": EVIDENCE, "failures": FAILURES}
    params.update(kwargs)
    return diagnose_and_propose_from_evidence(**params)


def test_success_parses_multi_edit_patchset(monkeypatch):
    seen = _capture(monkeypatch, _ok(json.dumps(VALID_MULTI)))

    diagnosis, patchset = _call()

    assert diagnosis == VALID_MULTI["diagnosis"]
    assert len(patchset.edits) == 1
    assert patchset.edits[0].file == "pkg/calc.py"
    assert patchset.reason == "Fix the operator."
    assert seen["auth"] == f"Bearer {FAKE_KEY}"
    payload = json.loads(seen["data"].decode("utf-8"))
    assert set(payload) == {"model", "messages", "max_completion_tokens", "reasoning_effort"}
    user_content = payload["messages"][1]["content"]
    assert "pkg/calc.py" in user_content
    assert "tests/test_calc.py::test_add" in user_content
    assert "assert -1 == 5" in user_content


def test_success_parses_legacy_single_shape(monkeypatch):
    _capture(monkeypatch, _ok(json.dumps(VALID_SINGLE)))

    _, patchset = _call()

    assert len(patchset.edits) == 1
    assert patchset.edits[0].after == "    return a + b"


def test_rejects_missing_diagnosis(monkeypatch):
    bad = dict(VALID_MULTI)
    del bad["diagnosis"]
    _capture(monkeypatch, _ok(json.dumps(bad)))

    with pytest.raises(MetaResponseError, match="diagnosis"):
        _call()


def test_rejects_empty_edits(monkeypatch):
    bad = {**VALID_MULTI, "edits": []}
    _capture(monkeypatch, _ok(json.dumps(bad)))

    with pytest.raises(MetaResponseError, match="empty 'edits'"):
        _call()


def test_rejects_too_many_edits(monkeypatch):
    bad = {
        **VALID_MULTI,
        "edits": [
            {"file": f"pkg/f{i}.py", "before": "a", "after": "b"}
            for i in range(meta_client.MAX_PATCHSET_EDITS + 1)
        ],
    }
    _capture(monkeypatch, _ok(json.dumps(bad)))

    with pytest.raises(MetaResponseError, match="maximum"):
        _call()


def test_rejects_non_string_edit_values(monkeypatch):
    bad = {**VALID_MULTI, "edits": [{"file": "pkg/calc.py", "before": {"x": 1}, "after": "b"}]}
    _capture(monkeypatch, _ok(json.dumps(bad)))

    with pytest.raises(MetaResponseError, match="must be a string"):
        _call()


def test_rejects_empty_before_block(monkeypatch):
    bad = {**VALID_MULTI, "edits": [{"file": "pkg/calc.py", "before": "", "after": "b"}]}
    _capture(monkeypatch, _ok(json.dumps(bad)))

    with pytest.raises(MetaResponseError, match="empty 'before'"):
        _call()


def test_rejects_neither_shape(monkeypatch):
    _capture(monkeypatch, _ok(json.dumps({"diagnosis": "d", "reason": "r"})))

    with pytest.raises(MetaResponseError, match="'edits' array"):
        _call()


def test_rate_limit_propagates(monkeypatch):
    error = urllib.error.HTTPError(
        meta_client.API_URL, 429, "limited", {}, io.BytesIO(b"slow down")
    )
    _capture(monkeypatch, error)

    with pytest.raises(MetaRateLimitError):
        _call()


def test_api_error_propagates_without_key(monkeypatch):
    error = urllib.error.HTTPError(
        meta_client.API_URL, 500, "broken", {}, io.BytesIO(b"oops")
    )
    _capture(monkeypatch, error)

    with pytest.raises(MetaAPIError) as exc_info:
        _call()
    assert FAKE_KEY not in str(exc_info.value)


def test_evidence_messages_bounded():
    big_files = {f"pkg/mod{i}.py": "x = 1\n" * 500 for i in range(20)}
    messages = build_evidence_messages(evidence_files=big_files, failures=FAILURES * 20)

    user_content = messages[1]["content"]
    assert len(user_content) <= (
        meta_client.MAX_EVIDENCE_CHARS + meta_client.MAX_FAILURE_CHARS + 4000
    )
    assert "[truncated" in user_content
    assert messages[0]["content"].startswith("You are a careful code-repair")


def test_evidence_messages_include_feedback():
    messages = build_evidence_messages(
        evidence_files=EVIDENCE, failures=FAILURES, feedback="do not repeat the no-op"
    )
    assert "do not repeat the no-op" in messages[1]["content"]
