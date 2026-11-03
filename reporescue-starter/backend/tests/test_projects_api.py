"""Tests for the authenticated projects API."""

from __future__ import annotations

from conftest import BOB


def test_create_project_defaults_display_name(client):
    response = client.post(
        "/api/projects", json={"github_url": "https://github.com/acme/calc"}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["display_name"] == "acme/calc"
    assert body["github_owner"] == "acme"
    assert body["repository_name"] == "calc"
    assert body["github_url"] == "https://github.com/acme/calc"
    assert body["default_branch"] is None
    assert body["visibility"] == "public"
    assert body["created_at"]


def test_create_project_rejects_bad_urls(client):
    for github_url in ["not-a-url", "https://example.com/acme/calc", "   "]:
        response = client.post("/api/projects", json={"github_url": github_url})
        assert response.status_code == 422, github_url


def test_create_duplicate_409(client):
    payload = {"github_url": "https://github.com/acme/calc"}
    assert client.post("/api/projects", json=payload).status_code == 201
    assert client.post("/api/projects", json=payload).status_code == 409


def test_list_newest_first_and_owned(client, current_user):
    first = client.post(
        "/api/projects", json={"github_url": "https://github.com/acme/one"}
    ).json()
    second = client.post(
        "/api/projects", json={"github_url": "https://github.com/acme/two"}
    ).json()
    body = client.get("/api/projects").json()
    assert [project["id"] for project in body] == [second["id"], first["id"]]
    current_user["user"] = BOB
    assert client.get("/api/projects").json() == []


def test_rename_project(client):
    created = client.post(
        "/api/projects", json={"github_url": "https://github.com/acme/calc"}
    ).json()
    body = client.patch(
        f"/api/projects/{created['id']}", json={"display_name": "Calculator"}
    ).json()
    assert body["display_name"] == "Calculator"
    assert body["github_owner"] == "acme"


def test_rename_rejects_empty_and_missing(client, current_user):
    created = client.post(
        "/api/projects", json={"github_url": "https://github.com/acme/calc"}
    ).json()
    assert (
        client.patch(
            f"/api/projects/{created['id']}", json={"display_name": "  "}
        ).status_code
        == 422
    )
    assert (
        client.patch("/api/projects/nope", json={"display_name": "x"}).status_code
        == 404
    )
    current_user["user"] = BOB
    assert (
        client.patch(
            f"/api/projects/{created['id']}", json={"display_name": "x"}
        ).status_code
        == 404
    )


def test_delete_project(client, current_user):
    created = client.post(
        "/api/projects", json={"github_url": "https://github.com/acme/calc"}
    ).json()
    assert client.delete(f"/api/projects/{created['id']}").status_code == 204
    assert client.delete(f"/api/projects/{created['id']}").status_code == 404
    other = client.post(
        "/api/projects", json={"github_url": "https://github.com/acme/kept"}
    ).json()
    current_user["user"] = BOB
    assert client.delete(f"/api/projects/{other['id']}").status_code == 404


def test_projects_require_auth(anon_client):
    assert anon_client.get("/api/projects").status_code == 401
