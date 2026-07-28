# 最小整改清单（重新总验后 — 开发关闭状态）

| 字段 | 值 |
|------|-----|
| 关联前次结论 | FAIL（工作区 dirty） |
| 开发关闭日期 | 2026-07-28 |
| 开发侧 R-RE-P0-1 | CLOSED（已 commit，树可复现） |
| 独立验收 | 仍须重新总验；不得自称 PASS |
| 生产切换 | 禁止（直至独立 PASS） |

## 已关闭（开发）

- 前次技术 P0/P1
- R-RE-P0-1 入库
- 失败登录 actor_username
- 证据 KPI 空库元数据
- logout CSRF 测试
- gitignore data/backups/

## 独立重验最小命令

1. git status --short（须空）
2. git rev-parse HEAD（与 status.json remediation head 一致）
3. pytest tests/ -q（391+ / 0 failed）
4. python scripts/final_1500_evaluation.py（engine=production_rule_modules）
