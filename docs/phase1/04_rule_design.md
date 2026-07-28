# Customer Formal Rule Design (aligned)

## Customer formal definition

Identical to `is_customer_formal_problem` / closeout count semantics:

1. **human_confirmed** on non-`AGENCY-*` / non-`ORG-*` rules → formal  
2. **Auto**: `confirmed_by_rule` + STRICT set `{META-001, DOC-001, DOC-002, DATE-001, DATE-002}` + `allow_customer_output=1` + still `unreviewed`

## Legacy AGENCY / ORG

- Kept for compatibility as clues  
- Forced non-customer family: **never** enter `formal_finding_count`  
- `allow_customer_output=0` on emit  

## New ORG rules (v1.0.0)

| Code | Default machine | Customer formal |
|---|---|---|
| ORG-001 … ORG-005 | suspected/hint | **never** |

## Golden sample — 普委办〔2013〕41号

- No ORG-001; optional ORG-002 hint; not formal
