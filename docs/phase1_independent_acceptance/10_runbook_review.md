# 10 Production Cutover Runbook Review

Source: `docs/phase1_closeout/production_cutover_runbook.md`  
**Review only — not executed.**

## Required topics checklist

| Topic | Present? |
|---|---|
| Maintenance window | Yes (§2) |
| Git rollback anchor | Yes (§3 HEAD file + ROLLBACK_ANCHOR) |
| Production DB backup + SHA | Yes (§4) |
| Copy migration rehearsal | Yes (§5 script) |
| Admin initialization | Yes (§6 bootstrap, no default pwd in doc beyond placeholder) |
| APP_ENV=production | Yes (§7) |
| Forbid POLICY_AUTH_DISABLED | Yes (§8) |
| HTTPS / Nginx | Yes (§9) |
| Close/restrict 6001 | Yes (§10) |
| Non-root user | Yes (§11 policyapp) |
| Production migration commands | Yes (§12 phase1 + closeout) |
| Service restart | Yes (§13) |
| Smoke tests | Yes (§14–15) |
| DB stats / logs | Yes (§16–17) |
| Code rollback | Yes (§19 git checkout anchor + restart) |
| Database rollback | Yes (§20 restore backup + sha256 check) |
| 24h observation | Yes (§21 checklist) |

## Executability notes

- Commands are concrete bash-oriented for `/opt/...` (Linux production shape).
- PM2 vs systemd called out as adapt-on-site.
- Password placeholders explicit; “never commit password”.
- Rollback conditions listed (§18).

## Gaps / residual (P2, not automatic FAIL)

- Does not pin exact accepted **git commit hash** of this dirty worktree (still uncommitted) — cutover assumes “accepted tree” exists as commit/tag first.
- Windows/dev path not covered (production is Linux-oriented) — expected.

## Section conclusion

Runbook is **complete enough to execute** on a Linux production host after code is committed/tagged. **PASS** for runbook completeness gate.
