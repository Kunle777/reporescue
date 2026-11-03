"""Investigation-scoped read-only conversation over persisted evidence.

The assistant never calls a model and never leaves the investigation: every
answer is composed from the job row's persisted runs, findings, patches,
attempts, and final report. Questions that match no persisted section get a
guidance response pointing at what is available. `` `backticked` `` tokens
in the question that appear nowhere in the evidence are reported as
unsupported references.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from . import job_runner, store
from .auth import AuthenticatedUser, get_current_user

router = APIRouter(
    prefix="/api/investigations/{investigation_id}/messages", tags=["conversation"]
)

MAX_CONTENT_CHARS = 4000
MAX_LISTED = 10
OUTPUT_TAIL_CHARS = 500

BACKTICK_TOKEN = re.compile(r"`([^`\n]{1,120})`")

FAIL_WORDS = ("fail", "error", "wrong", "crash", "broke")
FINDING_WORDS = ("finding", "diagnos", "why", "cause", "reason", "explain")
PATCH_WORDS = ("patch", "change", "diff", "fix", "edit", "appl")
RUN_WORDS = ("test", "run", "execut", "output", "exit", "pass", "baseline", "verif", "backend")
EVIDENCE_WORDS = ("evidence", "traceback", "file", "location", "cite")
SUMMARY_WORDS = ("summar", "status", "result", "verdict", "overview", "conclusion")


class PostMessageRequest(BaseModel):
    content: str


def _session_id(investigation_id: str) -> str:
    return f"conv-{investigation_id}"


def _job_or_404(investigation_id: str, user_id: str) -> dict[str, Any]:
    job = store.get_job_by_investigation(investigation_id, user_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Investigation not found."
        )
    return job


def _public_message(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "role": row["role"],
        "content": row["content"],
        "created_at": row.get("created_at"),
    }


@router.get("")
def get_messages(
    investigation_id: str,
    limit: int = Query(50, ge=1, le=200),
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, Any]:
    job = _job_or_404(investigation_id, user.id)
    rows = store.list_messages(job["investigation_id"], user.id, limit)
    return {
        "session_id": _session_id(job["investigation_id"]) if rows else None,
        "messages": [_public_message(row) for row in rows],
    }


def _evidence_corpus(job: dict[str, Any]) -> str:
    return json.dumps(
        {
            "repo_name": job.get("repo_name"),
            "verification_status": job.get("verification_status"),
            "error": store._load_json(job.get("error"), None),
            "runs": store._load_json(job.get("runs"), []),
            "findings": store._load_json(job.get("findings"), []),
            "patches": store._load_json(job.get("patches"), []),
            "attempts": store._load_json(job.get("attempts"), []),
            "final_report": store._load_json(job.get("final_report"), None),
            "limitations": store._load_json(job.get("limitations"), []),
        },
        default=str,
    ).lower()


def _format_findings(findings: list[dict[str, Any]]) -> str:
    lines = []
    for finding in findings[:MAX_LISTED]:
        title = finding.get("title") or finding.get("kind") or "finding"
        detail = (finding.get("detail") or "").strip()
        line = f"- {title}"
        if detail:
            line += f": {detail[:OUTPUT_TAIL_CHARS]}"
        lines.append(line)
    if len(findings) > MAX_LISTED:
        lines.append(f"- (+{len(findings) - MAX_LISTED} more)")
    return "\n".join(lines)


def _format_runs(runs: list[dict[str, Any]]) -> str:
    lines = []
    for run in runs[:MAX_LISTED]:
        passed = run.get("passed")
        outcome = "passed" if passed is True else ("failed" if passed is False else "unknown")
        lines.append(
            f"- {run.get('kind')}: {outcome} "
            f"(backend {run.get('backend')}, status {run.get('status')}, "
            f"exit {run.get('exit_code')})"
        )
        for failure in (run.get("failures") or [])[:MAX_LISTED]:
            if isinstance(failure, dict):
                lines.append(
                    f"  - {failure.get('test')}: {failure.get('message') or ''}"[:200]
                )
    return "\n".join(lines)


def _answer_question(content: str, job: dict[str, Any]) -> tuple[str, str, bool]:
    """Route one question to a persisted section.

    Returns ``(section, answer_text, cited)`` where ``cited`` is True only
    when the answer quotes non-empty persisted evidence.
    """
    text = content.lower()
    runs = store._load_json(job.get("runs"), []) or []
    findings = store._load_json(job.get("findings"), []) or []
    patches = store._load_json(job.get("patches"), []) or []
    final_report = store._load_json(job.get("final_report"), None) or {}
    error = store._load_json(job.get("error"), None) or {}
    repo = job.get("repo_name") or "this repository"
    status = job.get("status")

    def _has(words: tuple[str, ...]) -> bool:
        return any(word in text for word in words)

    if _has(FAIL_WORDS) and status == "failed" and error:
        return (
            "error",
            f"The investigation of {repo} failed "
            f"({error.get('reason')}): {error.get('message')}",
            True,
        )
    if _has(FINDING_WORDS):
        if findings:
            return (
                "findings",
                f"Findings recorded for {repo}:\n{_format_findings(findings)}",
                True,
            )
        return (
            "findings",
            f"This investigation recorded no findings for {repo}.",
            False,
        )
    if _has(PATCH_WORDS):
        if patches:
            lines = []
            for patch in patches[:MAX_LISTED]:
                files = patch.get("files") or []
                names = ", ".join(
                    f.get("file", "?") for f in files if isinstance(f, dict)
                )
                lines.append(
                    f"- {patch.get('status')}: {names or 'no files'}"
                    + (f" ({patch.get('reason')})" if patch.get("reason") else "")
                )
            return ("patches", f"Patches for {repo}:\n" + "\n".join(lines), True)
        return ("patches", f"No patches were proposed for {repo}.", False)
    if _has(RUN_WORDS):
        if runs:
            return ("runs", f"Test runs for {repo}:\n{_format_runs(runs)}", True)
        return (
            "runs",
            f"No test runs were recorded for {repo} "
            "(static modes execute nothing).",
            False,
        )
    if _has(EVIDENCE_WORDS):
        bits: list[str] = []
        for run in runs[:MAX_LISTED]:
            for failure in (run.get("failures") or [])[:MAX_LISTED]:
                if not isinstance(failure, dict):
                    continue
                trace = (failure.get("traceback") or "").strip()
                head = f"{failure.get('test')}: {failure.get('message') or ''}"
                bits.append(head + (f"\n{trace[:OUTPUT_TAIL_CHARS]}" if trace else ""))
        for patch in patches[:MAX_LISTED]:
            for item in (patch.get("files") or [])[:MAX_LISTED]:
                if isinstance(item, dict) and item.get("file"):
                    bits.append(f"patch file: {item['file']}")
        if bits:
            return ("evidence", "Evidence:\n- " + "\n- ".join(bits[:MAX_LISTED]), True)
        return ("evidence", f"No evidence entries were recorded for {repo}.", False)
    if _has(SUMMARY_WORDS):
        summary = final_report.get("summary") or "No summary was recorded."
        verdict = final_report.get("verdict")
        answer = f"Investigation of {repo} ({status}): {summary}"
        if verdict:
            answer += f" Verdict: {verdict}."
        answer += f" Verification: {job.get('verification_status')}."
        return ("summary", answer, bool(final_report))
    available = [
        name
        for name, items in (
            ("findings", findings),
            ("patches", patches),
            ("runs", runs),
        )
        if items
    ]
    hint = ", ".join(available) if available else "a summary"
    return (
        "overview",
        f"I can only answer from this investigation's persisted evidence "
        f"for {repo} ({status}). Try asking about {hint}.",
        False,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
def post_message(
    investigation_id: str,
    body: PostMessageRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, Any]:
    job = _job_or_404(investigation_id, user.id)
    if job["status"] not in job_runner.TERMINAL_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Investigation is still running; "
            "conversation opens when it completes.",
        )
    content = (body.content or "").strip()
    if not content:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Message content is empty.",
        )
    if len(content) > MAX_CONTENT_CHARS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Message exceeds {MAX_CONTENT_CHARS} characters.",
        )
    corpus = _evidence_corpus(job)
    section, answer_text, cited = _answer_question(content, job)
    unsupported: list[dict[str, str]] = []
    for token in BACKTICK_TOKEN.findall(content)[:MAX_LISTED]:
        cleaned = token.strip()
        if cleaned and cleaned.lower() not in corpus:
            unsupported.append(
                {
                    "reference": cleaned,
                    "reason": "not found in this investigation's persisted evidence",
                }
            )
    now = store.utcnow_iso()
    user_message = {
        "id": uuid.uuid4().hex[:12],
        "investigation_id": job["investigation_id"],
        "user_id": user.id,
        "role": "user",
        "content": content,
        "created_at": now,
    }
    assistant_message = {
        "id": uuid.uuid4().hex[:12],
        "investigation_id": job["investigation_id"],
        "user_id": user.id,
        "role": "assistant",
        "content": answer_text,
        "created_at": now,
    }
    store.add_message(user_message)
    store.add_message(assistant_message)
    return {
        "session_id": _session_id(job["investigation_id"]),
        "handled_as": "answer" if cited else "guidance",
        "detection_reason": f"matched: {section}" if cited else "no evidence match",
        "user_message": _public_message(user_message),
        "assistant_message": _public_message(assistant_message),
        "citations_verified": cited,
        "unsupported_references": unsupported,
    }
