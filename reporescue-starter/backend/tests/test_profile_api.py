"""Tests for the authenticated profile API."""

from __future__ import annotations

from conftest import ALICE, BOB
from app.auth import AuthenticatedUser


def _with_github(user, username, display_name="Octo Cat"):
    return AuthenticatedUser(
        id=user.id,
        email=user.email,
        claims={
            **user.claims,
            "user_metadata": {
                "user_name": username,
                "full_name": display_name,
                "avatar_url": "https://example.com/avatar.png",
            },
        },
    )


def test_get_provisions_profile(client):
    body = client.get("/api/profile").json()
    assert body["id"] == ALICE.id
    assert body["username"] is None
    assert body["github_username"] is None
    assert body["created_at"]


def test_update_username(client):
    body = client.patch("/api/profile", json={"username": "alice-dev"}).json()
    assert body["username"] == "alice-dev"
    assert client.get("/api/profile").json()["username"] == "alice-dev"


def test_update_username_rejects_bad_values(client):
    for username in ["", "   ", "bad name!", "x" * 40]:
        response = client.patch("/api/profile", json={"username": username})
        assert response.status_code == 422, username


def test_username_taken_409(client, current_user):
    current_user["user"] = BOB
    assert (
        client.patch("/api/profile", json={"username": "sam"}).status_code == 200
    )
    current_user["user"] = ALICE
    assert client.patch("/api/profile", json={"username": "sam"}).status_code == 409


def test_sync_github_without_identity_409(client):
    response = client.post("/api/profile/github", json={"confirm": False})
    assert response.status_code == 409


def test_sync_github_links_and_replaces(client, current_user):
    current_user["user"] = _with_github(ALICE, "octocat")
    body = client.post("/api/profile/github", json={"confirm": False}).json()
    assert body["github_username"] == "octocat"
    assert body["github_display_name"] == "Octo Cat"
    assert body["github_avatar_url"] == "https://example.com/avatar.png"
    assert body["github_connected_at"]
    same = client.post("/api/profile/github", json={"confirm": False}).json()
    assert same["github_username"] == "octocat"

    current_user["user"] = _with_github(ALICE, "defunkt")
    response = client.post("/api/profile/github", json={"confirm": False})
    assert response.status_code == 409
    replaced = client.post("/api/profile/github", json={"confirm": True}).json()
    assert replaced["github_username"] == "defunkt"


def test_disconnect_github_keeps_account(client, current_user):
    current_user["user"] = _with_github(ALICE, "octocat")
    client.patch("/api/profile", json={"username": "alice-dev"})
    client.post("/api/profile/github", json={"confirm": False})
    body = client.delete("/api/profile/github").json()
    assert body["github_username"] is None
    assert body["github_connected_at"] is None
    assert body["username"] == "alice-dev"


def test_profile_requires_auth(anon_client):
    assert anon_client.get("/api/profile").status_code == 401
