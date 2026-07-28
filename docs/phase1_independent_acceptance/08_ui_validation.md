# 08 UI Validation (Read-only)

## Method limits

- No policy-inspection service listening on product ports in this environment at baseline.
- Port 8765 later observed as **unrelated** `wtpy.apps.astock` — **not** used for acceptance UI.
- Validation is **static** (templates/JS) + API contract review + pytest UI-related assertions where present.
- **Did not** mutate production review data.
- Live login/role click-through: **未确认**

## Static findings (UTF-8 source)

| Item | Present? |
|---|---|
| 客户正式问题 tile → `formal_finding_count` | Yes |
| 线索合计 hint from total | Yes |
| Review chips: 待复核 / 数据不足 / 采集异常 / 自动排除 / 人工排除 / 客户正式 | Yes (labels in template/JS) |
| 规则版本 / 机器状态 / 人工状态 strings | Yes in JS/evidence paths |
| Job list column **问题** | Bound to `finding_count` (**total**, not formal) — P1 |
| Fixed “页面值/正文值” only for ORG | ORG evidence uses 公开主体/实际发文机关/联合/代字 fields in rules |

## Excel entry

Export route requires login + `export:read`; audited. Sheet set includes formal-filtered summary and detailed sheets with formal flag.

## Section conclusion

**PARTIAL / 未确认 for live multi-role browser.** Static labeling of primary detail tile is correct; list column ambiguity remains.
