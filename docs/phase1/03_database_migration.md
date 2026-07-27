# Phase 1 Database Migration

## Principles

- Additive only (no drop of legacy tables/columns)
- Idempotent (`schema_migrations.id = phase1_auth_findings_org_v1`)
- Validate on **copy** first
- Do not destroy production DB in this delivery

## Runner

```bash
python scripts/run_phase1_migration.py --db data/phase1_migration_copy.db --report tmp/phase1_migration_report.json --force-backfill
```

## Copy migration result (this workspace)

- Source SHA256: `13e6a8a412f69c13d149674a31076854c966f5b151e670fc9e22d0f53ab77af3`
- Copy after migration SHA256: see `tmp/phase1_migration_report.json`
- `rule_findings`: 21 → 21 (no loss)
- `review_decisions`: 5 → 5
- `policy_documents`: 269 → 269
- `document_snapshots`: 346 → 346
- Added evidence rows: 84
- Organizations seeded: 10

## New tables

users, roles, user_roles, sessions, operation_audits, rule_versions, finding_evidence, finding_reviews, review_reason_codes, organizations, organization_aliases, document_symbols, organization_relations, schema_migrations

## New columns on `rule_findings`

machine_status, human_status, rule_version, confidence, allow_customer_output, evidence_json

## Production procedure (when authorized)

1. Freeze writes / note HEAD
2. `cp data/policy_inspector.db data/policy_inspector.db.bak-$(date +%Y%m%d%H%M%S)`
3. Run migration on backup copy and verify counts
4. Run on production only with explicit approval
5. Bootstrap admin via CLI
6. Restart app as non-root behind HTTPS
