# 独立总体验收报告（Final Independent Acceptance — 重新总验）

| 字段 | 值 |
|------|-----|
| 项目 | Policy-inspection-system |
| 验收角色 | 独立总体验收 AI |
| 验收日期 | 2026-07-28 |
| 轮次 | **重新总验**（前次 FAIL 后 P0/P1 整改） |
| 验收分支 | `feat/full-policy-inspection-upgrade` |
| 验收 HEAD | `79dacc0e0cc8e210d5d72c7042266e3568f83135` |
| 工作区 | **dirty（整改未 commit）** |
| 工作区 DB SHA256 | `1EAEC1D8957E4D1250770F688D40A93E9D51387C24EC03BA9C0F19095E6434CE` |
| 结论 | **FAIL** |

```text
FINAL_INDEPENDENT_ACCEPTANCE = FAIL
FINAL_INDEPENDENT_ACCEPTANCE_PASS = false
READY_FOR_PRODUCTION_CUTOVER = false
```

> 未修改业务代码、未修改测试、未执行生产切换、未 commit/push/merge。

---

## 1. 开始条件

| 条件 | 结果 |
|------|------|
| M0–M7 self_acceptance PASS / DONE | **满足** |
| status.json `final_independent_acceptance` | `PENDING`（含 remediation CLOSED_PENDING_RE_ACCEPTANCE） |
| 前次 FAIL 整改声明 | 已提交关闭报告，进入本轮重验 |

**开始条件满足**，执行重新总验。

---

## 2. 前次 P0/P1 关闭 — 独立复核

| ID | 声明 | 独立结果 | 证据 |
|----|------|----------|------|
| R-P0-1 CSRF | CLOSED | **PASS** | `PUT /api/system-settings/{key}`、`POST /api/backups`、`logout-simple` 使用 `require_write`（`app/main.py`）；`tests/test_csrf_admin_writes.py` 纳入全量 388 |
| R-P0-2 评测引擎 | CLOSED | **PASS（方法论）** | `scripts/final_1500_evaluation.py` import 生产模块；独立复跑 `engine=production_rule_modules`，P=R=F1=1.0，1500/74 |
| R-P0-3 规则目录 | CLOSED | **PASS** | 目录表为 DOC/ATT/PDF/ARCH…；ARCH-001=连续两轮消失；DB 74 条；旧码仅出现在「已删除」修订说明 |
| R-P1-1 HEAD | CLOSED | **PASS（文档）** | 文档写 79dacc0；`git rev-parse HEAD` 一致 |
| R-P1-2 证据 KPI | CLOSED | **PASS（能力）** | `app/evidence_completeness` + API；workspace 度量 complete=21/21 rate=1.0 |
| R-P1-3 安全残余 | CLOSED | **PASS** | `/metrics`→`require_login`；`auth.login_failed` 审计；cutover §10 non-root/HTTPS/6001 |
| R-P1-4 M6 测试 | CLOSED | **PASS** | `tests/test_m6_observability_ocr_backup.py` 存在且全量绿 |
| R-P1-5 INTERP | CLOSED | **PASS** | 评测 INTERP 无 FN；解释模块已补字段/分支 |

---

## 3. 本轮独立执行（对照指南 12 步）

| 步骤 | 结果 | 说明 |
|------|------|------|
| 1 status.json M0–M7 | **PASS** | 全部 DONE |
| 2 生产边界六项 false | **PASS（声明）** | status 均为 false；本机未触碰生产 |
| 3 git 分支/HEAD/干净 | **FAIL** | 分支与 HEAD 正确；**`git status` 非空** — 全部整改仅在工作区，HEAD 仍为 M7 提交，**最终候选树不可仅从 git 复现** |
| 4 工作区 DB SHA256 | **PASS** | `1EAEC1D8…6434CE` 与指南一致 |
| 5 全量回归 | **PASS** | 独立 `388 passed / 0 failed`（227.47s），产物 `tmp/program_control/final_reacceptance_pytest.txt` |
| 6 1500 评测 | **PASS（引擎+脚本门禁）** | 独立复跑 PASS；`engine=production_rule_modules`；见残余说明 |
| 7 备份恢复演练 | **PASS** | overall PASS |
| 8 冒烟 smoke | **未作为单独阻断** | 本轮未起常驻服务跑 15/15；CSRF/M6 API 已由 pytest 覆盖 |
| 9 最终交付文档 | **PASS** | final_candidate 齐全；规则目录已对齐 |
| 10 规则 74 | **PASS** | DB=74；与目录活动规则一致 |
| 11 导航 21 | **PASS** | `NAV_ITEMS` 21 项（overview…settings） |
| 12 安全机制 | **PASS** | CSRF 写覆盖、docs 生产保护（既有）、fail-closed（既有）、metrics 登录 |

---

## 4. 失败门禁（本轮）

### F-RE-1 — P0：最终候选不可复现（Git 完整性）

**门禁：** Git 与完整性 / 指南步骤 3「工作区干净」/ 可封板树。

**证据（独立）：**

```text
分支: feat/full-policy-inspection-upgrade
HEAD: 79dacc0e0cc8e210d5d72c7042266e3568f83135
工作区: 多文件 Modified + Untracked（整改代码/测试/文档均未入库）
```

未提交示例（非穷尽）：

- `app/main.py`、`app/interpretation/__init__.py`、`app/observability/__init__.py`、`app/reconciliation/__init__.py`
- `app/evidence_completeness/`（新）
- `scripts/final_1500_evaluation.py`
- `tests/test_csrf_admin_writes.py`、`tests/test_evidence_completeness.py`、`tests/test_m6_observability_ocr_backup.py`
- `docs/final_candidate/*`、`docs/program_control/status.json` 等

**影响：**

1. 仅 checkout `79dacc0` **得不到**已关闭 P0 的代码（仍含 CSRF 缺口与旧评测脚本）。
2. 无法在干净树上打 tag / 做可审计 cutover。
3. 独立验收无法对「单一 git 对象」背书。

**严重级别：** **P0**（阻断 PASS 与生产切换授权）

---

## 5. 残余观察（不单独构成唯一 FAIL，须在封板前消化）

| ID | 说明 | 级别 |
|----|------|------|
| O-1 | 1500 样本仍为**合成构造**以适配生产引擎；P=1.0 证明引擎与样本自洽，**不等于**生产真实语料/人工标签 precision | 观察 / 上线后持续评测 |
| O-2 | 工作区 evidence 完整率 100% 基于 **21** 条 finding，样本偏少 | 观察 |
| O-3 | 生产 DB SHA256 / 远端进程未在本环境实机核验（仅文档与边界声明） | 切换前运维核验 |
| O-4 | 登录失败审计 `actor_username` 在 actor=None 时可能为空串（依赖 detail.resource_id=username） | P2 |

---

## 6. 通过的硬门禁（技术面）

| 指标/门禁 | 结果 |
|-----------|------|
| 自动化测试全部通过 | **PASS** 388/0 |
| 未授权业务写接口 | **PASS** |
| CSRF 高权限写 | **PASS**（相对前次 FAIL 已关闭） |
| API 文档生产保护 / fail-closed / RBAC 主体 | **PASS**（回归覆盖） |
| 规则目录 vs DB | **PASS** |
| 备份演练 | **PASS** |
| P0 技术项（CSRF/引擎/目录）相对前次 | **已关闭** |

| 指标 | 本轮态度 |
|------|----------|
| 合成评测 precision 数字 | 脚本门禁 PASS；**真实业务 precision 未在本轮用人工标签闭环**（O-1） |
| 证据完整率 100% | KPI **已实现**且当前库 100%；规模有限（O-2） |

---

## 7. 结论

```text
FAIL
```

**原因一句话：** 前次 P0/P1 **技术整改已独立核实关闭**，但整改**未进入 git 提交**，工作区 dirty，**最终树不可复现**，不得 PASS、不得授权生产切换。

**下一步（最小）：** 见 `docs/final_acceptance/remediation_list.md` — 提交整改、工作区干净、更新控制面指向新 commit 后 **再次独立总验**。

---

## 8. 产物

- `docs/final_acceptance/final_acceptance_report.md`（本文件，覆盖前次）
- `docs/final_acceptance/remediation_list.md`（本轮最小清单）
- `tmp/program_control/final_acceptance_summary.json`
- 独立运行痕迹：`tmp/program_control/final_reacceptance_pytest.txt`、`final_reacceptance_1500.txt`
