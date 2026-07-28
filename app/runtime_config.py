"""Runtime environment and fail-closed auth configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum


class AppEnv(StrEnum):
    TEST = "test"
    DEVELOPMENT = "development"
    PRODUCTION = "production"


VALID_ENVS = {e.value for e in AppEnv}


@dataclass(frozen=True)
class RuntimeSettings:
    app_env: AppEnv
    auth_disabled: bool
    auth_enabled: bool
    cookie_secure: bool

    def public_log_dict(self) -> dict[str, str | bool]:
        """Safe for startup logs — no secrets."""
        return {
            "app_env": self.app_env.value,
            "auth_enabled": self.auth_enabled,
            "cookie_secure": self.cookie_secure,
        }


class ConfigurationError(RuntimeError):
    """Invalid runtime configuration; process must not serve traffic."""


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def resolve_app_env(raw: str | None = None, *, default_from_os: bool = True) -> AppEnv:
    """Unset APP_ENV defaults to production for fail-closed behavior."""
    if raw is None and default_from_os:
        raw = os.environ.get("APP_ENV", "")
    value = (raw or "").strip().lower()
    if not value:
        return AppEnv.PRODUCTION
    if value not in VALID_ENVS:
        raise ConfigurationError(
            f"Invalid APP_ENV={value!r}; expected one of: {', '.join(sorted(VALID_ENVS))}"
        )
    return AppEnv(value)


def load_runtime_settings(
    *,
    environ: dict[str, str] | None = None,
    validate: bool = True,
) -> RuntimeSettings:
    # When environ is provided explicitly, do not fall back to process OS env for APP_ENV
    # (tests pass isolated maps). Missing APP_ENV key => production fail-closed.
    if environ is not None:
        app_env = resolve_app_env(environ.get("APP_ENV", ""), default_from_os=False)
        disabled_requested = _truthy(environ.get("POLICY_AUTH_DISABLED"))
        cookie_secure = _truthy(environ.get("POLICY_COOKIE_SECURE"))
    else:
        app_env = resolve_app_env()
        disabled_requested = _truthy(os.environ.get("POLICY_AUTH_DISABLED"))
        cookie_secure = _truthy(os.environ.get("POLICY_COOKIE_SECURE"))

    if validate:
        if disabled_requested and app_env != AppEnv.TEST:
            raise ConfigurationError(
                f"POLICY_AUTH_DISABLED is only allowed when APP_ENV=test "
                f"(current APP_ENV={app_env.value}). Refusing to start."
            )
        if app_env == AppEnv.PRODUCTION and disabled_requested:
            raise ConfigurationError(
                "APP_ENV=production forbids POLICY_AUTH_DISABLED=1. Refusing to start."
            )

    auth_disabled = bool(disabled_requested and app_env == AppEnv.TEST)
    return RuntimeSettings(
        app_env=app_env,
        auth_disabled=auth_disabled,
        auth_enabled=not auth_disabled,
        cookie_secure=cookie_secure,
    )


_CACHED: RuntimeSettings | None = None


def get_runtime_settings(*, refresh: bool = False) -> RuntimeSettings:
    global _CACHED
    if _CACHED is None or refresh:
        _CACHED = load_runtime_settings(validate=True)
    return _CACHED


def reset_runtime_settings_cache() -> None:
    global _CACHED
    _CACHED = None


def is_auth_disabled() -> bool:
    return get_runtime_settings().auth_disabled
