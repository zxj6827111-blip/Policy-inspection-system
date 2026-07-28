# 07 Database Migration — Independent Copy

## Method

1. SHA256-verify production DB  
2. `Copy-Item` → `data/phase1_independent_acceptance_copy.db` (hash match)  
3. Phase1 migration (`force_backfill=True`)  
4. Closeout migration (`force=True`)  
5. `seed_organizations`  
6. Repeat Phase1 (no force backfill) + closeout (`force=False`) + seed  
7. `Database.initialize()`  

**Did not** migrate production file. Artifact: `tmp/phase1_independent_acceptance_migration.json`

## Key table counts

| Table | before | after 1st | after 2nd | after init |
|---|---:|---:|---:|---:|
| policy_documents | 269 | 269 | 269 | 269 |
| document_snapshots | 346 | 346 | 346 | 346 |
| scan_jobs | 18 | 18 | 18 | 18 |
| rule_findings | 21 | 21 | 21 | 21 |
| review_decisions | 5 | 5 | 5 | 5 |
| finding_reviews | (n/a) | 5 | 5 | 5 |
| finding_evidence | (n/a) | 84 | 84 | 84 |
| users | (n/a) | 0 | 0 | 0 |
| roles | (n/a) | 5 | 5 | 5 |
| rule_versions | (n/a) | 14 | 14 | 14 |
| organizations | (n/a) | 10 | 10 | 10 |
| organization_aliases | (n/a) | 41 | 41 | 41 |
| document_symbols | (n/a) | 12 | 12 | 12 |
| organization_relations | (n/a) | 4 | 4 | 4 |

## Idempotency

| Check | Result |
|---|---|
| Second pass table counts stable | True |
| Org seed idempotent | True |
| Roles unique (5) | admin, auditor, operator, reviewer, viewer |
| Duplicate rule_versions | none observed |
| Docs/snapshots/findings/reviews lost | **No** |

## Section conclusion

**PASS** for independent copy migration idempotency and no loss of core business rows.
