"""M1: API documentation protection tests.

Verifies /docs, /redoc, /openapi.json are fail-closed in production
and accessible in non-production environments.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.db import Database
from app.jobs import JobManager
from app.runtime_config import AppEnv, get_runtime_settings, reset_runtime_settings_cache
from tests.auth_helpers import bootstrap_users

pytestmark = pytest.mark.real_auth


def _client(tmp_path, monkeypatch):
    db = Database(tmp_path / "m1_docs.db")
    db.initialize()
    manager = JobManager(db)
    monkeypatch.setattr(main_module, "db", db)
    monkeypatch.setattr(main_module, "manager", manager)
    monkeypatch.setenv("POLICY_AUTH_DISABLED", "0")
    bootstrap_users(db)
    return TestClient(main_module.app)


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_accessible_in_test_env(tmp_path, monkeypatch, path):
    """Non-production environments keep docs reachable."""
    client = _client(tmp_path, monkeypatch)
    with client:
        reset_runtime_settings_cache()
        settings = get_runtime_settings()
        assert settings.app_env == AppEnv.TEST
        response = client.get(path)
        assert response.status_code == 200, response.text


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_blocked_in_production_env(tmp_path, monkeypatch, path):
    """Production environment returns 404 for API documentation paths."""
    client = _client(tmp_path, monkeypatch)
    with client:
        # Force production fail-closed: APP_ENV=production, no auth bypass.
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("POLICY_AUTH_DISABLED", "0")
        reset_runtime_settings_cache()
        settings = get_runtime_settings()
        assert settings.app_env == AppEnv.PRODUCTION
        response = client.get(path)
        assert response.status_code == 404, response.text
        assert "已禁用" in response.json()["detail"]


def test_health_and_login_still_work_in_production(tmp_path, monkeypatch):
    """Production fail-closed does not block public health/login paths."""
    client = _client(tmp_path, monkeypatch)
    with client:
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("POLICY_AUTH_DISABLED", "0")
        reset_runtime_settings_cache()
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["app_env"] == "production"
        assert health.json()["auth_enabled"] is True
