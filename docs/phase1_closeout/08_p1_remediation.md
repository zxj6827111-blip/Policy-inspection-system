# P1 Remediation after Independent Acceptance FAIL

## Acceptance P1 items addressed

1. **Formal definition doc ≠ code**  
   - Code: all `AGENCY-*`/`ORG-*` never formal; `human_confirmed` no longer blocked by `allow_customer_output` for other rules; auto path still STRICT+allow.  
   - Docs updated: `docs/phase1/04_rule_design.md`, `docs/phase1_closeout/03_count_semantics.md`.

2. **Task list column 「问题」 bound to total**  
   - Headers: 正式问题 + 线索  
   - Cells/rail use `formal_finding_count`  
   - `list_jobs` live-recomputes counts so list API is not stale denormalized totals only.

## Tests

`tests/test_formal_definition_alignment.py`

## Not in this fix (P2 remain)

- logout-simple CSRF  
- OpenAPI public  
- real_auth mark warning  
- Live multi-role UI against production listener  
