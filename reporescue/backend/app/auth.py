from __future__ import annotations

"""Supabase access-token (JWT) verification (Phase 4).

Verifies signatures against the Supabase project's live JWKS document and
validates issuer, audience, and expiration. Accepted algorithms come from
the project's own JWKS entries (this project advertises ES256); no signing
algorithm is assumed. An unknown key id triggers one JWKS refresh before
rejection, so key rotation does not lock users out. Signing keys are cached
in memory with a TTL; only key metadata is ever held, never tokens.
"""

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWK

from . import config

bearer_scheme = HTTPBearer(auto_error=False)

JWKS_CACHE_SECONDS = 600
REQUEST_TIMEOUT_SECONDS = 15
CLOCK_SKEW_SECONDS = 30


class AuthError(Exception):
    """The caller's credentials are missing, expired, or invalid (HTTP 401)."""


class AuthServiceError(Exception):
    """Verification itself failed: bad server config or unreachable Auth (HTTP 503)."""


@dataclass(frozen=True)
class AuthenticatedUser:
    """Identity derived exclusively from a verified JWT's ``sub`` claim."""

    id: UUID
    email: str | None
    claims: dict[str, Any]


_cache_lock = threading.Lock()
_jwks_keys: list[dict[str, Any]] | None = None
_jwks_fetched_at: float = 0.0
_cached_issuer: str | None = None


def _reset_caches() -> None:
    """Clear cached JWKS/issuer state. Used by tests to isolate cases."""
    global _jwks_keys, _jwks_fetched_at, _cached_issuer
    with _cache_lock:
        _jwks_keys = None
        _jwks_fetched_at = 0.0
        _cached_issuer = None


def _fetch_json(url: str) -> dict[str, Any]:
    """GET a Supabase Auth metadata document. The URL holds no secrets."""
    try:
        anon_key = config.get_supabase_anon_key()
    except config.ConfigError as exc:
        raise AuthServiceError(str(exc)) from exc
    request = urllib.request.Request(url, headers={"apikey": anon_key})
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError, OSError) as exc:
        raise AuthServiceError(f"Could not reach Supabase Auth ({exc}).") from exc
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
    """FastAPI dependency: the user identified by a verified Bearer token."""
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=401,
            detail={"code": "NOT_AUTHENTICATED", "message": "Not authenticated."},
        )
    try:
        claims = verify_access_token(credentials.credentials)
    except AuthError as exc:
        raise HTTPException(
            status_code=401,
            detail={"code": "AUTH_FAILED", "message": str(exc)},
        )
    except AuthServiceError:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "AUTH_SERVICE_UNAVAILABLE",
                "message": "Authentication service unavailable.",
            },
        )
    email = claims.get("email")
    return AuthenticatedUser(
        id=UUID(str(claims["sub"])),
        email=email if isinstance(email, str) else None,
        claims=claims,
    )
