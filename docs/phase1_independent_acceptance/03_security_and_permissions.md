# 03 Security and Permissions

## Route inventory (from `app/main.py`)

| Method | Path | Auth | CSRF | Write | In matrix |
|---|---|---|---|---|---|
| GET | `/` | public | — | N | Y |
| GET | `/health` | public | — | N | Y |
| GET | `/api/auth/me` | optional | — | N | Y |
| POST | `/api/auth/login` | public | — | Y (session) | Y |
| POST | `/api/auth/logout` | `require_write("read")` | Y | Y | Y |
| POST | `/api/auth/logout-simple` | `require_login` | **N** | Y (session revoke) | Y |
| GET | `/api/users` | admin role | — | N | Y |
| POST | `/api/users` | `users:admin` write | Y | Y | Y |
| GET | `/api/audits` | admin, auditor | — | N | Y |
| GET | `/api/continuous-schedules` | login | — | N | Y |
| GET | `/api/jobs/{id}/generation` | login | — | N | Y |
| GET | `/api/jobs` | login | — | N | Y |
| GET | `/api/baselines` | login | — | N | Y |
| GET | `/api/scan-sites` | login | — | N | Y |
| POST | `/api/site-jobs` | jobs:write | Y | Y | Y |
| POST | `/api/site-jobs/full-rebuild` | jobs:write | Y | Y | Y |
| GET | `/api/jobs/{id}` | login | — | N | Y |
| GET | `/api/jobs/{id}/counts` | login | — | N | Y |
| POST | `/api/jobs` | jobs:write | Y | Y | Y |
| GET | `/api/jobs/{id}/item-stats` | login | — | N | Y |
| POST | `/api/jobs/{id}/pause|resume|stop` | jobs:write | Y | Y | Y |
| GET | `/api/jobs/{id}/export` | login + `export:read` | — | N | Y |
| GET | `/api/jobs/{id}/findings` | login | — | N | Y |
| GET | `/api/jobs/{id}/finding-summary` | login | — | N | Y |
| GET | `/api/jobs/{id}/scan-exceptions` | login | — | N | Y |
| GET | `/api/jobs/{id}/review-queue` | login | — | N | Y |
| GET | `/api/findings/{id}/evidence` | login | — | N | Y |
| POST | `/api/findings/{id}/review` | review:write | Y | Y | Y |

FastAPI default `/docs` `/redoc` `/openapi.json`: public (matrix acknowledged).

## Role capabilities (`app/auth/models.py`)

| Role | Capabilities |
|---|---|
| admin | all |
| operator | jobs:write, export:read, read |
| reviewer | review:write, export:read, read |
| viewer | read, export:read |
| auditor | read, audit:read, export:read |

Matches expected separation: operator cannot review/users; reviewer cannot jobs/users; viewer read(+export); auditor audits read-only.

## Auth environment matrix (code + probe)

| APP_ENV | POLICY_AUTH_DISABLED | Result |
|---|---|---|
| test | 1 | Allowed; auth off |
| development | 1 | **ConfigurationError** |
| production | 1 | **ConfigurationError** |
| production | 0 | Auth on |
| development | 0 | Auth on |
| unset | — | Treated as **production**, auth on |
| bogus | — | ConfigurationError |

Probe: `tmp/accept_probe_auth_env.py` output captured in acceptance run.

## Other controls

| Check | Result |
|---|---|
| Health exposes DB absolute path? | **No** — `database: "configured"`, app_env, auth_enabled |
| Password hash | PBKDF2-SHA256, 390k iterations, salt, `hmac.compare_digest` |
| Bootstrap default password | **None** — env or interactive |
| Audit fields | actor, action, resource, before/after/detail, ip, ua, time |
| Unauthenticated write business APIs | **Not found** on enumerated routes |

## Residual

- Live multi-role HTTP exercise against policy server: **未确认** (no policy process bound in this workspace; pytest covers many auth cases under test env).
- `logout-simple` CSRF exception: documented P2.
