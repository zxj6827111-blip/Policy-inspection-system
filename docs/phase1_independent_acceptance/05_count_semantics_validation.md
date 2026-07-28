# 05 Count Semantics Validation

## Shared implementation

`app/finding_counts.compute_counts_from_rows` + `app/findings_model.is_customer_formal_problem`.

## Independent fixture (10 current + 1 superseded)

Statuses covered: confirmed_by_rule, suspected, hint, insufficient_data, collection_error, auto_excluded, human_confirmed, human_dismissed, AGENCY-001, ORG-002 hint, is_current=0.

**Result:**

```
finding_total_count=10
formal_finding_count=2   # META-001 auto formal + human_confirmed H
suspected_count=4
hint_count=2
insufficient_data_count=1
collection_error_count=1
auto_excluded_count=1
human_dismissed_count=1
human_confirmed_count=1
finding_count == finding_total_count
```

AGENCY-001 and ORG-002 **not** in formal. Superseded row excluded.

## Formal gate checks

| Case | formal? |
|---|---|
| META-001 confirmed_by_rule unreviewed allow1 | True |
| human_dismissed | False |
| AGENCY-001 any | False |
| ORG-002 human_confirmed | False (NON_CUSTOMER) |
| ORG-001 human_confirmed allow1 | **True** (code; design “All ORG-*” would say False) |
| human_confirmed allow0 | **False** (code; pure design bullet 1 would say True) |
| non-strict rule confirmed_by_rule allow1 | **False** (STRICT whitelist) |

## Surface alignment

| Surface | Behavior | Verdict |
|---|---|---|
| `GET /api/jobs/{id}` | live `compute_counts_from_rows` + meanings | Aligned |
| `GET /api/jobs/{id}/counts` | same module | Aligned |
| Detail UI tile | label **客户正式问题** → `formal_finding_count`; hint 线索合计 → total | Aligned |
| Job **list** column **问题** | `finding_count` (total) | **Risk / P1** — not labeled formal, but ambiguous |
| Excel formal sheets | filter `is_customer_formal` | Aligned with code gate |
| Excel detail | all findings + formal flag column | Aligned |
| review queue / rail | uses finding_count for rail visibility | Total-oriented |

## Gate vs acceptance text

Acceptance requires formal = human_confirmed **OR** (allow=1 AND confirmed_by_rule), and AGENCY-001/ORG-002 not formal.

- AGENCY-001 / ORG-002 not formal: **PASS**
- Shared module API/Excel detail tile: **mostly PASS**
- Exact formula identity with design + list column clarity: **FAIL aspects recorded as P1**

**Section conclusion:** **CONDITIONAL** — operational formal exclusion of AGENCY/ORG-002 holds; documentation/code formula mismatch and list-column ambiguity prevent clean full PASS on “all statistics consistent with written design.”
