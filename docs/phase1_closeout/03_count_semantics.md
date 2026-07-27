# Count Semantics (aligned with code — single source of truth)

## Canonical formula (`app/findings_model.is_customer_formal_problem`)

A finding is a **客户正式问题** if and only if:

1. Rule is **not** in the AGENCY-* / ORG-* family; **and**
2. One of:
   - `human_status == human_confirmed`; **or**
   - `human_status == unreviewed` **and** `machine_status == confirmed_by_rule` **and** `rule_code ∈ {META-001, DOC-001, DOC-002, DATE-001, DATE-002}` **and** `allow_customer_output == 1`.

Never formal:

- Any `AGENCY-*` or `ORG-*` (including after human_confirmed)
- `human_dismissed`, `needs_more_evidence`
- Auto path without allow flag or outside STRICT whitelist

`allow_customer_output` **only** gates the automatic path. It does **not** block non-agency/org `human_confirmed`.

## Field dictionary

| Field | Meaning |
|---|---|
| `finding_count` | Compat alias of total current findings (**not** formal). |
| `finding_total_count` | Current findings total (clues + formal). |
| `formal_finding_count` | Customer formal count (formula above). |
| `suspected_count` / `hint_count` / … | Machine-status breakdowns. |
| `human_confirmed_count` | Raw human_confirmed rows (**may include org**, ≠ formal). |

## UI

| Surface | Shows |
|---|---|
| Task list / history columns | **正式问题** = formal; **线索** = total |
| Detail stat tile | 客户正式问题 + 线索合计 hint |
| Rail card | formal only |
| Excel 问题汇总 | formal only |

## Code modules

- Gate: `app/findings_model.py`
- Aggregation: `app/finding_counts.py`
- List API live recompute: `Database.list_jobs`
