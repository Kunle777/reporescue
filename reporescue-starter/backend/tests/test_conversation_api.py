"""Tests for investigation-scoped conversation over persisted evidence."""

from __future__ import annotations

import json

import pytest

from conftest import ALICE, BOB
from app import store

INVESTIGATION_ID = "inv-conv"


def _seed_job(**overrides):
    now = store.utcnow_iso()
    row = {
        "job_id": "job-conv",
        "investigation_id": INVESTIGATION_ID,
        "user_id": ALICE.id,
        "ingestion_id": "ing-conv",
        "mode": "debug",
        "max_attempts": 3,
        "status": "completed",
        "progress_pct": 100,
        "repo_name": "acme/calc",
        "repo_url": "https://github.com/acme/calc",
        "commit_sha": "c" * 40,
        "repo_ref": None,
        "verification_status": "verified",
        "runs": json.dumps(
            [
                {
                    "kind": "baseline",
                    "backend": "fake-test",
                    "status": "test_failure",
                    "passed": False,
                    "exit_code": 1,
                    "output": "1 failed",
                    "failures": [
                        {
                            "test": "tests/test_calc.py::test_add",
                            "kind": "failed",
                            "message": "assert -1 == 5",
                            "traceback": '  File "pkg/calc.py", line 2, in add',
                        }
                    ],
                    "duration_ms": 3,
                },
                {
                    "kind": "verification",
                    "backend": "fake-test",
                    "status": "ok",
                    "passed": True,
                    "exit_code": 0,
                    "output": "1 passed",
                    "failures": [],
                    "duration_ms": 3,
                },
            ]
        ),
        "findings": json.dumps(
            [
                {
                    "kind": "diagnosis",
                    "category": None,
                    "title": "AI diagnosis",
                    "detail": "add subtracts instead of adding.",
                    "confidence": "confirmed",
                }
            ]
        ),
        "patches": json.dumps(
            [
                {
                    "status": "applied",
                    "reason": "Fix the operator.",
                    "files": [
                        {
                            "file": "pkg/calc.py",
                            "before": "    return a - b",
                            "after": "    return a + b",
                        }
                    ],
                }
            ]
        ),
        "final_report": json.dumps(
            {
                "status": "complete",
                "summary": "Reproduced 1 failing test(s).",
                "verdict": "TESTS_PASSED",
            }
        ),
        "created_at": now,
        "updated_at": now,
        "completed_at": now,
    }
    row.update(overrides)
    store.create_job(row)
    return row


def _seed_failed_job():
    now = store.utcnow_iso()
    return _seed_job(
        job_id="job-failed",
        investigation_id="inv-failed",
        status="failed",
        verification_status="not_verified",
        error=json.dumps({"reason": "dependency_error", "message": "pip exploded"}),
        runs=json.dumps([]),
        findings=json.dumps([]),
        patches=json.dumps([]),
        final_report=json.dumps(
            {
                "status": "error",
                "reason": "dependency_error",
                "message": "pip exploded",
            }
        ),
        completed_at=now,
    )


def test_history_empty_returns_null_session(client):
    _seed_job()
    body = client.get(f"/api/investigations/{INVESTIGATION_ID}/messages").json()
    assert body == {"session_id": None, "messages": []}


def test_history_missing_and_foreign_404(client, current_user):
    assert client.get("/api/investigations/nope/messages").status_code == 404
    _seed_job()
    current_user["user"] = BOB
    assert (
        client.get(f"/api/investigations/{INVESTIGATION_ID}/messages").status_code
        == 404
    )


def test_post_summary_question_returns_answer(client):
    _seed_job()
    response = client.post(
        f"/api/investigations/{INVESTIGATION_ID}/messages",
        json={"content": "summarize the result"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {
        "session_id",
        "handled_as",
        "detection_reason",
        "user_message",
        "assistant_message",
        "citations_verified",
        "unsupported_references",
    }
    assert body["session_id"] == f"conv-{INVESTIGATION_ID}"
    assert body["handled_as"] == "answer"
    assert body["detection_reason"] == "matched: summary"
    assert body["citations_verified"] is True
    assert body["unsupported_references"] == []
    assert body["user_message"]["content"] == "summarize the result"
    assert body["assistant_message"]["role"] == "assistant"
    assert "TESTS_PASSED" in body["assistant_message"]["content"]


def test_post_findings_question_cites_evidence(client):
    _seed_job()
    body = client.post(
        f"/api/investigations/{INVESTIGATION_ID}/messages",
        json={"content": "why did it fail?"},
    ).json()
    assert body["handled_as"] == "answer"
    assert body["detection_reason"] == "matched: findings"
    assert "subtracts instead of adding" in body["assistant_message"]["content"]


def test_post_runs_question_lists_failures(client):
    _seed_job()
    body = client.post(
        f"/api/investigations/{INVESTIGATION_ID}/messages",
        json={"content": "show me the test runs"},
    ).json()
    assert body["handled_as"] == "answer"
    assert "tests/test_calc.py::test_add" in body["assistant_message"]["content"]


def test_post_patches_question_when_none_is_guidance(client):
    _seed_job(patches=json.dumps([]))
    body = client.post(
        f"/api/investigations/{INVESTIGATION_ID}/messages",
        json={"content": "what patches were applied?"},
    ).json()
    assert body["handled_as"] == "guidance"
    assert body["citations_verified"] is False
    assert "No patches" in body["assistant_message"]["content"]


def test_post_unknown_topic_returns_guidance(client):
    _seed_job()
    body = client.post(
        f"/api/investigations/{INVESTIGATION_ID}/messages",
        json={"content": "hello there"},
    ).json()
    assert body["handled_as"] == "guidance"
    assert body["detection_reason"] == "no evidence match"
    assert "persisted evidence" in body["assistant_message"]["content"]


def test_post_detects_unsupported_references(client):
    _seed_job()
    body = client.post(
        f"/api/investigations/{INVESTIGATION_ID}/messages",
        json={"content": "what about `src/missing.py` and `pkg/calc.py`?"},
    ).json()
    assert body["unsupported_references"] == [
        {
            "reference": "src/missing.py",
            "reason": "not found in this investigation's persisted evidence",
        }
    ]


def test_post_while_running_409(client):
    _seed_job(status="running", completed_at=None)
    response = client.post(
        f"/api/investigations/{INVESTIGATION_ID}/messages",
        json={"content": "summarize"},
    )
    assert response.status_code == 409


def test_post_empty_and_long_422(client):
    _seed_job()
    url = f"/api/investigations/{INVESTIGATION_ID}/messages"
    assert client.post(url, json={"content": "   "}).status_code == 422
    assert client.post(url, json={"content": "x" * 4001}).status_code == 422


def test_post_missing_and_foreign_404(client, current_user):
    response = client.post(
        "/api/investigations/nope/messages", json={"content": "hi"}
    )
    assert response.status_code == 404
    _seed_job()
    current_user["user"] = BOB
    response = client.post(
        f"/api/investigations/{INVESTIGATION_ID}/messages", json={"content": "hi"}
    )
    assert response.status_code == 404


def test_history_returns_turns_oldest_first(client):
    _seed_job()
    url = f"/api/investigations/{INVESTIGATION_ID}/messages"
    client.post(url, json={"content": "summarize the result"})
    client.post(url, json={"content": "why did it fail?"})
    body = client.get(url).json()
    assert body["session_id"] == f"conv-{INVESTIGATION_ID}"
    assert [message["role"] for message in body["messages"]] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
    assert body["messages"][0]["content"] == "summarize the result"
    assert len(client.get(f"{url}?limit=1").json()["messages"]) == 1


def test_failed_job_error_question(client):
    _seed_failed_job()
    body = client.post(
        "/api/investigations/inv-failed/messages",
        json={"content": "why did it fail?"},
    ).json()
    assert body["handled_as"] == "answer"
    assert body["detection_reason"] == "matched: error"
    assert "dependency_error" in body["assistant_message"]["content"]


def test_messages_require_auth(anon_client):
    _seed_job()
    url = f"/api/investigations/{INVESTIGATION_ID}/messages"
    assert anon_client.get(url).status_code == 401
    assert anon_client.post(url, json={"content": "hi"}).status_code == 401
