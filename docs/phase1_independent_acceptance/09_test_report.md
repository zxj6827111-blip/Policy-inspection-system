# 09 Independent Test Report

## Command

```text
PYTHONDONTWRITEBYTECODE=1
APP_ENV=test
POLICY_AUTH_DISABLED=1
PYTHONPATH=<repo>
.venv/Scripts/python.exe -m pytest -q
```

Log: `tmp/phase1_independent_acceptance_pytest.txt`

## Result

| Metric | Value |
|---|---|
| Passed | **178** |
| Failed | **0** |
| Skipped | 0 (not reported) |
| Warnings | 3 (unknown mark `real_auth` ×2; pytest cache WinError 5) |
| Duration | 107.12s (wall ~108.35s) |

Matches developer claim **178 passed, 0 failed**.

## Constraints honored

- Tests not modified
- No skip injection
- Assertions not relaxed by acceptor
- Production DB SHA unchanged after suite
- No external government site scan invoked by acceptor
- No production service restart

## Caveat

Passing tests **do not alone** grant overall PASS; see code/data gates. Unknown pytest mark `real_auth` is hygiene P2, not a failure.
