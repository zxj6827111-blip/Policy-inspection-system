# 02 Code Review — Uncommitted Phase1 + Closeout

## Files reviewed (primary)

Modified: `app/main.py`, `db.py`, `domain.py`, `exporter.py`, `jobs.py`, `repository.py`, `rules.py`, `static/app.js`, `templates/index.html`

New: `app/auth/**`, `findings_model.py`, `finding_counts.py`, `runtime_config.py`, `migrations/phase1.py`, `migrations/closeout.py`, `knowledge/organizations.py`, `extractors/document_roles.py`, `rules_legacy_agency.py`, scripts, phase1 tests, docs.

## Consistency with design

| Area | Assessment |
|---|---|
| Auth fail-closed (`runtime_config`) | Aligns with closeout `04_auth_fail_closed.md` |
| CSRF on `require_write` | Present for cookie session mutations |
| Lineage in `Repository.save_record` | Supersede + inherit human; **no** delete of `finding_reviews` |
| Counts module | Shared by API/job recompute; exporter uses `is_customer_formal` |
| ORG rules + knowledge seed | Knowledge/symbols/aliases; not URL whitelist |
| Legacy AGENCY | `allow_customer_output=False`, `machine_status=suspected` |
| Bootstrap admin | Env/prompt password; no hardcoded default secret |

## Issues found in code review

### P1 — Formal gate vs written design formula

Acceptance gate text and `03_count_semantics.md` state formal as:

1. `human_confirmed`, **or**
2. `allow_customer_output=1` **and** `machine_status=confirmed_by_rule`

Implementation in `is_customer_formal_problem` **additionally**:

- Hard-blocks `NON_CUSTOMER_RULES = {AGENCY-001..003, ORG-002}` even if `human_confirmed`
- Auto path only if `rule_code in STRICT_AUTO_CONFIRM_RULES` (META/DOC/DATE set), **not** any `confirmed_by_rule`
- Requires `allow_customer_output` **even for** `human_confirmed` (allow=0 → not formal)

Also: design “Exclusions” say **All ORG-***, but code only lists **ORG-002** in `NON_CUSTOMER_RULES`. **ORG-001 human_confirmed + allow=1 → formal=True** in code.

**Impact:** Documented formula ≠ code; ORG exclusion incomplete relative to design table. Runtime behavior is stricter on auto path and on allow flag; looser on ORG-001 human path vs “All ORG-*”.

### P1 — Job list UI column “问题” binds `finding_count` (total), not formal

`index.html` table header **问题** + `app.js` `job.finding_count`. Detail tile correctly uses **客户正式问题** + `formal_finding_count`. Risk of operators reading list column as formal count (closeout forbids presenting total as formal). Partially mitigated by detail tile labels and API `finding_count_meaning`.

### P2 — `POST /api/auth/logout-simple` without CSRF

Requires login but **not** `require_write`. Documented in matrix; residual session CSRF surface for logout only (not data mutation of findings).

### P2 — Public `/`, `/docs`, `/redoc`, `/openapi.json`

UI shell and OpenAPI public. Matrix notes “consider locking in prod later”. Not a bare unauthenticated **write** path.

### P2 — GET `/` serves full SPA without login

Business **APIs** require login; static shell is public. Acceptable if APIs enforced (they are for listed routes).

### P0 search results

- No hardcoded admin password in bootstrap
- No alternate unauthenticated job/review write routes found in `main.py`
- No `DELETE FROM finding_reviews` / `review_decisions` in app tree (lineage path comments + append-only reviews)
- `review_decisions` uses UPSERT (current decision only); history intended in `finding_reviews` — by design, not silent history wipe of append table

## Test-only vs production

`POLICY_AUTH_DISABLED` effective **only** when `APP_ENV=test` after validation. Lifespan calls `get_runtime_settings(validate=True)`. Production/development + disable → `ConfigurationError`.

## Hardcoded credentials

None found in bootstrap/auth paths. Tests use helpers (not production defaults).

## Uncovered / residual risk

- Live browser multi-role UI against a running **policy** server: **未确认** (no policy service on expected port in this environment; 8765 is unrelated `wtpy`).
- Full OpenAPI lock-down in production: deferred per matrix.
