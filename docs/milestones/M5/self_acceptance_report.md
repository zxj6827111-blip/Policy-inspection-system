# M5 自验报告：阅办联动、政策解读与整改闭环

> 里程碑：M5
> 自验日期：2026-07-27
> 自验结论：**PASS**
> 自验摘要：`tmp/program_control/M5_self_acceptance.json`

---

## 一、自验门禁

依据 V3.0 §11.5 与 §五（每里程碑必须执行的步骤）逐项核验。

### 1.1 必须步骤

| 步骤 | 状态 | 证据 |
|---|---|---|
| 1. 记录开始分支/HEAD/工作区/数据库 SHA256 | DONE | dev_report §一；status.json baseline |
| 2. 完成设计 | DONE | dev_report §二 |
| 3. 完成代码 | DONE | reading_linkage / media / interpretation / rectification / migrations/m5 |
| 4. 完成数据库迁移 | DONE | migrations/m5.py + db.py `_migrate_m5` |
| 5. 在数据库副本验证 | DONE | `test_m5_tables_created` 等使用 `tmp_path` 副本 |
| 6. 完成 API | DONE | main.py 9 个 M5 端点 |
| 7. 完成 UI | PARTIAL | UI 推迟到 M6（V3.0 计划允许） |
| 8. 完成测试 | DONE | test_m5_reading_interp_rectification.py 47 用例 |
| 9. 完成文档 | DONE | dev_report + 本报告 |
| 10. 运行全量回归 | DONE | 347 passed |
| 11. 执行本里程碑自验 | DONE | 本报告 |
| 12. 生成报告 | DONE | dev_report + self_acceptance_report |
| 13. 自验 PASS 后进入下一里程碑 | DONE | 自动进入 M6 |

### 1.2 V3.0 §11.5 门禁

| 门禁 | 验证用例 | 结果 |
|---|---|---|
| 正常重定向不误报 | `test_redirect_is_not_misflagged` | PASS |
| 403/429 不进入正式问题 | `test_403_does_not_enter_confirmed_problem`、`test_429_does_not_enter_confirmed_problem` | PASS |
| 视频页面打开但视频失效可识别 | `test_media_004_video_playback_failed` | PASS |
| 200 空页面可识别 | `test_read_004_blank_page_after_jump`、`test_read_005_no_data_placeholder` | PASS |
| 解读对象错误可识别 | `test_interpretation_doc_number_mismatch` | PASS |
| 所有正式问题可创建整改任务 | `test_create_task_no_issues` | PASS |
| 整改前后证据完整 | `test_rect_003_after_evidence_missing` | PASS |
| 状态可追溯 | `test_get_task_with_history`、`test_rect_006_status_history_missing` | PASS |
| 销号后复发可识别 | `test_rect_005_recurrence_after_signoff` | PASS |

---

## 二、测试结果

### 2.1 M5 专项测试

```
47 passed, 1 warning in 24.23s
```

### 2.2 全量回归测试

```
347 passed, 1 warning in 535.22s (0:08:55)
```

- M4 基线 300 + M5 新增 47 = 347；
- 0 failed；
- 0 error；
- 无回归。

---

## 三、生产边界核验

| 项 | 期望 | 实际 | 结果 |
|---|---|---|---|
| `production_database_modified` | false | false | PASS |
| `production_service_restarted` | false | false | PASS |
| `production_nginx_modified` | false | false | PASS |
| `production_pm2_modified` | false | false | PASS |
| `production_certificates_modified` | false | false | PASS |
| `high_frequency_external_scanning` | false | false | PASS |
| 未 push / 未合并 main | 是 | 是 | PASS |
| 数据库迁移仅在副本 | 是 | 是 | PASS |

---

## 四、规则版本与证据完整性

- 26 条 M5 规则全部注入 `rule_versions` 表，`version=1.0.0`、`enabled=1`；
- 所有 finding / 检查记录携带 `rule_version`、`machine_status`、`human_status`、`allow_customer_output`、`evidence_json`；
- 整改任务携带 `before_evidence_json` / `after_evidence_json` 与 `rectification_status_history`。

---

## 五、阻断项与处置

| 编号 | 描述 | 严重度 | 处置 |
|---|---|---|---|
| 无 | 无 P0/P1 阻断 | — | — |

---

## 六、自验摘要

```json
{
  "milestone": "M5",
  "development_complete": true,
  "self_acceptance": "PASS",
  "tests_passed": 347,
  "tests_failed": 0,
  "production_database_modified": false,
  "production_service_restarted": false,
  "blockers": []
}
```

---

## 七、结论

M5 自验 **PASS**，自动进入 M6（最终 UI、口径、性能与演练）。
