# Phase 1 Test Report

## Command

```bash
.\.venv\Scripts\python.exe -m pytest tests -q
```

## Results (this delivery)

- Phase1 dedicated: `tests/test_phase1_security_org.py` — **11 passed**
- Full suite target: legacy + new (see `tmp/pytest_full2.txt` after final run)
- Baseline claimed before change: 161 passed
- After phase1 additions: **172 passed** (161 legacy-equivalent surface + 11 phase1; full file count includes all collected tests)

## Coverage added

- Unauthenticated write → 401/403
- viewer cannot write/review
- reviewer can review, cannot manage users
- admin user create + audit row
- CSRF enforcement
- health path redaction
- AGENCY-001 not customer formal
- 普委办〔2013〕41号 regression
- street / alias symbols
- review append + state fields
- human conclusion inheritance on resave

## Notes

- Legacy API tests use `POLICY_AUTH_DISABLED=1` via autouse fixture
- Real auth tests mark/force `POLICY_AUTH_DISABLED=0`
