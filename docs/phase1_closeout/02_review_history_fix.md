# Review History Fix

## Before

`Repository.save_record` deleted `finding_evidence`, `finding_reviews`, `review_decisions`, then re-inserted findings. Human status could be copied, but **audit rows were destroyed**.

## After

1. Load current findings for `(job_id, document_id)`.
2. Insert new findings as `is_current=1` with new evidence rows only.
3. Mark prior rows `is_current=0`, set `superseded_by` + `superseded_at`.
4. **Never DELETE** `finding_reviews`.
5. Mirror latest `review_decisions` onto new finding id for API convenience; original decision row on old id remains.
6. Inherit non-`unreviewed` human status by `rule_code` / stable key from any prior row.

## Guarantees

| Requirement | Status |
|---|---|
| Reviews append-only | Yes |
| Rescan does not delete history | Yes |
| Human status inherits | Yes |
| Rule version on new row independent | Yes |
| Superseded linkage | `superseded_by` |
| review_decisions retained | Yes (old + mirrored new) |

## Tests

`tests/test_phase1_closeout.py::test_rescan_keeps_review_history_and_inherits_human_status`

`tests/test_phase1_closeout.py::test_dismissed_finding_reappears_with_inherited_status`
