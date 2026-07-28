# Closeout Test Report

## Full suite

```text
178 passed, 0 failed
```

Source: `tmp/phase1_closeout_pytest.txt` (113.93s)

## Breakdown

| Suite | Notes |
|---|---|
| Legacy + Phase1 | previously 172 |
| Closeout new | +6 in `tests/test_phase1_closeout.py` |
| Total | **178** |

## Closeout cases

- rescan keeps review history + inherits human status
- dismissed reappears with inheritance
- count semantics / formal excludes AGENCY & ORG-002
- auth fail-closed matrix
- production misconfig refuses startup
- 普委办 sample still clean

## Constraints observed

- No external government website access in tests
- Production DB SHA unchanged
- No production service restart
