# 04 Review History and Lineage Validation

## Method

Independent DB copy: `data/phase1_independent_lineage_copy.db` (from production SHA-matched file).  
Applied Phase1 + closeout migrations on copy only. Exercised `Repository.save_record` lineage path.

Artifact: `tmp/phase1_independent_lineage_report.json`

## Scenario 1 — Confirm / rescan / supersede / inherit

1. Insert META-001 finding (v1).
2. Append two `finding_reviews`; set human to `human_dismissed` (via second review).
3. Rescan same job/document with new META-001 finding.

**Observed:**

| Check | Result |
|---|---|
| Old `is_current` | 0 |
| `superseded_by` | points to new id |
| New `human_status` | `human_dismissed` (inherited) |
| `stable_key` equal | yes (`{document_id}:META-001`) |
| `finding_reviews` on old id | still 2 |
| `review_decisions` copied to new id | yes (current decision mirror) |
| Old finding physically deleted? | **No** |

## Scenario 2 — Two reviews, no overwrite of history rows

Two INSERT rows into `finding_reviews` with `supersedes_review_id` chain.  
`review_decisions` remains one row per finding (UPSERT current) — **current** conclusion table, not append-only; history is `finding_reviews`.

| Check | Result |
|---|---|
| Two history rows retained | Yes |
| Current human after second review | `human_dismissed` |
| UPDATE overwriting old `finding_reviews` rows | **Not used** for history |

## Scenario 3 — Rule version change

Rescan with `rule_version=2.0.0`.

| Check | Result |
|---|---|
| Prior rows retained | Yes (≥3 META-001 rows) |
| Current version | 2.0.0 |
| Human still inherited | `human_dismissed` |
| Reviews on original id | still ≥2 |
| New result “forges” old human without inheritance? | Inheritance applied; not unreviewed wipe |

## Gates

```json
{
  "reviews_not_deleted": true,
  "old_not_current": true,
  "superseded_set": true,
  "human_inherited": true,
  "two_reviews_kept": true,
  "version_changed_current": true,
  "human_still_inherited_after_version": true,
  "physical_findings_retained": true
}
```

## Code path note

`save_record` comment: do **not** copy `finding_reviews` onto new ids; join via `stable_key` / `superseded_by`. Evidence API must expose history via lineage (covered in phase1 closeout tests; live UI **未确认** without policy server).

## FAIL trigger

No path found that **deletes** `finding_reviews` during rescan. Gate **PASS** for history retention.
