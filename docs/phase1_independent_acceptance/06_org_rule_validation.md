# 06 Organization and Rule Validation

## Knowledge base

`seed_organizations` on temp DB: 10 orgs, 41 aliases, 12 symbols (idempotent on re-seed).

Lookup is **name/symbol/alias tables**, not URL whitelist.

## Sample 1 — 普委办〔2013〕41号

Page subject: 上海市普陀区人民政府  
Body: 中共上海市普陀区委办公室 / 上海市普陀区人民政府办公室转发  
Doc number: 普委办〔2013〕41号

**`evaluate_organization_rules` + knowledge:**

| Expectation | Observed |
|---|---|
| Joint forward recognized | Evidence lists joint issuers |
| 中共上海市普陀区委办公室 | Present in actual/joint fields |
| 普委办 matches 区委办 | Knowledge match evidence for 普委办 |
| No ORG-001 | **No ORG-001 emitted** |
| ORG-002 hint possible | **ORG-002 hint** emitted |
| Not formal | `allow_customer_output=False`, formal False |
| Evidence fields | 公开主体 / 实际发文机关 / 联合 / 代字 / 规则版本 / 机器·人工状态 |

**Note:** Primary issuer string concatenation from role extractor is somewhat noisy (`中共上海市普陀区委办公室上海市普陀区人民政…`) but still produces correct ORG-002 hint path without ORG-001.

## Sample 2 — 普宜办

`match_organization_by_symbol` → 上海市普陀区宜川路街道办事处.  
Synthetic record with 普宜办文号: **no erroneous ORG-001**.

## Sample 3 — 长街办

Symbol match → 上海市普陀区长征镇人民政府.  
No erroneous ORG-001 on synthetic record.

## Sample 4 — AGENCY-001

Via `legacy_agency_findings` with agency row prefixes not matching 普委办:

| Field | Value |
|---|---|
| machine_status | suspected |
| allow_customer_output | False |
| rule_version | 1.0.0 |
| is_customer_formal | False |

## Rule versions

`RULE_VERSIONS` includes ORG-001..005 and AGENCY-001..003 at `1.0.0`; findings carry `rule_version`.

## Unknown org policy

ORG rules use suspected/hint/insufficient style with `allow_customer_output=False` for org codes reviewed; no auto `confirmed_by_rule` for ORG in STRICT set.

## Hardcoded test-only sample?

Seed includes 普委办 / 普宜办 / 长街办 as **knowledge rows** (production seed data), not `if title == fixture` branches in evaluators. Acceptable as domain knowledge; not a unit-test monkeypatch.

## Section conclusion

Specified samples: **PASS** for no ORG-001 on 普委办 sample, symbol matches, AGENCY not formal.
