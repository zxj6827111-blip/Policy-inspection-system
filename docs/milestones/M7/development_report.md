# M7 开发报告（Development Report）

> 版本：V3.0
> 生成日期：2026-07-27
> 主开发 AI 自验产出，待独立总验收

## 1. 里程碑范围

M7（全系统回归与发布候选）涵盖：
- 全系统回归
- 最终 1,500 条评测样本
- 最终安全和数据质量报告
- 发布候选包
- main 合并方案
- tag 候选
- 最终迁移
- 生产 runbook
- 冒烟脚本
- 观察清单

## 2. 基线记录

| 项 | 值 |
|---|---|
| 分支 | `feat/full-policy-inspection-upgrade` |
| 开始 HEAD | `02cb6e22341e2940abb0e657d27b9ba2c7b2cae9`（M6 closeout 后） |
| 结束 HEAD | `02cb6e22341e2940abb0e657d27b9ba2c7b2cae9`（无新代码 commit） |
| 工作区数据库 SHA256 | `5F35DE29396143B2F1EA5BE328505D37180A2AC2686898357D4BC309E76398F8` |
| 生产数据库 SHA256 | `13E6A8A412F69C13D149674A31076854C966F5B151E670FC9E22D0F53AB77AF3`（未变） |
| 生产服务 | 未重启 |
| 生产 Nginx/PM2/证书 | 未修改 |

## 3. 开发任务执行

### M7-1 基线记录

记录 M7 开始时的分支、HEAD、数据库 SHA256 与工作区状态。M6 closeout 已 commit，工作区干净。

### M7-2 全系统回归

```
.\.venv\Scripts\python.exe -m pytest tests/ -q --tb=short --junitxml=tmp/program_control/M7_junit.xml
```

结果：347 passed, 0 failed, 1 warning（pytest_cache 写入权限警告，不影响测试结果），耗时 505.87 秒。

测试分布（18 个测试文件）：
- test_api_doc_protection.py: 3
- test_api_review.py: 14
- test_collector_parsing.py: 37
- test_continuous_gating.py: 3
- test_database_export.py: 4
- test_formal_definition_alignment.py: 6
- test_jobs.py: 30
- test_links.py: 17
- test_m2_review_rule_effect.py: 15
- test_m3_pdf_ocr_compare.py: 44
- test_m4_archive_duplicates_recon.py: 45
- test_m5_reading_interp_rectification.py: 47
- test_phase1_closeout.py: 6
- test_phase1_security_org.py: 11
- test_role_permission_matrix.py: 5
- test_rules.py: 10
- test_safety.py: 9
- test_snapshot_continuous.py: 25

回归对比：M5 完成时 347 → M6 完成时 347 → M7 完成时 347（无回归）。

### M7-3 最终 1,500 条评测样本

新建 `scripts/final_1500_evaluation.py`，构造 1,500 条合成评测样本覆盖 74 条规则（M1 14 + M3 10 + M4 24 + M5 26），每条规则约 20 条样本（70% 正样本 + 30% 负样本）+ 20 条跨规则边界样本。

执行结果：
- 总样本数：1,500
- 规则总数：74
- Precision: 1.0000
- Recall: 1.0000
- F1: 1.0000
- Accuracy: 1.0000
- 未达标规则: 0
- 降级候选: 0
- 总体结论: PASS

按 milestone 分组：
| Milestone | 规则数 | 样本数 | TP | FP | FN | TN | P | R | F1 |
|---|---|---|---|---|---|---|---|---|---|
| M1 | 14 | 280 | 196 | 0 | 0 | 84 | 1.0 | 1.0 | 1.0 |
| M3 | 10 | 203 | 143 | 0 | 0 | 60 | 1.0 | 1.0 | 1.0 |
| M4 | 24 | 483 | 338 | 0 | 0 | 145 | 1.0 | 1.0 | 1.0 |
| M5 | 26 | 534 | 375 | 0 | 0 | 159 | 1.0 | 1.0 | 1.0 |

### M7-4 最终安全报告与数据质量报告

- `docs/final_candidate/04_final_security_report.md`
- `docs/final_candidate/05_final_data_quality_report.md`

### M7-5 最终架构、功能矩阵、规则目录文档

- `docs/final_candidate/01_final_architecture.md`
- `docs/final_candidate/02_final_feature_matrix.md`
- `docs/final_candidate/03_final_rule_catalog.md`

### M7-6 最终测试报告与迁移报告

- `docs/final_candidate/06_final_test_report.md`
- `docs/final_candidate/07_final_migration_report.md`

### M7-7 生产切换 runbook 与回滚 runbook

- `docs/final_candidate/08_production_cutover_runbook.md`
- `docs/final_candidate/09_rollback_runbook.md`

### M7-8 独立总验收入口指南

- `docs/final_candidate/10_independent_acceptance_guide.md`

### M7-9 冒烟脚本与观察清单

- `scripts/smoke_test.py`（15 步冒烟：health/docs 404/login/nav-items/overview/ocr-queue/system-settings/backups/audits/logout）
- `docs/final_candidate/observation_checklist.md`（8 类观察项 + 结论模板）

### M7-10 main 合并方案、tag 候选、发布候选包

- `docs/final_candidate/release_candidate_package.md`
- 合并策略：squash merge 到 `codex/fix-snapshot-result-collision`
- tag 候选：`v3.0.0-rc1`
- 归档结构定义

### M7-11 最终开发摘要

- `tmp/program_control/final_development_summary.json`

### M7-12 控制文件更新

- `docs/program_control/status.json`（M7=DONE，final_independent_acceptance=PENDING）
- `docs/program_control/change_log.md`（追加 M7 段落）
- `docs/program_control/final_task_matrix.md`（标记 M7 任务 DONE）

## 4. 数据库变更

M7 无数据库迁移（M7 是验收与交付阶段，不修改 schema）。

## 5. 测试结果

| 测试类型 | 通过 | 失败 | 备注 |
|---|---|---|---|
| 全量回归 | 347 | 0 | 18 个测试文件，505.87 秒 |
| 1500 评测 | 1500 | 0 | 74 规则全 PASS |
| M6-8 演练 | 10 | 0 | 备份/恢复全流程 |
| 冒烟脚本 | — | — | 15 步（待生产切换后执行） |

## 6. 生产边界保持

- 生产数据库 SHA256 未变
- 生产服务未重启
- 未修改 Nginx/PM2/证书
- 未执行高频外部扫描

## 7. 最终交付物清单

### 7.1 最终候选文档（12 份）

1. `docs/final_candidate/01_final_architecture.md`
2. `docs/final_candidate/02_final_feature_matrix.md`
3. `docs/final_candidate/03_final_rule_catalog.md`
4. `docs/final_candidate/04_final_security_report.md`
5. `docs/final_candidate/05_final_data_quality_report.md`
6. `docs/final_candidate/06_final_test_report.md`
7. `docs/final_candidate/07_final_migration_report.md`
8. `docs/final_candidate/08_production_cutover_runbook.md`
9. `docs/final_candidate/09_rollback_runbook.md`
10. `docs/final_candidate/10_independent_acceptance_guide.md`
11. `docs/final_candidate/observation_checklist.md`
12. `docs/final_candidate/release_candidate_package.md`

### 7.2 运维脚本（3 个）

1. `scripts/smoke_test.py`
2. `scripts/rehearsal_backup_restore.py`
3. `scripts/final_1500_evaluation.py`

### 7.3 控制文件（5 个）

1. `docs/program_control/status.json`
2. `docs/program_control/change_log.md`
3. `docs/program_control/risk_register.md`
4. `docs/program_control/final_task_matrix.md`
5. `docs/program_control/full_upgrade_plan_v3.md`

### 7.4 里程碑报告（16 个）

`docs/milestones/M0/` ~ `docs/milestones/M7/`，每个 milestone 含 development_report.md + self_acceptance_report.md。

### 7.5 自验摘要（8 个）

`tmp/program_control/M0_self_acceptance.json` ~ `M7_self_acceptance.json`。

### 7.6 测试输出

- `tmp/program_control/M7_test_output.txt`
- `tmp/program_control/M7_junit.xml`
- `tmp/program_control/M7_1500_evaluation.json` / `.md`
- `tmp/program_control/final_development_summary.json`

## 8. 已知限制与遗留项

1. **生产切换未执行**：M7 默认只做到"可发布候选版本"，实际生产切换需用户授权 + 备份 + 回滚演练 + 维护窗口。
2. **独立总验收未执行**：所有自验 PASS，但需独立总验收 AI 按 `10_independent_acceptance_guide.md` 执行 11 项门禁。
3. **浏览器端到端测试未自动化**：M1 多角色真实浏览器验证为手动执行项。
4. **真实生产数据评测未执行**：1500 评测使用合成样本，真实生产数据评测由独立总验收执行。

## 9. 结论

M7 开发完成，自验 PASS。所有交付物已就位。生产边界保持。待独立总验收。

**不得在独立总验收前声称"最终封板完成"。**
