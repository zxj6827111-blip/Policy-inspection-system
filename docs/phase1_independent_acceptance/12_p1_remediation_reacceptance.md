# P1 Remediation — Independent Re-acceptance

## Role

Independent acceptor re-check after developer P1 remediation.  
**No code fixes, no production DB writes, no service restart, no commit/push.**

## Scope

Re-verify only the two prior **P1** FAIL items and developer-requested spot checks; prior PASS areas (auth fail-closed, lineage, migration, samples, runbook) are **not re-litigated** unless regression observed.

## Environment

| Item | Value |
|---|---|
| Branch | `codex/fix-snapshot-result-collision` |
| HEAD | `be74df72598ae9ae0feb3a8fd9987c84c5bd8b67` (unchanged) |
| Production DB SHA256 | `13e6a8a412f69c13d149674a31076854c966f5b151e670fc9e22d0f53ab77af3` (**unchanged**) |
| Worktree | still dirty |

## Spot checks (requested)

| # | Check | Result |
|---|---|---|
| 1 | `ORG-001` + `human_confirmed` → formal False | **PASS** |
| 1b | `DATE-001` + `human_confirmed` + allow=0 → formal True | **PASS** |
| 2 | List UI: no bare `<th>问题</th>`; 正式问题/线索; cells use formal/total | **PASS** |
| 3 | `Database.list_jobs` / `GET /api/jobs` live `formal_finding_count` | **PASS** (temp DB: formal=1, total=3 with META auto + ORG human + clue) |
| 4 | Full pytest | **184 passed, 0 failed** (~107s); log `tmp/phase1_p1_reacceptance_pytest.txt` |
| 5 | Production DB SHA | **unchanged** |

## P1-1 Formal definition

Code `is_customer_formal_problem` + docs `03_count_semantics.md` / `04_rule_design.md` / `08_p1_remediation.md` aligned:

- All `AGENCY-*` / `ORG-*` (prefix + explicit set) never formal
- Other rules: `human_confirmed` formal even if allow=0
- Auto: STRICT + confirmed_by_rule + allow=1 + unreviewed
- dismissed / needs_more never formal

Independent case matrix: **ALL_OK True**.

## P1-2 List column

- Headers: **正式问题** + **线索** (both main and history tables)
- Cells: `formal_finding_count` / `finding_total_count`
- Rail: **客户正式问题** + note 非线索总数
- Zero sole `fmtNum(job.finding_count)` table cells in `app.js`

## Residual P2 (frozen, not blocking this re-acceptance of P1)

- logout-simple without CSRF
- public `/docs`
- `real_auth` mark warning
- live multi-role UI **未确认**
- dirty worktree before cutover commit strategy

## Integrity

| Action | Done? |
|---|---|
| Modified production code for “fix during accept”? | No |
| Modified production DB | No |
| Restarted production service | No |
| commit / push | No |

## Verdict for P1 remediation re-check

**PASS** — both prior P1 items cleared under independent verification.

## Overall Phase1+closeout posture

- Prior full-suite independent acceptance FAIL was driven by these P1s.
- With P1 cleared and no regression on retested gates, **overall acceptance for Phase1+closeout uncommitted tree is upgraded to PASS**, subject to frozen P2 and still requiring a formal commit/tag before production cutover per runbook.

## Allow Git formal commit and production cutover prep?

**Yes, for commit preparation** of the accepted dirty tree (after normal review of full diff).  
**Production cutover** still follows `production_cutover_runbook.md` (backup, copy migration, APP_ENV=production, etc.) — not executed in this acceptance.
