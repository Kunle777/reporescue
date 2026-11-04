from __future__ import annotations

import html
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .patch_guard import FileEdit, PatchProposal, PatchSet

API_URL = "https://api.meta.ai/v1/chat/completions"
MODEL = "muse-spark-1.3"
REASONING_EFFORT = "minimal"
MAX_OUTPUT_TOKENS = 800
REQUEST_TIMEOUT_SECONDS = 60

MAX_CONTEXT_CHARS = 12000
MAX_BASELINE_CHARS = 2000
MAX_FEEDBACK_CHARS = 1500
ERROR_BODY_CHARS = 2000
ERROR_DETAIL_CHARS = 500

# Bounds for real-repository evidence requests (Phase 3).
MAX_EVIDENCE_FILES = 12
MAX_EVIDENCE_CHARS = 8000
MAX_FAILURE_CHARS = 4000
MAX_PATCHSET_EDITS = 5
MAX_EXPLAIN_MESSAGE_CHARS = 2000
MAX_STATIC_MESSAGE_CHARS = 2000

AI_MODE_ENV_VAR = "REPORESCUE_AI_MODE"
API_KEY_ENV_VAR = "META_API_KEY"


class MetaClientError(Exception):
    """Base class for live Meta API client failures."""


class MetaConfigError(MetaClientError):
    """Invalid mode or missing credentials. No request was sent."""


class MetaAPIError(MetaClientError):
    """The API request failed (HTTP error or network failure)."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class MetaRateLimitError(MetaAPIError):
    """The API rejected the request with HTTP 429 (rate limited)."""


class MetaResponseError(MetaClientError):
    """The API response was not usable (bad JSON or schema)."""


def get_ai_mode() -> str:
    """Return 'stub' (default) or 'meta' from the backend environment."""
    mode = os.environ.get(AI_MODE_ENV_VAR, "stub").strip().lower()
    if mode in ("stub", "meta"):
        return mode
    raise MetaConfigError(f"Unknown {AI_MODE_ENV_VAR}={mode!r}; expected 'stub' or 'meta'.")


def get_api_key() -> str:
    """Return the Meta API key from the backend environment.

    The key value is never logged or included in error messages.
    """
    key = os.environ.get(API_KEY_ENV_VAR, "").strip()
    if not key:
        raise MetaConfigError(
            f"{API_KEY_ENV_VAR} is not set; set it in the backend environment "
            "to use meta mode. The key is never logged."
        )
    return key


def propose_patch(source_files: list[str]) -> PatchProposal:
    """Local stub for the Meta model client.

    Returns the deterministic demo patch for the bundled fixture. It makes no
    network calls, performs no paid API calls, and needs no credentials.
    `source_files` is accepted (and currently unused) so the orchestrator
    already passes repository context exactly as the real client will require.
    """
    _ = source_files
    return PatchProposal(
        file="src/pricing.py",
        before="    return price - discount",
        after="    return max(0, price - discount)",
        reason="Clamp the final price at zero so discounts cannot produce a negative charge.",
    )


def diagnose_and_propose(
    *,
    workspace: Path,
    baseline: dict[str, Any],
    feedback: str | None = None,
    api_key: str | None = None,
) -> tuple[str, PatchProposal]:
    """Make one live Meta API call; return (diagnosis, patch proposal).

    Raises MetaConfigError, MetaRateLimitError, MetaAPIError, or
    MetaResponseError. Never returns a stub patch and never exposes the key.
    """
    key = api_key or get_api_key()
    messages = build_messages(workspace=workspace, baseline=baseline, feedback=feedback)
    # Payload converged to the proven parameter set for this endpoint: the live
    # API rejected the earlier shape (max_tokens alias, temperature, and
    # response_format) with HTTP 400, while model + messages +
    # max_completion_tokens + reasoning_effort is confirmed working. JSON
    # discipline stays at the prompt level plus client-side schema validation.
    payload = {
        "model": MODEL,
        "messages": messages,
        "max_completion_tokens": MAX_OUTPUT_TOKENS,
        "reasoning_effort": REASONING_EFFORT,
    }
    data = _post_chat_completion(key, payload)
    content, finish_reason = _extract_content(data)
    parsed = _parse_json_object(content, finish_reason)
    return _to_diagnosis_and_proposal(parsed)


def build_messages(
    *,
    workspace: Path,
    baseline: dict[str, Any],
    feedback: str | None = None,
) -> list[dict[str, str]]:
    """Build the bounded chat messages for one diagnosis/proposal request."""
    context = _render_context(workspace)
    baseline_text = (
        f"passed={baseline.get('passed')} "
        f"exit_code={baseline.get('exit_code')}\n"
        f"{str(baseline.get('output', ''))[-MAX_BASELINE_CHARS:]}"
    )
    user_parts = [
        "Repository source (truncated to fit the request budget):",
        context,
        "Baseline test result (this failure must be fixed):",
        baseline_text,
    ]
    if feedback:
        user_parts += [
            "Feedback on the previous attempt (do not repeat it):",
            feedback[-MAX_FEEDBACK_CHARS:],
        ]
    return [
        {
            "role": "system",
            "content": (
                "You are a careful code-repair assistant. Diagnose the test "
                "failure and propose the smallest source change that fixes it. "
                "Respond with exactly one JSON object and no other text, with "
                "these string keys: diagnosis, file, before, after, reason. "
                "'file' must be a repository-relative path under src/. 'before' "
                "must be the exact lines to replace. 'after' is the replacement. "
                "Do not rewrite unrelated code. Do not modify tests."
            ),
        },
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


def diagnose_and_propose_from_evidence(
    *,
    evidence_files: dict[str, str],
    failures: list[dict[str, str]],
    baseline_tail: str = "",
    feedback: str | None = None,
    api_key: str | None = None,
) -> tuple[str, PatchSet]:
    """Make one live Meta API call for a real repository.

    ``evidence_files`` maps repository-relative paths to bounded source
    text; ``failures`` carries parsed failing tests. Returns (diagnosis,
    patch set). Raises MetaConfigError, MetaRateLimitError, MetaAPIError,
    or MetaResponseError. Never returns a stub patch.
    """
    key = api_key or get_api_key()
    messages = build_evidence_messages(
        evidence_files=evidence_files,
        failures=failures,
        baseline_tail=baseline_tail,
        feedback=feedback,
    )
    payload = {
        "model": MODEL,
        "messages": messages,
        "max_completion_tokens": MAX_OUTPUT_TOKENS,
        "reasoning_effort": REASONING_EFFORT,
    }
    data = _post_chat_completion(key, payload)
    content, finish_reason = _extract_content(data)
    parsed = _parse_json_object(content, finish_reason)
    return _to_diagnosis_and_patchset(parsed)


def build_evidence_messages(
    *,
    evidence_files: dict[str, str],
    failures: list[dict[str, str]],
    baseline_tail: str = "",
    feedback: str | None = None,
) -> list[dict[str, str]]:
    """Build bounded chat messages for a real-repository repair request."""
    context_chunks: list[str] = []
    used = 0
    for path in sorted(evidence_files)[:MAX_EVIDENCE_FILES]:
        chunk = f"--- {path} ---\n{evidence_files[path]}\n"
        if used + len(chunk) > MAX_EVIDENCE_CHARS:
            context_chunks.append("[truncated: remaining evidence omitted to bound request size]")
            break
        context_chunks.append(chunk)
        used += len(chunk)
    context = "\n".join(context_chunks) if context_chunks else "[no source evidence available]"
    failure_lines: list[str] = []
    for failure in failures[:MAX_EVIDENCE_FILES]:
        block = (
            f"{failure.get('test', '?')} [{failure.get('kind', '')}]: "
            f"{failure.get('message', '')}\n{failure.get('traceback', '')}"
        ).strip()
        failure_lines.append(block)
    failure_text = "\n\n".join(failure_lines)[:MAX_FAILURE_CHARS] or "[no parsed failures]"
    user_parts = [
        "Repository evidence (only these files may be patched; never patch tests):",
        context,
        "Failing tests (these failures must be fixed):",
        failure_text,
    ]
    if baseline_tail:
        user_parts += ["Test output tail:", baseline_tail[-MAX_BASELINE_CHARS:]]
    if feedback:
        user_parts += [
            "Feedback on the previous attempt (do not repeat it):",
            feedback[-MAX_FEEDBACK_CHARS:],
        ]
    return [
        {
            "role": "system",
            "content": (
                "You are a careful code-repair assistant. Diagnose the test "
                "failures and propose the smallest source change that fixes "
                "them. Respond with exactly one JSON object and no other "
                "text, with string keys 'diagnosis' and 'reason' plus an "
                "'edits' array of 1 to "
                f"{MAX_PATCHSET_EDITS} objects with string keys 'file', "
                "'before', 'after'. 'file' must be a repository-relative "
                "path to one of the evidence files. 'before' must be the "
                "exact lines to replace. 'after' is the replacement. Never "
                "propose edits to test files, test directories, or "
                "configuration files. Do not rewrite unrelated code."
            ),
        },
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


def explain_from_context(
    *,
    profile_summary: str,
    structure: str,
    excerpts: dict[str, str],
    user_message: str | None = None,
    api_key: str | None = None,
) -> str:
    """Make one live Meta API call explaining a repository; return prose.

    ``excerpts`` maps repository-relative paths to bounded source text.
    Raises MetaConfigError, MetaRateLimitError, MetaAPIError, or
    MetaResponseError for an empty response. Never returns a stub.
    """
    key = api_key or get_api_key()
    messages = build_explain_messages(
        profile_summary=profile_summary,
        structure=structure,
        excerpts=excerpts,
        user_message=user_message,
    )
    payload = {
        "model": MODEL,
        "messages": messages,
        "max_completion_tokens": MAX_OUTPUT_TOKENS,
        "reasoning_effort": REASONING_EFFORT,
    }
    data = _post_chat_completion(key, payload)
    content, _ = _extract_content(data)
    return content.strip()


def build_explain_messages(
    *,
    profile_summary: str,
    structure: str,
    excerpts: dict[str, str],
    user_message: str | None = None,
) -> list[dict[str, str]]:
    """Build bounded chat messages for a static repository explanation."""
    context_chunks: list[str] = []
    used = 0
    for path in sorted(excerpts)[:MAX_EVIDENCE_FILES]:
        chunk = f"--- {path} ---\n{excerpts[path]}\n"
        if used + len(chunk) > MAX_EVIDENCE_CHARS:
            context_chunks.append("[truncated: remaining excerpts omitted to bound request size]")
            break
        context_chunks.append(chunk)
        used += len(chunk)
    context = "\n".join(context_chunks) if context_chunks else "[no source excerpts available]"
    user_parts = [
        "Repository profile:",
        profile_summary,
        "Repository structure:",
        structure,
        "Source excerpts (truncated to fit the request budget):",
        context,
    ]
    if user_message:
        user_parts += [
            "The user asked the following; focus the explanation on it:",
            user_message[:MAX_EXPLAIN_MESSAGE_CHARS],
        ]
    return [
        {
            "role": "system",
            "content": (
                "You are a careful code-reading assistant. Explain what this "
                "repository does, how its main parts fit together, and where "
                "a newcomer should start. Base the explanation only on the "
                "profile, structure, and excerpts provided; do not invent "
                "files, behavior, or dependencies. Respond with plain "
                "Markdown prose and no JSON."
            ),
        },
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


def diagnose_static_from_evidence(
    *,
    evidence_files: dict[str, str],
    user_message: str | None = None,
    api_key: str | None = None,
) -> tuple[str, PatchSet]:
    """Make one live Meta API call for a static-only diagnosis.

    There is no baseline failure: the model reviews the evidence for
    likely defects. Returns (diagnosis, patch set); the edits are
    unverified suggestions, never applied by the static path. Raises
    MetaConfigError, MetaRateLimitError, MetaAPIError, or
    MetaResponseError.
    """
    key = api_key or get_api_key()
    messages = build_static_messages(
        evidence_files=evidence_files,
        user_message=user_message,
    )
    payload = {
        "model": MODEL,
        "messages": messages,
        "max_completion_tokens": MAX_OUTPUT_TOKENS,
        "reasoning_effort": REASONING_EFFORT,
    }
    data = _post_chat_completion(key, payload)
    content, finish_reason = _extract_content(data)
    parsed = _parse_json_object(content, finish_reason)
    return _to_diagnosis_and_patchset(parsed)


def build_static_messages(
    *,
    evidence_files: dict[str, str],
    user_message: str | None = None,
) -> list[dict[str, str]]:
    """Build bounded chat messages for a static-only diagnosis request."""
    context_chunks: list[str] = []
    used = 0
    for path in sorted(evidence_files)[:MAX_EVIDENCE_FILES]:
        chunk = f"--- {path} ---\n{evidence_files[path]}\n"
        if used + len(chunk) > MAX_EVIDENCE_CHARS:
            context_chunks.append("[truncated: remaining evidence omitted to bound request size]")
            break
        context_chunks.append(chunk)
        used += len(chunk)
    context = "\n".join(context_chunks) if context_chunks else "[no source evidence available]"
    user_parts = [
        "This repository has no supported test framework, so no tests were "
        "run. Review the evidence statically for likely defects:",
        context,
    ]
    if user_message:
        user_parts += [
            "The user reported the following problem; focus the review on it:",
            user_message[:MAX_STATIC_MESSAGE_CHARS],
        ]
    return [
        {
            "role": "system",
            "content": (
                "You are a careful code-review assistant. Identify the most "
                "likely defects in the evidence and propose the smallest "
                "source change that would address the top one. These are "
                "unverified hypotheses: no tests exist to confirm them. "
                "Respond with exactly one JSON object and no other text, "
                "with string keys 'diagnosis' and 'reason' plus an 'edits' "
                "array of 1 to "
                f"{MAX_PATCHSET_EDITS} objects with string keys 'file', "
                "'before', 'after'. 'file' must be a repository-relative "
                "path to one of the evidence files. 'before' must be the "
                "exact lines to replace. 'after' is the replacement. Never "
                "propose edits to test files, test directories, or "
                "configuration files. Do not rewrite unrelated code."
            ),
        },
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


def assess_from_context(
    *,
    profile_summary: str,
    structure: str,
    checks_text: str,
    excerpts: dict[str, str],
    user_message: str | None = None,
    api_key: str | None = None,
) -> str:
    """Make one live Meta API call assessing repository health; return prose.

    ``checks_text`` carries the deterministic check outcomes the prose must
    stay consistent with. Raises MetaConfigError, MetaRateLimitError,
    MetaAPIError, or MetaResponseError. Never returns a stub.
    """
    key = api_key or get_api_key()
    messages = build_health_messages(
        profile_summary=profile_summary,
        structure=structure,
        checks_text=checks_text,
        excerpts=excerpts,
        user_message=user_message,
    )
    payload = {
        "model": MODEL,
        "messages": messages,
        "max_completion_tokens": MAX_OUTPUT_TOKENS,
        "reasoning_effort": REASONING_EFFORT,
    }
    data = _post_chat_completion(key, payload)
    content, _ = _extract_content(data)
    return content.strip()


def build_health_messages(
    *,
    profile_summary: str,
    structure: str,
    checks_text: str,
    excerpts: dict[str, str],
    user_message: str | None = None,
) -> list[dict[str, str]]:
    """Build bounded chat messages for a static health assessment."""
    context_chunks: list[str] = []
    used = 0
    for path in sorted(excerpts)[:MAX_EVIDENCE_FILES]:
        chunk = f"--- {path} ---\n{excerpts[path]}\n"
        if used + len(chunk) > MAX_EVIDENCE_CHARS:
            context_chunks.append("[truncated: remaining excerpts omitted to bound request size]")
            break
        context_chunks.append(chunk)
        used += len(chunk)
    context = "\n".join(context_chunks) if context_chunks else "[no source excerpts available]"
    user_parts = [
        "Repository profile:",
        profile_summary,
        "Repository structure:",
        structure,
        "Deterministic health checks (observed facts; stay consistent with them):",
        checks_text,
        "Source excerpts (truncated to fit the request budget):",
        context,
    ]
    if user_message:
        user_parts += [
            "The user asked the following; focus the assessment on it:",
            user_message[:MAX_EXPLAIN_MESSAGE_CHARS],
        ]
    return [
        {
            "role": "system",
            "content": (
                "You are a careful repository-health assistant. Assess this "
                "repository's maintainability: dependency hygiene, test "
                "coverage, structure, and runtime verifiability. Base the "
                "assessment only on the profile, structure, checks, and "
                "excerpts provided; do not contradict the deterministic "
                "checks and do not invent files or behavior. Respond with "
                "plain Markdown prose and no JSON."
            ),
        },
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]


def _render_context(workspace: Path) -> str:
    """Render workspace source files, bounded by MAX_CONTEXT_CHARS."""
    chunks: list[str] = []
    used = 0
    files = sorted(
        path
        for path in workspace.rglob("*.py")
        if path.is_file() and "__pycache__" not in path.parts
    )
    for path in files:
        rel = str(path.relative_to(workspace)).replace("\\", "/")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            text = "[unreadable file]"
        chunk = f"--- {rel} ---\n{text}\n"
        if used + len(chunk) > MAX_CONTEXT_CHARS:
            chunks.append("[truncated: remaining source omitted to bound request size]")
            break
        chunks.append(chunk)
        used += len(chunk)
    return "\n".join(chunks) if chunks else "[no readable Python source found]"


def _post_chat_completion(api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        label = _http_label(exc)
        detail = _safe_error_detail(exc, api_key)
        if exc.code == 429:
            raise MetaRateLimitError(
                f"Meta API rate limited the request ({label}).{detail}"
            ) from exc
        raise MetaAPIError(
            f"Meta API request failed ({label}).{detail}",
            status_code=exc.code,
        ) from exc
    except urllib.error.URLError as exc:
        raise MetaAPIError(
            f"Meta API request failed: could not reach the API ({exc.reason})."
        ) from exc
    if not isinstance(data, dict):
        raise MetaResponseError("Meta API returned a malformed response envelope.")
    return data


def _http_label(exc: urllib.error.HTTPError) -> str:
    """Status label with the response content type when available."""
    label = f"HTTP {exc.code}"
    try:
        headers = getattr(exc, "hdrs", None) or getattr(exc, "headers", None)
        content_type = headers.get("Content-Type", "") if headers else ""
    except (AttributeError, ValueError):
        content_type = ""
    content_type = content_type.split(";")[0].strip()
    return f"{label}, {content_type}" if content_type else label


def _safe_error_detail(exc: urllib.error.HTTPError, api_key: str = "") -> str:
    """Extract a short, credential-free detail string from an HTTP error.

    Prefers the JSON error-envelope message; otherwise converts an HTML
    gateway page to plain text so the report shows the reason, not markup.
    """
    try:
        raw = exc.read().decode("utf-8", "replace")[:ERROR_BODY_CHARS]
    except (OSError, ValueError):
        return ""
    if api_key:
        raw = raw.replace(api_key, "[REDACTED]")
    try:
        parsed = json.loads(raw)
        message = parsed.get("error", {}).get("message", "")
        if not isinstance(parsed, dict):
            message = ""
    except (ValueError, AttributeError):
        message = ""
    detail = (message.strip() or _html_to_text(raw).strip())[:ERROR_DETAIL_CHARS]
    detail = detail.strip()
    return f" Details: {detail}" if detail else ""


def _html_to_text(raw: str) -> str:
    """Convert an HTML error page to readable plain text."""
    if "<" not in raw or ">" not in raw:
        return raw
    text = re.sub(
        r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.DOTALL | re.IGNORECASE
    )
    text = re.sub(r"<[^>]*>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text))


def _extract_content(data: dict[str, Any]) -> tuple[str, str | None]:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise MetaResponseError("Meta API response contained no choices.")
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message", {})
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise MetaResponseError("Meta API response contained no message content.")
    finish_reason = first.get("finish_reason")
    return content, finish_reason if isinstance(finish_reason, str) else None


def _parse_json_object(content: str, finish_reason: str | None) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:] if len(lines) > 1 else []
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = _extract_embedded_object(text)
        if parsed is None:
            hint = (
                " The response was truncated by the output-token limit."
                if finish_reason == "length"
                else ""
            )
            raise MetaResponseError(f"Meta API output was not valid JSON.{hint}")
    if not isinstance(parsed, dict):
        raise MetaResponseError("Meta API output was not a JSON object.")
    return parsed


def _extract_embedded_object(text: str) -> dict[str, Any] | None:
    """Return the JSON object embedded in prose, if there is exactly one."""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _to_diagnosis_and_proposal(payload: dict[str, Any]) -> tuple[str, PatchProposal]:
    required = ("diagnosis", "file", "before", "after", "reason")
    missing = [key for key in required if key not in payload]
    if missing:
        raise MetaResponseError(
            f"Meta API output is missing keys: {', '.join(sorted(missing))}."
        )
    values: dict[str, str] = {}
    for key in required:
        value = payload[key]
        if not isinstance(value, str):
            raise MetaResponseError(f"Meta API output key {key!r} must be a string.")
        values[key] = value
    if not values["diagnosis"].strip():
        raise MetaResponseError("Meta API output has an empty diagnosis.")
    if not values["file"].strip():
        raise MetaResponseError("Meta API output has an empty file path.")
    if not values["before"]:
        raise MetaResponseError("Meta API output has an empty 'before' block.")
    if not values["reason"].strip():
        raise MetaResponseError("Meta API output has an empty reason.")
    return values["diagnosis"], PatchProposal(
        file=values["file"].strip(),
        before=values["before"],
        after=values["after"],
        reason=values["reason"].strip(),
    )


def _to_diagnosis_and_patchset(payload: dict[str, Any]) -> tuple[str, PatchSet]:
    """Parse a real-repository repair response into (diagnosis, patch set).

    Accepts the multi-edit shape (``edits`` array) and the legacy
    single-edit shape (``file``/``before``/``after``).
    """
    for key in ("diagnosis", "reason"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise MetaResponseError(
                f"Meta API output has a missing or empty {key!r}."
            )
    raw_edits: list[Any]
    if "edits" in payload:
        raw_edits = payload["edits"]
        if not isinstance(raw_edits, list) or not raw_edits:
            raise MetaResponseError("Meta API output has an empty 'edits' array.")
    elif all(key in payload for key in ("file", "before", "after")):
        raw_edits = [
            {"file": payload["file"], "before": payload["before"], "after": payload["after"]}
        ]
    else:
        raise MetaResponseError(
            "Meta API output must contain an 'edits' array or file/before/after keys."
        )
    if len(raw_edits) > MAX_PATCHSET_EDITS:
        raise MetaResponseError(
            f"Meta API output has {len(raw_edits)} edits "
            f"(maximum {MAX_PATCHSET_EDITS})."
        )
    edits: list[FileEdit] = []
    for index, item in enumerate(raw_edits):
        if not isinstance(item, dict):
            raise MetaResponseError(f"Meta API edit {index} is not an object.")
        for key in ("file", "before", "after"):
            if not isinstance(item.get(key), str):
                raise MetaResponseError(
                    f"Meta API edit {index} key {key!r} must be a string."
                )
        file = item["file"].strip()
        if not file:
            raise MetaResponseError(f"Meta API edit {index} has an empty file path.")
        if not item["before"]:
            raise MetaResponseError(f"Meta API edit {index} has an empty 'before' block.")
        edits.append(FileEdit(file=file, before=item["before"], after=item["after"]))
    return payload["diagnosis"].strip(), PatchSet(
        edits=tuple(edits), reason=payload["reason"].strip()
    )
