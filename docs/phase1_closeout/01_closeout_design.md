# Phase1 Closeout Design

## Goals

Seal three production blockers before independent acceptance:

1. Immutable review history across rescan/lineage
2. Explicit multi-metric finding counts (formal ≠ total clues)
3. Fail-closed auth (`APP_ENV` + ban `POLICY_AUTH_DISABLED` outside `test`)

## Non-goals

- No production DB mutation in this closeout run
- No production service restart
- No git commit/push

## Modules

| Module | Role |
|---|---|
| `app/migrations/closeout.py` | lineage columns + job count columns |
| `app/finding_counts.py` | shared count semantics |
| `app/runtime_config.py` | APP_ENV + auth fail-closed |
| `app/repository.py` | supersede-not-delete finding rewrite |
| `app/main.py` | counts API, evidence review_history, startup validation |
| `tests/test_phase1_closeout.py` | closeout gates |

## Lineage model

- `stable_key = {document_id}:{rule_code}`
- On rescan: insert new current finding; set prior `is_current=0`, `superseded_by=<new_id>`
- `finding_reviews` rows remain on original `finding_id` forever
- API evidence returns full history via document_id+rule_code join
