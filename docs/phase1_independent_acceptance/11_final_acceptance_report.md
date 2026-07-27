# 11 Final Independent Acceptance Report

## 1. Overall verdict

# **FAIL**

Not all mandatory gates are simultaneously clean. Critical history/migration/auth/agency-sample/pytest/prod-integrity gates largely hold, but **count-semantics design↔code mismatch** and **UI list column ambiguity** violate the “all statistics consistent / no total presented as formal problem count” bar when read strictly. Acceptor does **not** substitute “basically OK”.

## 2. Branch / HEAD / worktree

| Item | Value |
|---|---|
| Branch | `codex/fix-snapshot-result-collision` |
| HEAD | `be74df72598ae9ae0feb3a8fd9987c84c5bd8b67` |
| Worktree | dirty (Phase1 + closeout uncommitted); acceptance added only `docs/phase1_independent_acceptance/**` and `tmp/phase1_independent*` (+ probe scripts under tmp) |

## 3. Patch fingerprint start vs end

| | Start | End | Match |
|---|---|---|---|
| production code | `402e1590…8404d` | same | **Yes** |
| tests | `89061b2f…a5dd94` | same | **Yes** |
| uncommitted patch snapshot | `cb395c0a…47678e` | same | **Yes** |

## 4. Production DB SHA start vs end

`13e6a8a412f69c13d149674a31076854c966f5b151e670fc9e22d0f53ab77af3` — **unchanged** (matches expected).

## 5. Security and permissions

- Write APIs authenticated; CSRF on `require_write` paths.
- production/development cannot disable auth; unset APP_ENV → production fail-closed.
- No default admin password; PBKDF2 hashing; health hides DB path.
- P2: `logout-simple` without CSRF; public OpenAPI/UI shell.
- Live multi-role HTTP against policy server: **未确认**.

**Security core gates: PASS** (with P2 residuals).

## 6. Review history

Lineage copy scenarios: reviews retained, supersede/inherit works, no delete path found. **PASS**.

## 7. Count semantics

AGENCY-001 / ORG-002 excluded from formal: **PASS**.  
Shared API recompute: **PASS**.  
Design formula vs `is_customer_formal_problem` (STRICT whitelist, allow required for human_confirmed, ORG-001 human can formal): **FAIL vs written design**.  
Job list “问题” = `finding_count` total: **FAIL vs “must not present total as formal” spirit / operator risk**.

## 8–9. AGENCY / ORG / samples

| Sample | Result |
|---|---|
| 普委办〔2013〕41号 | No ORG-001; ORG-002 hint; not formal; evidence rich |
| 普宜办 / 长街办 | Knowledge match; no false ORG-001 |
| AGENCY-001 | suspected, allow=0, not formal |
| rule_version on ORG/AGENCY | present |

**PASS** for specified samples.

## 10. Migration (independent copy)

Idempotent; no loss of documents/snapshots/findings/review_decisions; knowledge not duplicated. **PASS**.

## 11. Tests

**178 passed, 0 failed**, ~107s. Prod DB hash stable. **PASS** (tests only).

## 12. Frontend

Static detail tile correct; live role UI **未确认**. List column P1. **PARTIAL**.

## 13. Runbook

Complete and actionable. **PASS** (review only).

## 14. Issues

### P0

None confirmed that delete review history, open unauthenticated writes, or allow production auth disable.

### P1

1. **Formal definition divergence** between closeout design text / acceptance formula and `is_customer_formal_problem` (STRICT set; `allow_customer_output` gates human_confirmed; ORG-001 not in NON_CUSTOMER while design says all ORG-*).
2. **Job list “问题” column uses `finding_count` (total)** while detail uses formal — operator confusion risk against count semantics gate.

### P2

1. `logout-simple` without CSRF.
2. Public OpenAPI/docs.
3. Pytest unknown mark `real_auth`.
4. Role extractor string noise on joint issuers (still correct ORG-002 path).
5. Runbook assumes committed/tag deploy; current tree still dirty.
6. Live UI/service smoke for this product **未确认** in environment.

## 15–18. Integrity of acceptance process

| Question | Answer |
|---|---|
| Modified production code? | **No** |
| Modified production database? | **No** |
| Restarted production policy service? | **No** (none bound; unrelated 8765 not touched) |
| commit / push? | **No** |

## 19. Deliverables

```text
docs/phase1_independent_acceptance/
  01_scope_and_baseline.md
  02_code_review.md
  03_security_and_permissions.md
  04_review_history_validation.md
  05_count_semantics_validation.md
  06_org_rule_validation.md
  07_migration_validation.md
  08_ui_validation.md
  09_test_report.md
  10_runbook_review.md
  11_final_acceptance_report.md

tmp/phase1_independent_acceptance_summary.json
tmp/phase1_independent_acceptance_commands.txt
tmp/phase1_independent_acceptance_baseline.json
tmp/phase1_independent_acceptance_endstate.json
tmp/phase1_independent_acceptance_migration.json
tmp/phase1_independent_acceptance_pytest.txt
tmp/phase1_independent_lineage_report.json
```

## 20. Allow Git formal commit and production cutover prep?

**No — not while overall FAIL.**  
Fix or formally amend design+UI for P1 count semantics, re-run independent acceptance, then consider commit/cutover prep.
