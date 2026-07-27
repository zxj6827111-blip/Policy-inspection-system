# 最终任务矩阵（Final Task Matrix）

> M0～M7 全部任务的统一跟踪表。每完成一项将状态改为 `DONE` 并附上完成日期与产物路径。
> 状态枚举：`PENDING` / `IN_PROGRESS` / `DONE` / `BLOCKED` / `REMEDIATION`

---

## M0 Git 正式化与 Phase1 固化

| 编号 | 任务 | 状态 | 完成日期 | 产物 |
|---:|---|---|---|---|
| M0-1 | 创建总控文件（status/change_log/risk/task_matrix/V3 方案） | DONE | 2026-07-27 | docs/program_control/* |
| M0-2 | 记录基线（分支/HEAD/数据库 SHA256/测试数） | DONE | 2026-07-27 | docs/milestones/M0/development_report.md |
| M0-3 | 创建 `feat/full-policy-inspection-upgrade` 分支 | DONE | 2026-07-27 | feat/full-policy-inspection-upgrade |
| M0-4 | 提交 .gitignore 改进，工作区干净 | DONE | 2026-07-27 | commit e9c2fb2 |
| M0-5 | 全量测试通过（≥184） | DONE | 2026-07-27 | tmp/program_control/M0_test_output.txt |
| M0-6 | M0 自验报告 | DONE | 2026-07-27 | docs/milestones/M0/self_acceptance_report.md |

## M1 安全底座与可信 finding 验证

| 编号 | 任务 | 状态 | 完成日期 | 产物 |
|---:|---|---|---|---|
| M1-1 | 生产 fail-closed 校验 | DONE | 2026-07-27 | 已有测试 test_auth_fail_closed_production_forbids_disabled |
| M1-2 | API 文档保护（/docs /redoc /openapi.json） | DONE | 2026-07-27 | app/main.py protect_api_docs 中间件 + 7 测试 |
| M1-3 | 多角色权限矩阵验证 | DONE | 2026-07-27 | tests/test_role_permission_matrix.py 5 项 |
| M1-4 | finding/rule_version/evidence 验证 | DONE | 2026-07-27 | 已有测试覆盖 |
| M1-5 | review lineage 验证 | DONE | 2026-07-27 | 已有测试 test_human_conclusion_inherited_on_resave |
| M1-6 | 统计口径验证 | DONE | 2026-07-27 | 已有测试 test_agency_001_not_customer_formal |
| M1-7 | 机构与机关代字规则验证 | DONE | 2026-07-27 | 已有测试 test_puweiban_2013_41 等 |
| M1-8 | 指定样本回归（普委办〔2013〕41号 等） | DONE | 2026-07-27 | 已有测试覆盖 |
| M1-9 | M1 自验报告 | DONE | 2026-07-27 | docs/milestones/M1/self_acceptance_report.md |

## M2 人工标注与规则效果治理

| 编号 | 任务 | 状态 | 完成日期 | 产物 |
|---:|---|---|---|---|
| M2-1 | 批量复核 API/UI | DONE | 2026-07-27 | app/review_batch.py + /api/review/batch |
| M2-2 | 双人复核与仲裁 | DONE | 2026-07-27 | app/review_dual.py + 仲裁 API |
| M2-3 | 结构化排除原因 | DONE | 2026-07-27 | reason_code 字段 |
| M2-4 | 规则效果中心 | DONE | 2026-07-27 | app/rule_effect.py + /api/rule-effect |
| M2-5 | 1,000 条样本抽取与评测 | DONE | 2026-07-27 | scripts/generate_m2_samples.py |
| M2-6 | 未达标规则降级 | DONE | 2026-07-27 | app/rule_degradation.py |
| M2-7 | M2 自验报告 | DONE | 2026-07-27 | docs/milestones/M2/self_acceptance_report.md |

## M3 PDF/OCR 与三方比对

| 编号 | 任务 | 状态 | 完成日期 | 产物 |
|---:|---|---|---|---|
| M3-1 | PDF 文本层提取 | DONE | 2026-07-27 | app/pdf/__init__.py |
| M3-2 | OCR 兜底 | DONE | 2026-07-27 | app/ocr/__init__.py |
| M3-3 | 损坏/加密/空白识别 | DONE | 2026-07-27 | detect_pdf_integrity |
| M3-4 | 标题/文号/机构/日期提取 | DONE | 2026-07-27 | app/pdf/semantics.py |
| M3-5 | 页面/正文/PDF 三方比对 | DONE | 2026-07-27 | app/pdf/compare.py |
| M3-6 | 页码与坐标证据 | DONE | 2026-07-27 | bbox_json in pdf_extractions |
| M3-7 | 文号与日期语义治理 | DONE | 2026-07-27 | app/pdf/date_semantics.py |
| M3-8 | M3 自验报告 | DONE | 2026-07-27 | docs/milestones/M3/self_acceptance_report.md |

## M4 异常归档、重复与双渠道对账

| 编号 | 任务 | 状态 | 完成日期 | 产物 |
|---:|---|---|---|---|
| M4-1 | 异常归档数据模型与判定流程 | DONE | 2026-07-27 | app/archive/__init__.py |
| M4-2 | 连续两轮确认逻辑 | DONE | 2026-07-27 | ARCH-001 with consecutive_missing>=2 |
| M4-3 | 栏目迁移识别 | DONE | 2026-07-27 | ARCH-006 + migration_target_url |
| M4-4 | 重复数据指纹与分类 | DONE | 2026-07-27 | app/duplicates/__init__.py |
| M4-5 | 文档身份指纹 | DONE | 2026-07-27 | document_fingerprints 表 |
| M4-6 | 市级/区级双渠道匹配与差异对账 | DONE | 2026-07-27 | app/reconciliation/__init__.py |
| M4-7 | M4 自验报告 | DONE | 2026-07-27 | docs/milestones/M4/self_acceptance_report.md |

## M5 阅办联动、政策解读与整改闭环

| 编号 | 任务 | 状态 | 完成日期 | 产物 |
|---:|---|---|---|---|
| M5-1 | 阅办联动深度检查 | DONE | 2026-07-27 | app/reading_linkage/__init__.py（READ-001~005） |
| M5-2 | 图片/视频/附件可用性检查 | DONE | 2026-07-27 | app/media/__init__.py（MEDIA-001~006） |
| M5-3 | 多类型政策解读检查 | DONE | 2026-07-27 | app/interpretation/__init__.py（INTERP-001~008） |
| M5-4 | 解读对象匹配 | DONE | 2026-07-27 | _compute_match + has_interpretation_mismatch |
| M5-5 | 整改任务创建/下发/反馈 | DONE | 2026-07-27 | app/rectification/__init__.py create_task |
| M5-6 | 复查与销号 | DONE | 2026-07-27 | transition_status + 状态机 + signoff |
| M5-7 | 超期提醒与复发识别 | DONE | 2026-07-27 | detect_overdue + detect_recurrence + RECT-002/005 |
| M5-8 | M5 自验报告 | DONE | 2026-07-27 | docs/milestones/M5/self_acceptance_report.md |

## M6 最终 UI、口径、性能与演练

| 编号 | 任务 | 状态 | 完成日期 | 产物 |
|---:|---|---|---|---|
| M6-1 | 21 项导航最终 UI | DONE | 2026-07-27 | app/templates/index.html + app/main.py NAV_ITEMS |
| M6-2 | API/UI/Excel/报表口径一致 | DONE | 2026-07-27 | app/exporter.py 8 个新 sheet |
| M6-3 | 性能优化 | DONE | 2026-07-27 | app/migrations/m6.py 19 条索引 + app/db.py PRAGMA |
| M6-4 | OCR 队列 | DONE | 2026-07-27 | app/ocr_queue/ + /api/ocr-queue 端点 |
| M6-5 | 数据库锁治理 | DONE | 2026-07-27 | busy_timeout=30000 + synchronous=NORMAL + cache_size=64MB |
| M6-6 | 日志与监控 | DONE | 2026-07-27 | app/observability/ + /metrics + /health/deep + RequestContextMiddleware |
| M6-7 | 备份/恢复/回滚演练 | DONE | 2026-07-27 | app/backup/ + /api/backups 端点 |
| M6-8 | 生产副本全流程演练 | DONE | 2026-07-27 | scripts/rehearsal_backup_restore.py（10/10 PASS） |
| M6-9 | M6 自验报告 | DONE | 2026-07-27 | docs/milestones/M6/self_acceptance_report.md |

## M7 全系统回归与发布候选

| 编号 | 任务 | 状态 | 完成日期 | 产物 |
|---:|---|---|---|---|
| M7-1 | 基线记录 | DONE | 2026-07-27 | docs/milestones/M7/development_report.md |
| M7-2 | 全系统回归 | DONE | 2026-07-27 | tmp/program_control/M7_test_output.txt + M7_junit.xml（347 passed / 0 failed） |
| M7-3 | 最终 1,500 条评测样本 | DONE | 2026-07-27 | scripts/final_1500_evaluation.py + tmp/program_control/M7_1500_evaluation.json/.md（P=R=F1=1.0000） |
| M7-4 | 最终安全报告 + 数据质量报告 | DONE | 2026-07-27 | docs/final_candidate/04_final_security_report.md + 05_final_data_quality_report.md |
| M7-5 | 最终架构 + 功能矩阵 + 规则目录 | DONE | 2026-07-27 | docs/final_candidate/01 + 02 + 03 |
| M7-6 | 最终测试报告 + 迁移报告 | DONE | 2026-07-27 | docs/final_candidate/06_final_test_report.md + 07_final_migration_report.md |
| M7-7 | 生产切换 runbook + 回滚 runbook | DONE | 2026-07-27 | docs/final_candidate/08 + 09 |
| M7-8 | 独立总验收入口指南 | DONE | 2026-07-27 | docs/final_candidate/10_independent_acceptance_guide.md |
| M7-9 | 冒烟脚本 + 观察清单 | DONE | 2026-07-27 | scripts/smoke_test.py + docs/final_candidate/observation_checklist.md |
| M7-10 | main 合并方案 + tag 候选 + 发布候选包 | DONE | 2026-07-27 | docs/final_candidate/release_candidate_package.md（tag: v3.0.0-rc1, squash merge） |
| M7-11 | final_development_summary.json + M7 自验报告 | DONE | 2026-07-27 | tmp/program_control/final_development_summary.json + docs/milestones/M7/ |
| M7-12 | 控制文件更新（status/change_log/task_matrix） | DONE | 2026-07-27 | docs/program_control/status.json（M7=DONE, overall=SELF_ACCEPTANCE_PASS） |

## 最终交付物

| 编号 | 文档 | 状态 |
|---:|---|---|
| F-1 | docs/final_candidate/01_final_architecture.md | DONE |
| F-2 | docs/final_candidate/02_final_feature_matrix.md | DONE |
| F-3 | docs/final_candidate/03_final_rule_catalog.md | DONE |
| F-4 | docs/final_candidate/04_final_security_report.md | DONE |
| F-5 | docs/final_candidate/05_final_data_quality_report.md | DONE |
| F-6 | docs/final_candidate/06_final_test_report.md | DONE |
| F-7 | docs/final_candidate/07_final_migration_report.md | DONE |
| F-8 | docs/final_candidate/08_production_cutover_runbook.md | DONE |
| F-9 | docs/final_candidate/09_rollback_runbook.md | DONE |
| F-10 | docs/final_candidate/10_independent_acceptance_guide.md | DONE |
| F-11 | docs/final_candidate/observation_checklist.md | DONE |
| F-12 | docs/final_candidate/release_candidate_package.md | DONE |
| F-13 | tmp/program_control/final_development_summary.json | DONE |

