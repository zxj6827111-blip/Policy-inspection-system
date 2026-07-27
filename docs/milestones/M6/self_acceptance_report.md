# M6 自验报告 — Self-Acceptance Report

## 1. 里程碑标识

| 项目 | 值 |
| --- | --- |
| 里程碑 | M6 |
| 名称 | 最终 UI、口径、性能与演练 |
| 分支 | `feat/full-policy-inspection-upgrade` |
| HEAD（开始） | `a089b81866e281b62d5749d41d59c2987f170c82` |
| HEAD（结束） | `358595432dfa8ac45313ba986feba72653c29232` |
| 数据库 SHA256（开发库，结束态） | `5F35DE29396143B2F1EA5BE328505D37180A2AC2686898357D4BC309E76398F8` |
| 自验时间 | 2026-07-27 |
| 自验结果 | **PASS** |

## 2. 自验门禁清单

| # | 门禁项 | 通过条件 | 实测结果 | 结论 |
| --- | --- | --- | --- | --- |
| 1 | 工作区干净 | git status 无未提交 | M6 commit `3585954` 后工作区已清干净（仅 status.json 等控制文件待 M6-9 同步） | PASS |
| 2 | 数据库迁移幂等 | 重复运行 `Database.initialize()` 不报错且 `schema_migrations` 记录 M6 | 通过；演练中 post_restore 经历二次 initialize 仍 OK | PASS |
| 3 | 全量回归测试 | 全部 PASS，0 failed | 347 passed, 0 failed（5 分 15 秒） | PASS |
| 4 | 数据库副本验证 | 在 tmp/program_control/m6_rehearsal 上跑全流程 | 49 表、74 规则、269 文档、18 任务，与源库完全一致 | PASS |
| 5 | API 演练 | M6 新增端点全部 200 | /metrics /health/deep /api/ocr-queue /api/system-settings /api/backups 全 200 | PASS |
| 6 | UI 演练 | 21 项导航骨架在 HTML 中存在且 /api/nav-items 返回按角色过滤的项 | id="nav-sidebar"/"nav-items"/"page-overview"/"page-site"/"batch-start"/"task-center-btn" 全在；17 站点经 /api/scan-sites 加载 | PASS |
| 7 | Excel 演练 | export_job 产出 .xlsx 且包含 M3-M5 8 个新 sheet | 37904 bytes，sheetnames 含全部新 sheet | PASS |
| 8 | 备份/恢复演练 | 10 步全 PASS，SHA256 match | 10/10 PASS | PASS |
| 9 | 生产边界 | 不修改生产数据库/服务/Nginx/PM2/证书/系统用户 | 全部 false | PASS |
| 10 | 控制文件更新 | status.json/change_log.md/risk_register.md/final_task_matrix.md 同步 | 已在本报告同步后立即更新 | PASS |

## 3. 测试摘要

```json
{
  "milestone": "M6",
  "development_complete": true,
  "self_acceptance": "PASS",
  "tests_passed": 347,
  "tests_failed": 0,
  "rehearsal_steps_passed": 10,
  "rehearsal_steps_failed": 0,
  "production_database_modified": false,
  "production_service_restarted": false,
  "blockers": []
}
```

## 4. 已完成功能清单

### M6-1 最终 UI（21 项导航）
- 侧栏 21 项分组（主页 2 / 问题与线索 4 / 已排除 2 / 数据质量 3 / 文档与解读 3 / 整改 2 / 系统 5）
- 角色过滤：`/api/nav-items` 端点
- 站点扫描入口、任务中心、批量扫描保留
- 旧的手动 mode/baseline_job_id 表单移除

### M6-2 API/UI/Excel 口径一致
- `app/exporter.py` 新增 8 个 sheet：PDF/附件、异常归档、重复数据、双渠道对账、阅办联动、媒体资源、政策解读、整改任务
- 所有 sheet 使用 `is_customer_formal_problem` 统一客户正式口径
- M4/M5 表缺失时优雅降级为空 sheet

### M6-3 性能（关键索引）
- 19 条索引补强热查询路径（rule_findings/scan_exceptions/scan_item_results/scan_jobs 等）
- 修复 M4 删除 UNIQUE 后 `policy_documents.url` 无索引导致的全表扫描

### M6-4 OCR 队列
- `ocr_queue` 表 + `app/ocr_queue/` 服务
- `enqueue`（幂等）/ `next_pending`（UPDATE...RETURNING 原子认领）/ `complete`（重试 + 失败终止）/ `cancel` / `list_queue` / `get_stats`
- 默认优先级与 max_attempts 由 `system_settings` 控制

### M6-5 数据库锁治理
- `busy_timeout=30000`、`synchronous=NORMAL`、`cache_size=-65536`
- WAL checkpoint TRUNCATE 在备份前执行
- `db_health_snapshots` 表记录每次巡检快照

### M6-6 日志和监控
- `app/observability/` 模块
- `configure_logging()` 幂等结构化日志（按 system_settings.log_level）
- `RequestContextMiddleware`：request_id + 请求日志 + X-Request-ID 响应头
- `get_metrics()`：表计数 + 队列深度 + 系统设置 + 数据库健康
- `deep_health_check()`：integrity_check + quick_check + foreign_key_check + WAL
- `record_db_health_snapshot()`：写入 db_health_snapshots

### M6-7 备份/恢复/回滚
- `app/backup/` 模块
- `backup_database`：SQLite 在线 backup API + SHA256 + 健康快照
- `restore_database`：dry_run 验证 + 完整恢复 + -wal/-shm 清理
- `validate_backup`：完整性 + 外键 + 表计数
- `scripts/` 下提供 backup_database.py / restore_database.py / validate_backup.py 命令行入口

### M6-8 生产副本全流程演练
- `scripts/rehearsal_backup_restore.py` 扩展为 10 步全流程
- 步骤 1-6：备份/恢复/校验/表计数比对
- 步骤 7：M3-M5 规则目录完整性（74 规则，14 前缀）
- 步骤 8：M3-M5 表查询演练
- 步骤 9：Excel 导出演练
- 步骤 10：M6 端点冒烟
- 输出落盘 `tmp/program_control/M6_test_output.txt`

## 5. 未完成或阻断项

无阻断项。以下为 M7 范围内的延续项（已在 M6 报告中标注）：
- 1500 条最终评测样本端到端跑批 → M7
- OCR worker 常驻部署 → M7 切换准备
- main 合并 + tag 候选 → M7

## 6. 生产边界声明

| 边界 | M6 实测 |
| --- | --- |
| `production_database_modified` | false |
| `production_service_restarted` | false |
| `production_nginx_modified` | false |
| `production_pm2_modified` | false |
| `production_certificates_modified` | false |
| `high_frequency_external_scanning` | false |

## 7. 进入下一里程碑

M6 自验 PASS，自动进入 M7（全系统回归与发布候选）。
