# M7 自验报告（Self-Acceptance Report）

> 版本：V3.0
> 生成日期：2026-07-27
> 主开发 AI 自验产出，待独立总验收

## 1. 自验摘要

```json
{
  "milestone": "M7",
  "development_complete": true,
  "self_acceptance": "PASS",
  "tests_passed": 347,
  "tests_failed": 0,
  "full_regression_passed": 347,
  "evaluation_1500": {
    "total_samples": 1500,
    "total_rules": 74,
    "precision": 1.0,
    "recall": 1.0,
    "f1": 1.0,
    "accuracy": 1.0,
    "failed_rules": 0,
    "degradation_candidates": 0,
    "result": "PASS"
  },
  "rehearsal_steps_passed": 10,
  "rehearsal_total": 10,
  "production_database_modified": false,
  "production_service_restarted": false,
  "production_nginx_modified": false,
  "production_pm2_modified": false,
  "production_certificates_modified": false,
  "high_frequency_external_scanning": false,
  "production_db_sha256_before": "13E6A8A412F69C13D149674A31076854C966F5B151E670FC9E22D0F53AB77AF3",
  "production_db_sha256_after": "13E6A8A412F69C13D149674A31076854C966F5B151E670FC9E22D0F53AB77AF3",
  "production_db_sha256_unchanged": true,
  "blockers": []
}
```

## 2. 门禁清单

| # | 门禁项 | 期望 | 实际 | 结果 |
|---|---|---|---|---|
| 1 | 全系统回归 | 347 passed / 0 failed | 347 passed / 0 failed | PASS |
| 2 | 1500 条评测 | P/R/F1 >= 0.90 | P=R=F1=1.0000 | PASS |
| 3 | 未达标规则 | 0 | 0 | PASS |
| 4 | 降级候选 | 0 | 0 | PASS |
| 5 | M6-8 演练 | 10/10 PASS | 10/10 PASS | PASS |
| 6 | 最终交付文档 | 10 份 + 2 份辅助 | 10 + observation_checklist + release_candidate_package | PASS |
| 7 | 运维脚本 | 3 个 | smoke_test.py + rehearsal_backup_restore.py + final_1500_evaluation.py | PASS |
| 8 | 控制文件更新 | status.json + change_log.md + final_task_matrix.md | 已更新 | PASS |
| 9 | final_development_summary.json | 存在 | tmp/program_control/final_development_summary.json | PASS |
| 10 | 生产数据库 SHA256 | 未变 | 13E6A8A4...（与基线一致） | PASS |
| 11 | 生产服务 | 未重启 | false | PASS |
| 12 | Nginx/PM2/证书 | 未修改 | false | PASS |
| 13 | 高频外部扫描 | 未执行 | false | PASS |
| 14 | 工作区数据库 SHA256 | 记录 | 5F35DE29...（与 M6 一致） | PASS |
| 15 | 阻断项 | 0 | 0 | PASS |

## 3. 测试结果详情

### 3.1 全量回归

```
.\.venv\Scripts\python.exe -m pytest tests/ -q --tb=short --junitxml=tmp/program_control/M7_junit.xml
```

输出：
```
347 passed, 1 warning in 505.87s (0:08:25)
```

测试覆盖：
- 鉴权与 RBAC（5 角色 × 能力矩阵）
- CSRF 三段校验
- M1 元数据/文号/日期/机构/机关代字
- M2 批量复核/双复核/仲裁/规则效果
- M3 PDF/OCR/三方比对/附件
- M4 异常归档/重复检测/双渠道对账
- M5 阅办联动/媒体/解读/整改
- M6 性能索引/OCR 队列/可观测性/备份恢复

### 3.2 1500 条评测

```
.\.venv\Scripts\python.exe scripts\final_1500_evaluation.py
```

输出：
```
=== M7-3 评测完成 ===
样本: 1500, 规则: 74
Precision=1.0000 Recall=1.0000 F1=1.0000 Accuracy=1.0000
未达标规则: 0
降级候选: 0
总体结论: PASS
```

### 3.3 M6-8 演练

M7 沿用 M6 的演练结果（10/10 PASS），未重新执行（演练脚本不依赖 M7 新增内容）。

## 4. 生产边界保持证据

| 项 | 基线值 | 当前值 | 一致 |
|---|---|---|---|
| 生产数据库 SHA256 | 13E6A8A412F69C13D149674A31076854C966F5B151E670FC9E22D0F53AB77AF3 | 13E6A8A412F69C13D149674A31076854C966F5B151E670FC9E22D0F53AB77AF3 | YES |
| 工作区数据库 SHA256 | 5F35DE29396143B2F1EA5BE328505D37180A2AC2686898357D4BC309E76398F8 | 5F35DE29396143B2F1EA5BE328505D37180A2AC2686898357D4BC309E76398F8 | YES |
| 生产服务 | 运行中 | 运行中（未重启） | YES |
| Nginx | 未修改 | 未修改 | YES |
| PM2 | 未修改 | 未修改 | YES |
| 证书 | 未修改 | 未修改 | YES |

## 5. 最终交付物核验

### 5.1 最终候选文档（12 份）

| # | 文件 | 存在 |
|---|---|---|
| 1 | docs/final_candidate/01_final_architecture.md | YES |
| 2 | docs/final_candidate/02_final_feature_matrix.md | YES |
| 3 | docs/final_candidate/03_final_rule_catalog.md | YES |
| 4 | docs/final_candidate/04_final_security_report.md | YES |
| 5 | docs/final_candidate/05_final_data_quality_report.md | YES |
| 6 | docs/final_candidate/06_final_test_report.md | YES |
| 7 | docs/final_candidate/07_final_migration_report.md | YES |
| 8 | docs/final_candidate/08_production_cutover_runbook.md | YES |
| 9 | docs/final_candidate/09_rollback_runbook.md | YES |
| 10 | docs/final_candidate/10_independent_acceptance_guide.md | YES |
| 11 | docs/final_candidate/observation_checklist.md | YES |
| 12 | docs/final_candidate/release_candidate_package.md | YES |

### 5.2 运维脚本（3 个）

| # | 文件 | 存在 |
|---|---|---|
| 1 | scripts/smoke_test.py | YES |
| 2 | scripts/rehearsal_backup_restore.py | YES |
| 3 | scripts/final_1500_evaluation.py | YES |

### 5.3 自验摘要（8 个）

M0 ~ M7 的 self_acceptance.json 全部存在。

### 5.4 测试输出

- tmp/program_control/M7_test_output.txt: YES
- tmp/program_control/M7_junit.xml: YES
- tmp/program_control/M7_1500_evaluation.json: YES
- tmp/program_control/M7_1500_evaluation.md: YES
- tmp/program_control/final_development_summary.json: YES

## 6. 自验结论

**M7 自验 PASS。**

所有门禁通过：
- 347 测试通过 / 0 失败
- 1500 评测 P/R/F1 = 1.0000
- 演练 10/10 PASS
- 12 份最终交付文档 + 3 个脚本就位
- 生产边界保持
- 0 阻断项

**M0-M7 全部自验 PASS。**

## 7. 后续行动

1. **独立总验收**：由独立总验收 AI 按 `docs/final_candidate/10_independent_acceptance_guide.md` 执行 11 项门禁。
2. **生产切换**：独立总验收 PASS 后，由用户授权 + 备份 + 回滚演练 + 维护窗口确认后执行。
3. **main 合并**：按 `docs/final_candidate/release_candidate_package.md` 中的 squash merge 方案执行。
4. **tag 创建**：合并后打 `v3.0.0-rc1` tag。

**不得在独立总验收前声称"最终封板完成"。**
