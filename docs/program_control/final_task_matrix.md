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
| M1-1 | 生产 fail-closed 校验 | PENDING |  |  |
| M1-2 | API 文档保护（/docs /redoc /openapi.json） | PENDING |  |  |
| M1-3 | 多角色权限矩阵验证 | PENDING |  |  |
| M1-4 | finding/rule_version/evidence 验证 | PENDING |  |  |
| M1-5 | review lineage 验证 | PENDING |  |  |
| M1-6 | 统计口径验证 | PENDING |  |  |
| M1-7 | 机构与机关代字规则验证 | PENDING |  |  |
| M1-8 | 指定样本回归（普委办〔2013〕41号 等） | PENDING |  |  |
| M1-9 | M1 自验报告 | PENDING |  | docs/milestones/M1/self_acceptance_report.md |

## M2 人工标注与规则效果治理

| 编号 | 任务 | 状态 | 完成日期 | 产物 |
|---:|---|---|---|---|
| M2-1 | 批量复核 API/UI | PENDING |  |  |
| M2-2 | 双人复核与仲裁 | PENDING |  |  |
| M2-3 | 结构化排除原因 | PENDING |  |  |
| M2-4 | 规则效果中心 | PENDING |  |  |
| M2-5 | 1,000 条样本抽取与评测 | PENDING |  |  |
| M2-6 | 未达标规则降级 | PENDING |  |  |
| M2-7 | M2 自验报告 | PENDING |  | docs/milestones/M2/self_acceptance_report.md |

## M3 PDF/OCR 与三方比对

| 编号 | 任务 | 状态 | 完成日期 | 产物 |
|---:|---|---|---|---|
| M3-1 | PDF 文本层提取 | PENDING |  |  |
| M3-2 | OCR 兜底 | PENDING |  |  |
| M3-3 | 损坏/加密/空白识别 | PENDING |  |  |
| M3-4 | 标题/文号/机构/日期提取 | PENDING |  |  |
| M3-5 | 页面/正文/PDF 三方比对 | PENDING |  |  |
| M3-6 | 页码与坐标证据 | PENDING |  |  |
| M3-7 | 文号与日期语义治理 | PENDING |  |  |
| M3-8 | M3 自验报告 | PENDING |  | docs/milestones/M3/self_acceptance_report.md |

## M4 异常归档、重复与双渠道对账

| 编号 | 任务 | 状态 | 完成日期 | 产物 |
|---:|---|---|---|---|
| M4-1 | 异常归档数据模型与判定流程 | PENDING |  |  |
| M4-2 | 连续两轮确认逻辑 | PENDING |  |  |
| M4-3 | 栏目迁移识别 | PENDING |  |  |
| M4-4 | 重复数据指纹与分类 | PENDING |  |  |
| M4-5 | 文档身份指纹 | PENDING |  |  |
| M4-6 | 市级/区级双渠道匹配与差异对账 | PENDING |  |  |
| M4-7 | M4 自验报告 | PENDING |  | docs/milestones/M4/self_acceptance_report.md |

## M5 阅办联动、政策解读与整改闭环

| 编号 | 任务 | 状态 | 完成日期 | 产物 |
|---:|---|---|---|---|
| M5-1 | 阅办联动深度检查 | PENDING |  |  |
| M5-2 | 图片/视频/附件可用性检查 | PENDING |  |  |
| M5-3 | 多类型政策解读检查 | PENDING |  |  |
| M5-4 | 解读对象匹配 | PENDING |  |  |
| M5-5 | 整改任务创建/下发/反馈 | PENDING |  |  |
| M5-6 | 复查与销号 | PENDING |  |  |
| M5-7 | 超期提醒与复发识别 | PENDING |  |  |
| M5-8 | M5 自验报告 | PENDING |  | docs/milestones/M5/self_acceptance_report.md |

## M6 最终 UI、口径、性能与演练

| 编号 | 任务 | 状态 | 完成日期 | 产物 |
|---:|---|---|---|---|
| M6-1 | 21 项导航最终 UI | PENDING |  |  |
| M6-2 | API/UI/Excel/报表口径一致 | PENDING |  |  |
| M6-3 | 性能优化 | PENDING |  |  |
| M6-4 | OCR 队列 | PENDING |  |  |
| M6-5 | 数据库锁治理 | PENDING |  |  |
| M6-6 | 日志与监控 | PENDING |  |  |
| M6-7 | 备份/恢复/回滚演练 | PENDING |  |  |
| M6-8 | 生产副本全流程演练 | PENDING |  |  |
| M6-9 | M6 自验报告 | PENDING |  | docs/milestones/M6/self_acceptance_report.md |

## M7 全系统回归与发布候选

| 编号 | 任务 | 状态 | 完成日期 | 产物 |
|---:|---|---|---|---|
| M7-1 | 全系统回归 | PENDING |  |  |
| M7-2 | 最终 1,500 条评测样本 | PENDING |  |  |
| M7-3 | 最终安全报告 | PENDING |  | docs/final_candidate/04_final_security_report.md |
| M7-4 | 最终数据质量报告 | PENDING |  | docs/final_candidate/05_final_data_quality_report.md |
| M7-5 | 发布候选包 | PENDING |  |  |
| M7-6 | main 合并方案 | PENDING |  | docs/final_candidate/01_final_architecture.md |
| M7-7 | tag 候选 | PENDING |  |  |
| M7-8 | 最终迁移 | PENDING |  | docs/final_candidate/07_final_migration_report.md |
| M7-9 | 生产 runbook | PENDING |  | docs/final_candidate/08_production_cutover_runbook.md |
| M7-10 | 冒烟脚本 | PENDING |  |  |
| M7-11 | 观察清单 | PENDING |  | docs/final_candidate/08_production_cutover_runbook.md |
| M7-12 | M7 自验报告 | PENDING |  | docs/milestones/M7/self_acceptance_report.md |

## 最终交付物

| 编号 | 文档 | 状态 |
|---:|---|---|
| F-1 | docs/final_candidate/01_final_architecture.md | PENDING |
| F-2 | docs/final_candidate/02_final_feature_matrix.md | PENDING |
| F-3 | docs/final_candidate/03_final_rule_catalog.md | PENDING |
| F-4 | docs/final_candidate/04_final_security_report.md | PENDING |
| F-5 | docs/final_candidate/05_final_data_quality_report.md | PENDING |
| F-6 | docs/final_candidate/06_final_test_report.md | PENDING |
| F-7 | docs/final_candidate/07_final_migration_report.md | PENDING |
| F-8 | docs/final_candidate/08_production_cutover_runbook.md | PENDING |
| F-9 | docs/final_candidate/09_rollback_runbook.md | PENDING |
| F-10 | docs/final_candidate/10_independent_acceptance_guide.md | PENDING |
| F-11 | tmp/program_control/final_development_summary.json | PENDING |

