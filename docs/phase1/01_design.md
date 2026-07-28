# Phase 1 Design

## Scope

Security/RBAC, finding machine/human state split, AGENCY-001 customer-output freeze, organization knowledge v1, ORG rules, UI classification, acceptance pack.

Out of scope: PDF/OCR, archive anomalies, dual-channel reconciliation, rectification closed loop, destructive production migration.

## Architecture additions

- `app/auth/*` — users, roles, sessions, CSRF, audit
- `app/findings_model.py` — machine/human states, customer-formal gate
- `app/migrations/phase1.py` — idempotent schema + backfill
- `app/knowledge/organizations.py` — org/alias/symbol/relation seed
- `app/extractors/document_roles.py` — joint/forward/signature roles
- `app/rules.py` + `app/rules_legacy_agency.py` — ORG-* + legacy AGENCY-*
- `scripts/bootstrap_admin.py` — admin init without hardcoded password
- `scripts/run_phase1_migration.py` — copy-DB migration runner

## Data flow

1. Collector builds `PolicyRecord`
2. `evaluate_record(..., knowledge=...)` emits `Finding` with machine status, rule version, evidence fields
3. `Repository.save_record` persists findings + `finding_evidence`, preserves human status on rescan
4. APIs expose enriched rows; export counts **customer formal** only
5. Reviews append to `finding_reviews` and mirror legacy `review_decisions`

## Compatibility

- Legacy `review_status` retained
- `review_decisions` retained
- Auth bypass via `POLICY_AUTH_DISABLED=1` for automated legacy tests only
