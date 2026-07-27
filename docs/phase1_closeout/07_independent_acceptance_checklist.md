# Independent Acceptance Checklist

Use this list for a reviewer who did **not** implement Phase1.

## Code & tests

- [ ] `git rev-parse HEAD` matches delivery note
- [ ] `pytest tests` → 178 passed
- [ ] `tests/test_phase1_closeout.py` all green
- [ ] No commit/push required for acceptance of workspace artifacts

## Security

- [ ] `APP_ENV=production POLICY_AUTH_DISABLED=1` refuses start
- [ ] Unset `APP_ENV` defaults to production + auth on
- [ ] All mutating API routes require auth+CSRF when auth enabled
- [ ] `/health` has no DB absolute path
- [ ] Route matrix reviewed: `route_permission_matrix.md`

## Data model

- [ ] Rescan does not delete `finding_reviews`
- [ ] `superseded_by` links old→new findings
- [ ] Counts: formal ≠ total; AGENCY-001 excluded from formal
- [ ] Copy migration report reviewed; prod SHA unchanged

## Rules

- [ ] 普委办〔2013〕41号: no ORG-001; optional ORG-002 hint; not formal
- [ ] 普宜办 / 长街办 knowledge match without false ORG-001

## Ops readiness (docs only until cutover)

- [ ] `production_cutover_runbook.md` complete
- [ ] Admin bootstrap uses env password (no default)
- [ ] HTTPS / 6001 / non-root steps understood

## Sign-off

| Role | Name | Date | Result |
|---|---|---|---|
| Independent reviewer |  |  |  |
| Delivery owner |  |  |  |
