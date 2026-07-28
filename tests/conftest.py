"""Auto-configure APP_ENV/auth for tests. Fail-closed rules still enforced in unit tests."""

from __future__ import annotations

import pytest

from app.runtime_config import reset_runtime_settings_cache


@pytest.fixture(autouse=True)
def _default_auth_bypass(monkeypatch, request):
    reset_runtime_settings_cache()
    if request.node.get_closest_marker("real_auth"):
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("POLICY_AUTH_DISABLED", "0")
    else:
        # Legacy functional tests may bypass auth only in APP_ENV=test.
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("POLICY_AUTH_DISABLED", "1")
    reset_runtime_settings_cache()
    yield
    reset_runtime_settings_cache()
