"""Test helpers: bootstrap users and auth cookies for API tests."""

from __future__ import annotations

import os

from fastapi.testclient import TestClient

from app.auth.service import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE, AuthService
from app.db import Database


def enable_test_auth(monkeypatch) -> None:
    """Force real auth (not disabled) for security tests."""
    monkeypatch.delenv("POLICY_AUTH_DISABLED", raising=False)
    monkeypatch.setenv("POLICY_AUTH_DISABLED", "0")


def disable_auth_for_legacy_tests(monkeypatch) -> None:
    """Legacy functional tests run with auth bypass."""
    monkeypatch.setenv("POLICY_AUTH_DISABLED", "1")


def bootstrap_users(db: Database) -> dict[str, dict]:
    auth = AuthService(db)
    auth.ensure_role_rows()
    specs = {
        "admin": (["admin"], "AdminPass123!"),
        "operator": (["operator"], "OperPass123!"),
        "reviewer": (["reviewer"], "ReviewPass123!"),
        "viewer": (["viewer"], "ViewerPass123!"),
        "auditor": (["auditor"], "AuditPass123!"),
    }
    created = {}
    for username, (roles, password) in specs.items():
        try:
            auth.create_user(username, password, roles, display_name=username)
        except ValueError:
            pass
        created[username] = {"username": username, "password": password, "roles": roles}
    return created


def login(client: TestClient, username: str, password: str) -> dict:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    data = response.json()
    csrf = data["csrf_token"]
    return {"csrf_token": csrf, "headers": {CSRF_HEADER: csrf}}


def authed_client_headers(client: TestClient, username: str, password: str) -> dict:
    return login(client, username, password)["headers"]
