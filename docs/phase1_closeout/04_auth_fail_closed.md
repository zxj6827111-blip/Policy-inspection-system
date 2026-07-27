# Auth Fail-Closed

## APP_ENV

| Value | Auth disable allowed? |
|---|---|
| `test` | Yes, only here may `POLICY_AUTH_DISABLED=1` |
| `development` | **No** |
| `production` | **No** |
| unset | Treated as **production** (fail closed) |

## Startup

- `get_runtime_settings(validate=True)` runs in FastAPI lifespan.
- Misconfiguration raises `ConfigurationError` → process refuses to serve.
- Startup log only: `app_env=... auth_enabled=...` (no secrets).

## Health

```json
{"status":"ok","database":"configured","app_env":"production","auth_enabled":true}
```

No DB absolute path.

## Admin bootstrap

`scripts/bootstrap_admin.py` never prints the password; only username/id/db path.

## Tests

- production + disabled → ConfigurationError
- development + disabled → ConfigurationError
- test + disabled → allowed
- unset APP_ENV → production + auth on
- TestClient lifespan refuses production misconfig
