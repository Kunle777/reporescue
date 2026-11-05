"""Supabase JWT verification with mocked signing keys (no live Auth)."""

import base64
import time
import uuid
from uuid import UUID

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app import auth
from app.auth import AuthError, AuthServiceError

ISSUER = "https://test.supabase.co/auth/v1"
AUDIENCE = "authenticated"
KID = "test-key-1"


def _b64u(number: int) -> str:
    return base64.urlsafe_b64encode(number.to_bytes(32, "big")).rstrip(b"=").decode("ascii")


@pytest.fixture()
def signing_key():
    private = ec.generate_private_key(ec.SECP256R1())
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    numbers = private.public_key().public_numbers()
    jwk = {
        "kty": "EC",
        "crv": "P-256",
        "x": _b64u(numbers.x),
        "y": _b64u(numbers.y),
        "kid": KID,
        "alg": "ES256",
        "use": "sig",
    }
    return private_pem, jwk


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "test-anon-key")
    monkeypatch.delenv("SUPABASE_JWT_AUDIENCE", raising=False)
    auth._reset_caches()
    yield
    auth._reset_caches()


def _install_jwks(monkeypatch, jwk, issuer=ISSUER):
    calls = {"n": 0}

    def fake_fetch(url):
        calls["n"] += 1
        if url.endswith("jwks.json"):
            return {"keys": [jwk]}
        return {"issuer": issuer}

    monkeypatch.setattr(auth, "_fetch_json", fake_fetch)
    return calls


def _mint(private_pem, *, kid=KID, drop=(), **overrides):
    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": str(uuid.uuid4()),
        "exp": now + 300,
        "iat": now,
        "email": "tester@example.com",
    }
    for key in drop:
        payload.pop(key, None)
    payload.update(overrides)
    return jwt.encode(payload, private_pem, algorithm="ES256", headers={"kid": kid})


def _credentials(token):
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_valid_token(monkeypatch, signing_key):
    private_pem, jwk = signing_key
    _install_jwks(monkeypatch, jwk)
    claims = auth.verify_access_token(_mint(private_pem))
    assert claims["aud"] == AUDIENCE
    assert claims["email"] == "tester@example.com"
    UUID(claims["sub"])


def test_valid_token_via_dependency(monkeypatch, signing_key):
    private_pem, jwk = signing_key
    _install_jwks(monkeypatch, jwk)
    user = auth.get_current_user(_credentials(_mint(private_pem)))
    assert isinstance(user.id, UUID)
    assert user.email == "tester@example.com"


def test_expired_token(monkeypatch, signing_key):
    private_pem, jwk = signing_key
    _install_jwks(monkeypatch, jwk)
    token = _mint(private_pem, exp=int(time.time()) - 100)
    with pytest.raises(AuthError, match="expired"):
        auth.verify_access_token(token)
    with pytest.raises(HTTPException) as exc_info:
        auth.get_current_user(_credentials(token))
    assert exc_info.value.status_code == 401


def test_invalid_signature(monkeypatch, signing_key):
    _, jwk = signing_key
    _install_jwks(monkeypatch, jwk)
    other = ec.generate_private_key(ec.SECP256R1())
    other_pem = other.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    with pytest.raises(AuthError):
        auth.verify_access_token(_mint(other_pem))
    with pytest.raises(HTTPException) as exc_info:
        auth.get_current_user(_credentials(_mint(other_pem)))
    assert exc_info.value.status_code == 401


def test_wrong_issuer(monkeypatch, signing_key):
    private_pem, jwk = signing_key
    _install_jwks(monkeypatch, jwk)
    with pytest.raises(AuthError):
        auth.verify_access_token(_mint(private_pem, iss="https://evil.example/auth/v1"))


def test_wrong_audience(monkeypatch, signing_key):
    private_pem, jwk = signing_key
    _install_jwks(monkeypatch, jwk)
    with pytest.raises(AuthError):
        auth.verify_access_token(_mint(private_pem, aud="someone-else"))


def test_missing_subject(monkeypatch, signing_key):
    private_pem, jwk = signing_key
    _install_jwks(monkeypatch, jwk)
    with pytest.raises(AuthError):
        auth.verify_access_token(_mint(private_pem, drop=("sub",)))


def test_non_uuid_subject_rejected(monkeypatch, signing_key):
    private_pem, jwk = signing_key
    _install_jwks(monkeypatch, jwk)
    with pytest.raises(AuthError, match="subject"):
        auth.verify_access_token(_mint(private_pem, sub="not-a-uuid"))


def test_malformed_token(monkeypatch, signing_key):
    _, jwk = signing_key
    _install_jwks(monkeypatch, jwk)
    with pytest.raises(AuthError, match="Malformed"):
        auth.verify_access_token("not-a-jwt")


def test_missing_key_id_rejected(monkeypatch, signing_key):
    private_pem, jwk = signing_key
    _install_jwks(monkeypatch, jwk)
    now = int(time.time())
    token = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "sub": str(uuid.uuid4()), "exp": now + 300},
        private_pem,
        algorithm="ES256",
    )
    with pytest.raises(AuthError, match="Malformed"):
        auth.verify_access_token(token)


def test_unexpected_algorithm_rejected(monkeypatch, signing_key):
    private_pem, jwk = signing_key
    forged_jwk = dict(jwk, alg="RS256")
    _install_jwks(monkeypatch, forged_jwk)
    with pytest.raises(AuthError, match="algorithm"):
        auth.verify_access_token(_mint(private_pem))


def test_missing_authorization_header():
    with pytest.raises(HTTPException) as exc_info:
        auth.get_current_user(None)
    assert exc_info.value.status_code == 401
    with pytest.raises(HTTPException) as exc_info:
        auth.get_current_user(_credentials(""))
    assert exc_info.value.status_code == 401


def test_unknown_key_id_refreshes_then_rejects(monkeypatch, signing_key):
    private_pem, jwk = signing_key
    calls = _install_jwks(monkeypatch, jwk)
    with pytest.raises(AuthError, match="signing key"):
        auth.verify_access_token(_mint(private_pem, kid="unknown-kid"))
    assert calls["n"] == 4  # initial JWKS+issuer fetch, then one refresh retry


def test_signing_keys_cached_across_verifications(monkeypatch, signing_key):
    private_pem, jwk = signing_key
    calls = _install_jwks(monkeypatch, jwk)
    auth.verify_access_token(_mint(private_pem))
    auth.verify_access_token(_mint(private_pem))
    assert calls["n"] == 2  # one JWKS fetch + one issuer fetch, then cached


def test_service_error_maps_to_503(monkeypatch, signing_key):
    private_pem, _ = signing_key

    def failing_fetch(url):
        raise AuthServiceError("down")

    monkeypatch.setattr(auth, "_fetch_json", failing_fetch)
    with pytest.raises(HTTPException) as exc_info:
        auth.get_current_user(_credentials(_mint(private_pem)))
    assert exc_info.value.status_code == 503


def test_missing_auth_config_maps_to_503(monkeypatch, signing_key):
    private_pem, _ = signing_key
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    with pytest.raises(HTTPException) as exc_info:
        auth.get_current_user(_credentials(_mint(private_pem)))
    assert exc_info.value.status_code == 503


def test_route_auth_wiring():
    from app import main as main_module
    from app.auth import get_current_user

    def uses_auth(route) -> bool:
        return any(
            dependency.call is get_current_user
            for dependency in route.dependant.dependencies
        )

    api_routes = [
        route
        for route in main_module.app.routes
        if getattr(route, "path", "").startswith("/api")
    ]
    assert api_routes, "expected registered /api routes"
    checked = 0
    for route in api_routes:
        key = (route.path, tuple(route.methods or ()))
        if route.path == "/api/health":
            assert not uses_auth(route), key
        else:
            assert uses_auth(route), key
            checked += 1
    # Guard against the test silently covering nothing if routes move.
    assert checked >= 15, checked


def test_health_endpoint_reports_liveness_without_demo_mode():
    from app.main import health

    assert health() == {"status": "ok", "service": "RepoRescue API"}
