# Route Permission Matrix

Auth model: cookie session + CSRF on writes.  
`POLICY_AUTH_DISABLED` only effective when `APP_ENV=test`.

Legend: A=admin O=operator R=reviewer V=viewer U=auditor | Y=allowed | N=denied | pub=public

| Method | Path | Auth | Roles/Capability | CSRF | Notes |
|---|---|---|---|---|---|
| GET | `/` | pub | — | — | UI shell |
| GET | `/health` | pub | — | — | no DB path; env+auth_enabled only |
| GET | `/docs` `/redoc` `/openapi.json` | pub | — | — | consider locking in prod later |
| POST | `/api/auth/login` | pub | — | — | issues session+csrf |
| POST | `/api/auth/logout` | login+write(read) | any logged-in | Y | |
| POST | `/api/auth/logout-simple` | login | any | — | still requires session |
| GET | `/api/auth/me` | optional | — | — | |
| GET | `/api/users` | roles | admin | — | |
| POST | `/api/users` | write | users:admin | Y | audited |
| GET | `/api/audits` | roles | admin,auditor | — | |
| GET | `/api/jobs` | login | read | — | |
| POST | `/api/jobs` | write | jobs:write | Y | O/A |
| GET | `/api/jobs/{id}` | login | read | — | includes count semantics |
| GET | `/api/jobs/{id}/counts` | login | read | — | |
| POST | `/api/jobs/{id}/pause` | write | jobs:write | Y | |
| POST | `/api/jobs/{id}/resume` | write | jobs:write | Y | |
| POST | `/api/jobs/{id}/stop` | write | jobs:write | Y | |
| GET | `/api/jobs/{id}/export` | login | export:read | — | audited |
| GET | `/api/jobs/{id}/findings` | login | read | — | current only |
| GET | `/api/jobs/{id}/finding-summary` | login | read | — | |
| GET | `/api/jobs/{id}/review-queue` | login | read | — | |
| GET | `/api/jobs/{id}/scan-exceptions` | login | read | — | |
| GET | `/api/jobs/{id}/item-stats` | login | read | — | |
| GET | `/api/jobs/{id}/generation` | login | read | — | |
| GET | `/api/findings/{id}/evidence` | login | read | — | + review_history |
| POST | `/api/findings/{id}/review` | write | review:write | Y | R/A; append reviews |
| GET | `/api/scan-sites` | login | read | — | |
| POST | `/api/site-jobs` | write | jobs:write | Y | |
| POST | `/api/site-jobs/full-rebuild` | write | jobs:write | Y | |
| GET | `/api/baselines` | login | read | — | |
| GET | `/api/continuous-schedules` | login | read | — | |

## Role summary

| Role | Can |
|---|---|
| admin | all |
| operator | jobs write, export, read |
| reviewer | review write, export, read |
| viewer | read + export only |
| auditor | read + audits + export |

## CSRF

All cookie-session **POST** (except public login) go through `require_write` or explicit login session checks.  
`logout-simple` requires login; prefer `/api/auth/logout` with CSRF in browsers.
