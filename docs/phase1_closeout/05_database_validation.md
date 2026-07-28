# Database Copy Validation

## Production DB

- Path: `data/policy_inspector.db`
- SHA256 (unchanged through closeout): `13e6a8a412f69c13d149674a31076854c966f5b151e670fc9e22d0f53ab77af3`
- **Not modified** by closeout scripts

## Copy used

- `data/phase1_closeout_copy.db` (from prior phase1 copy / source)
- Report: `tmp/phase1_closeout_migration_report.json`

## Counts after phase1+closeout (copy)

| Table | Count |
|---|---:|
| policy_documents | 269 |
| document_snapshots | 346 |
| rule_findings | 21 |
| review_decisions | 5 |
| finding_reviews | 5 |
| finding_evidence | 84 |
| organizations | 10 |
| roles | 5 |
| schema_migrations | 2 |

## Idempotency

Second `run_phase1_migration` + `run_closeout_migration` on same copy succeeds without row loss.

## Command

```bash
python scripts/run_closeout_migration_copy.py
```
