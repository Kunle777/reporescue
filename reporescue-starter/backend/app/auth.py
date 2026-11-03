"""Supabase JWT verification for authenticated API routes.

Access tokens are verified against the project's live JWKS document
(signatures, issuer, audience, and expiry). Accepted algorithms come from the
project's own JWKS entries; the bundled project advertises ES256. ``None``
(anonymous) is never a valid identity: routes that need a user depend on
:func:`get_current_user`, which answers 401 for caller credential problems
and 503 when verification itself cannot run.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWK

from . import config

bearer_scheme = HTTPBearer(auto_error=False)

JWKS_CACHE_SECONDS = 600
CLOCK_SKEW_SECONDS = 30
FETCH_TIMEOUT_SECONDS = 10

_cache_lock = threading.Lock()
_jwks_keys: list[dict[str, Any]] | None = None
_jwks_fetched_at = 0.0
_cached_issuer: str | None = None


class AuthError(Exception):
    """Caller credential problem (maps to HTTP 401). Never carries secrets."""


class AuthServiceError(Exception):
    """Verification cannot run (maps to HTTP 503). Never carries secrets."""


@dataclass(frozen=True)
class AuthenticatedUser:
    """The user identified by a verified Bearer [REDACTED]"""

    id: str
    email: str | None
    claims: dict[str, Any]


def _fetch_json(url: str) -> dict[str, Any]:
    try:
        api_key = config.get_supabase_anon_key()
    except config.ConfigError as exc:
        raise AuthServiceError(str(exc)) from exc
    request = urllib.request.Request(url, headers={"apikey": api_key})
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise AuthServiceError("Supabase Auth is unreachable.") from exc
    if not isinstance(data, dict):
        raise AuthServiceError("Supabase Auth returned a malformed document.")
    return data


def _refresh_jwks() -> None:
    """Reload signing keys and the expected issuer from Supabase Auth."""
    global _jwks_keys, _jwks_fetched_at, _cached_issuer
    try:
        base = config.get_supabase_url()
    except config.ConfigError as exc:
        raise AuthServiceError(str(exc)) from exc
    document = _fetch_json(f"{base}/auth/v1/.well-known/jwks.json")
    keys = document.get("keys")
    if not isinstance(keys, list) or not keys:
        raise AuthServiceError("Supabase JWKS document contains no keys.")
    issuer: str | None = None
    try:
        discovery = _fetch_json(f"{base}/auth/v1/.well-known/openid-configuration")
        raw_issuer = discovery.get("issuer")
        if isinstance(raw_issuer, str) and raw_issuer.strip():
            issuer = raw_issuer.strip()
    except AuthServiceError:
        issuer = None
    if issuer is None:
        issuer = f"{base}/auth/v1"
    with _cache_lock:
        _jwks_keys = [key for key in keys if isinstance(key, dict)]
        _jwks_fetched_at = time.monotonic()
        _cached_issuer = issuer


def _signing_key(kid: str) -> dict[str, Any]:
    """Return the JWK for ``kid``, refreshing once on a cache miss."""

    def find() -> dict[str, Any] | None:
        for key in _jwks_keys or []:
            if key.get("kid") == kid:
                return key
        return None

    with _cache_lock:
        fresh = _jwks_keys is not None and (
            time.monotonic() - _jwks_fetched_at
        ) < JWKS_CACHE_SECONDS
    if not fresh:
        _refresh_jwks()
    key = find()
    if key is None:
        _refresh_jwks()
        key = find()
    if key is None:
        raise AuthError("Token signing key is not recognized.")
    return key


def _expected_issuer() -> str:
    """Return the issuer to require, from discovery with a derived fallback."""
    with _cache_lock:
        fresh = _cached_issuer is not None and (
            time.monotonic() - _jwks_fetched_at
        ) < JWKS_CACHE_SECONDS
        issuer = _cached_issuer
    if not fresh or issuer is None:
        _refresh_jwks()
        with _cache_lock:
            issuer = _cached_issuer
    assert issuer is not None
    return issuer


def verify_access_token(token: str) -> dict[str, Any]:
    """Verify a Supabase access token and return its claims.

    Raises :class:`AuthError` for caller credential problems and
    :class:`AuthServiceError` when verification itself cannot run.
    """
    if not isinstance(token, str) or not token.strip():
        raise AuthError("Missing credentials.")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise AuthError("Malformed credentials.") from exc
    kid = header.get("kid")
    alg = header.get("alg")
    if not kid or not alg:
        raise AuthError("Malformed credentials.")
    jwk = _signing_key(kid)
    expected_alg = jwk.get("alg")
    if not expected_alg or alg != expected_alg:
        raise AuthError("Unexpected token algorithm.")
    try:
        public_key = PyJWK(jwk).key
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        raise AuthServiceError("Unusable signing key.") from exc
    try:
        claims = jwt.decode(
            token,
            key=public_key,
            algorithms=[expected_alg],
            issuer=_expected_issuer(),
            audience=config.get_jwt_audience(),
            leeway=CLOCK_SKEW_SECONDS,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Credentials have expired.") from exc
    except jwt.PyJWTError as exc:
        raise AuthError("Invalid credentials.") from exc
    try:
        UUID(str(claims["sub"]))
    except (ValueError, AttributeError, TypeError) as exc:
        raise AuthError("Invalid subject.") from exc
    return claims


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> AuthenticatedUser:
    """FastAPI dependency: the user identified by a verified Bearer [REDACTED]"""
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing credentials.",
        )
    try:
        claims = verify_access_token(credentials.credentials)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc
    except AuthServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    email = claims.get("email")
    return AuthenticatedUser(
        id=str(claims["sub"]),
        email=str(email) if email is not None else None,
        claims=claims,
    )
