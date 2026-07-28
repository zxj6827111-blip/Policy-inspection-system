# 01 Scope and Baseline — Independent Acceptance

## Role

Independent Phase1 + Phase1 closeout acceptance. **Not** the development team. **No fixes**, **no production DB writes**, **no service restart**, **no commit/push**.

## Workspace note

Acceptance ran on:

`E:\Software Development\Policy inspection system`

(not `/opt/policy-inspection-system`). Behavior of code/tests is evaluated against the same tree; path difference is environmental only.

## Baseline (start)

| Item | Value |
|---|---|
| Branch | `codex/fix-snapshot-result-collision` |
| HEAD | `be74df72598ae9ae0feb3a8fd9987c84c5bd8b67` (matches expected) |
| Worktree | dirty (Phase1 + closeout uncommitted) |
| Production DB | `data/policy_inspector.db` |
| DB size | 6746112 bytes |
| DB SHA256 | `13e6a8a412f69c13d149674a31076854c966f5b151e670fc9e22d0f53ab77af3` (**matches expected**) |
| Python | system 3.14.3; venv 3.12.13 |
| Policy service on 6001 | **not listening** at baseline |
| Unrelated listener | later observed `python -m wtpy.apps.astock serve --port 8765` (not this product) |

## Combined fingerprints (start)

See `tmp/phase1_independent_acceptance_baseline.json`:

| Fingerprint | SHA256 |
|---|---|
| production code (app+scripts) | `402e15904d6ac243f4b2ebc6109ea2f6a07588c02fe2b512d15c91480098404d` |
| tests | `89061b2fb6d7a1d69abf464acf2c0d3407e5d8e5930172bc91fce76de4a5dd94` |
| phase1 + closeout docs | `df640015e1fccc42762c876ddea0206a6e49be535273c299159b2b1877ea0d8f` |
| uncommitted patch snapshot | `cb395c0aaf6307a47c621df294767007ecd582ebc4ce9155c8e77bdb5547678e` |

## Scope covered

Code review, security/routes, review lineage, count semantics, org/agency samples, independent migration on DB **copy**, full pytest, UI static review, runbook review, end-state integrity.

## Explicit non-actions

- No production code edits for “fixes”
- No production migration
- No Nginx/PM2 changes
- No commit / push / stash / reset / checkout overwrite
