# Phase 1 Acceptance Report

## Gate checklist

| Gate | Status |
|---|---|
| All write APIs authenticated (when auth enabled) | PASS |
| Permission matrix tests | PASS |
| HTTPS + non-root deploy guidance documented | PASS (docs; not applied to live host in this run) |
| AGENCY-001 not in customer formal output | PASS |
| 普委办〔2013〕41号 no ORG-001 mis-fire | PASS |
| Rules carry versions | PASS |
| Machine/human states separated | PASS |
| Evidence locates fields/excerpts | PASS |
| Historical rows preserved on copy migration | PASS |
| Tests green | see final pytest |
| Production DB not destructively migrated | PASS (SHA unchanged) |
| No commit/push | PASS |

## Environment recorded at start

- branch: `codex/fix-snapshot-result-collision`
- HEAD: `be74df72598ae9ae0feb3a8fd9987c84c5bd8b67`
- DB SHA256: `13e6a8a412f69c13d149674a31076854c966f5b151e670fc9e22d0f53ab77af3`

## Residual risks

- Production still needs ops to enable HTTPS, close 6001, non-root, bootstrap admin
- Job `finding_count` still counts all findings (not only customer formal)
- Auth disabled env must never be set in production
- Organization knowledge is Putuo-first; other districts still thin
- Resave deletes prior `finding_reviews` rows for same job/document (human_status preserved on finding)

## Explicit non-actions

- Production database not migrated in-place
- Production service not restarted
- No git commit / push
