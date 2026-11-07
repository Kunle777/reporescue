"""Central secret redaction for everything that reaches the AI provider.

All outbound prompts, context blobs, and persisted model outputs pass
through here. Patterns use single-backslash regex escapes (``\\b`` is a
word boundary inside a raw string; ``\\\\b`` would match a literal
backslash and silently disable redaction).
"""
from __future__ import annotations

import re
from typing import Any

# PEM private-key blocks (RSA, EC, OpenSSH, PKCS8, ...). DOTALL so the
# base64 body matches across newlines; non-greedy so adjacent blocks
# redact independently.
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)

# Provider-issued tokens with recognizable prefixes.
_ISSUED_TOKEN_RE = re.compile(
    r"\b(ghp_[A-Za-z0-9]{8,}|gho_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,})\b"
)

# Authorization header values. Bearer runs before the generic
# key=value matcher so the token itself (not just the scheme word)
# is removed.
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_BASIC_RE = re.compile(r"(?i)\bBasic\s+[A-Za-z0-9+/=]+")

# Credentials embedded in URLs: postgres://user:pass@host/db and
# https://bot:token@github.com/owner/repo. Only the password segment
# is redacted; hosts and paths stay readable.
_URL_PASSWORD_RE = re.compile(
    r"(?i)\b((?:https?|postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?"
    r"|redis(?:s)?|amqp(?:s)?)://[^/\s:@]+:)([^@\s/]+)(@)"
)

# Named secrets: api_key=..., password: ..., db_password = ... The
# optional [\w.-]* prefix covers db_password-style names. A separator
# plus a value is required, so prose ("password policies") and
# identifiers (password_policy) never match.
_NAMED_SECRET_RE = re.compile(
    r"(?i)\b([\w.-]*(?:api[_-]?key|access[_-]?token|refresh[_-]?token"
    r"|client[_-]?secret|auth[_-]?token|session[_-]?token"
    r"|password|passwd|pwd|secret|authorization|private[_-]?key))"
    r"(\s*[:=]\s*)([^\s,;\"']+)"
)

# UPPER_SNAKE env assignments whose name signals a credential, e.g.
# STRIPE_SECRET_KEY=... (the generic matcher cannot see SECRET_KEY
# because of the trailing _KEY word characters).
_ENV_SECRET_RE = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:API[_-]?KEY|SECRET|TOKEN|PASSWORD)[A-Z0-9_]*)\s*=\s*([^\s\"']+)"
)

REDACTED = "[REDACTED]"


def redact_text(value: object, limit: int | None = None) -> str:
    """Replace credential-like values in ``value`` with ``[REDACTED]``."""
    text = str(value or "")
    text = _PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", text)
    text = _ISSUED_TOKEN_RE.sub(REDACTED, text)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _BASIC_RE.sub("Basic [REDACTED]", text)
    text = _URL_PASSWORD_RE.sub(r"\1[REDACTED]\3", text)
    text = _NAMED_SECRET_RE.sub(r"\1\2[REDACTED]", text)
    text = _ENV_SECRET_RE.sub(r"\1=[REDACTED]", text)
    if limit is not None:
        text = text[:limit]
    return text


def redact_data(data: Any) -> Any:
    """Deep-copy ``data`` with every string redacted (no truncation)."""
    if isinstance(data, str):
        return redact_text(data)
    if isinstance(data, dict):
        return {key: redact_data(val) for key, val in data.items()}
    if isinstance(data, list):
        return [redact_data(item) for item in data]
    if isinstance(data, tuple):
        return tuple(redact_data(item) for item in data)
    return data


def sanitize_model_text(text: str) -> str:
    """Redact credentials from model-generated text before persistence."""
    return redact_text(text)
