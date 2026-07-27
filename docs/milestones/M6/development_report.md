# M6 开发报告 — 最终 UI、口径、性能与演练

## 1. 里程碑范围

M6 的目标是把 M0~M5 的能力整合到一个最终可交付的运行时形态：
- 21 项最终 UI 导航（按角色过滤）
- API/UI/Excel/报表口径一致（补齐 M3~M5 的 Excel 输出）
- 数据库性能与锁治理（关键索引、PRAGMA、WAL）
- OCR 队列（串行/并行、优先级、重试）
- 日志与监控（结构化日志、metrics、深度健康）
- 备份/恢复/回滚（SQLite 在线 backup、SHA256 校验、dry_run）
- 生产副本全流程演练

## 2. 起止基线

| 项目 | 值 |
| --- | --- |
| 分支 | `feat/full-policy-inspection-upgrade` |
| HEAD（开始） | `a089b81866e281b62d5749d41d59c2987f170c82` (M5 final) |
| HEAD（结束） | `358595432dfa8ac45313ba986feba72653c29232` (M6 final) |
| 工作区数据库 SHA256（开发库） | `5F35DE29396143B2F1EA5BE328505D37180A2AC2686898357D4BC309E76398F8` |
| 生产数据库 SHA256 | 未触碰，沿用 M5 baseline |

## 3. 数据库迁移（app/migrations/m6.py）

### 3.1 关键索引（19 条）

补强 M0~M5 阶段所有热查询路径：
- `policy_documents.url`（M4 删除 UNIQUE 后无索引导致的 save_record 全表扫描修复）
- `rule_findings(document_id, rule_code, is_current)` / `(job_id, human_status, machine_status, is_current)` / `(document_id, is_current)`
- `scan_exceptions(job_id, status)`
- `scan_item_results(job_id, url)`
- `scan_jobs(status, id)` / `(mode, status, coverage_status)`
- `generation_items(job_id, id)`
- `link_checks(job_id, document_id, visible)`
- `source_inventory(target_key, is_present)`
- `continuous_schedules(enabled, next_incremental_at, next_full_reconcile_at)`
- `rectification_tasks(status, deadline)` + 部分索引 `WHERE recurrence_count > 0`
- `archive_findings(machine_status, human_status, status)`
- `reconciliation_matches(match_status)`
- `operation_audits(actor_username)` / `(action, created_at)`

### 3.2 新增表

- `ocr_queue`：OCR 处理队列（status: pending/running/ok/failed/skipped/cancelled，priority, attempts, max_attempts, worker_id, ocr_confidence, pages_processed/total）
- `system_settings`：系统配置键值对（key/value/category/description/updated_at/updated_by）
- `db_health_snapshots`：数据库健康快照（snapshot_at, db_size_bytes, wal_size_bytes, page_count, freelist_count, journal_mode, table_counts_json, index_list_json）

### 3.3 迁移特性

- 所有 DDL 使用 `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`，幂等
- 在 `_migrate_m6` 中通过 `schema_migrations` 表记录已应用版本，避免重复执行
- 失败 fail-closed：任何 SQL 错误立即抛出，不静默吞掉

## 4. db.py PRAGMA 调优

`Database.connect()` 显式设置：
```python
conn.execute("PRAGMA foreign_keys=ON")
conn.execute("PRAGMA busy_timeout=30000")      # 30s，与 connect timeout 一致
conn.execute("PRAGMA synchronous=NORMAL")       # WAL 下安全且更快
conn.execute("PRAGMA cache_size=-65536")         # 64MB cache
```

## 5. 可观测性模块（app/observability/）

### 5.1 结构化日志
- `configure_logging()`：幂等，读取 `system_settings.log_level`，默认 INFO；降低 urllib3/openpyxl 噪音
- `get_request_logger(request)`：附加 `request_id` 字段

### 5.2 指标（get_metrics）
- 表计数：rule_findings/policy_documents/scan_jobs/ocr_queue/rectification_tasks/operation_audits
- 队列深度：ocr_queue pending/running/failed
- 系统设置摘要
- 数据库健康（page_count/freelist_count/journal_mode/integrity_check）

### 5.3 深度健康检查（deep_health_check）
- 数据库完整性 `PRAGMA integrity_check` + `quick_check`
- 外键完整性 `PRAGMA foreign_key_check`
- WAL 大小与 journal_mode
- 关键表计数与异常检测

### 5.4 请求上下文中间件（RequestContextMiddleware）
- 为每个请求生成 `request_id`（UUID 前 8 位）
- 记录 `request_start` / `request_end` 日志（method/path/status_code/duration_ms）
- 在响应头注入 `X-Request-ID`

### 5.5 数据库健康快照（record_db_health_snapshot）
- 写入 `db_health_snapshots` 表
- 用于备份前后、定期巡检

## 6. OCR 队列模块（app/ocr_queue/）

- `enqueue()`：幂等入队，已存在 pending/running 项则返回现有 ID
- `next_pending()`：使用 `UPDATE ... RETURNING *` 原子认领（最高优先级 + 最早请求）
- `complete()`：根据 ocr_status 决定终态（ok/skipped/not_needed → ok；failed → 重试或失败终止）
- `cancel()` / `list_queue()` / `get_stats()`

## 7. 备份/恢复模块（app/backup/）

### 7.1 backup_database
- 使用 SQLite `Connection.backup()` 在线 API（不阻塞写入）
- 源库 WAL checkpoint TRUNCATE 前先快照
- 备份库再次 WAL checkpoint（确保 -wal/-shm 合并）
- 写 `.db.sha256` 旁路文件
- 调用 `record_db_health_snapshot` 记录源库 + 备份库快照

### 7.2 restore_database
- 校验备份完整性 `PRAGMA integrity_check`
- 校验 SHA256
- 支持 `dry_run=True` 仅校验不写入
- 替换目标库前清理 -wal/-shm 残留
- 使用 `Connection.backup()` 写回目标库

### 7.3 validate_backup
- 完整性 + 外键 + 关键表计数

## 8. 主程序端点（app/main.py）

新增 M6 端点（全部受 `system_settings` 开关与角色保护）：

| 端点 | 方法 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| `/metrics` | GET | 登录 | Prometheus 风格指标；Accept: text/plain 返回文本 |
| `/health/deep` | GET | 公开 | 深度健康（readiness/liveness 探针）；unhealthy 返回 503 |
| `/api/ocr-queue` | GET | 登录 | 列出队列项（支持 status 过滤） |
| `/api/ocr-queue/enqueue` | POST | jobs:write | 入队 |
| `/api/ocr-queue/{item_id}/complete` | POST | jobs:write | 完成 |
| `/api/ocr-queue/{item_id}/cancel` | POST | jobs:write | 取消 |
| `/api/system-settings` | GET | 登录 | 列出所有配置 |
| `/api/system-settings/{key}` | PUT | admin | 更新配置 |
| `/api/backups` | GET | 登录 | 列出备份 |
| `/api/backups` | POST | jobs:write | 触发新备份 |
| `/api/backups/{filename}/restore` | POST | admin | 从备份恢复（dry_run 可选） |
| `/api/overview` | GET | 登录 | 全局聚合：最近任务 + 队列 + 配置 + 健康 |
| `/api/nav-items` | GET | 登录 | 21 项导航按角色过滤 |

## 9. 21 项导航 UI（app/templates/index.html + static）

侧栏 21 项分组（按角色过滤）：
- 主页（2）：首页总览 / 扫描任务
- 问题与线索（4）：确认问题 / 待复核线索 / 数据不足 / 采集异常
- 已排除（2）：已自动排除 / 已人工排除
- 数据质量（3）：异常归档 / 重复数据 / 双渠道对账
- 文档与解读（3）：PDF 与附件 / 政策解读 / 阅办联动
- 整改（2）：整改任务 / 销号与复发
- 系统（5）：站点扫描 / 任务中心 / 系统配置 / OCR 队列 / 备份恢复

`/api/nav-items` 端点根据当前用户角色返回可见项。

## 10. Excel 导出口径补齐（app/exporter.py）

新增 8 个 Sheet（插入在"元数据问题"与"扫描运行记录"之间）：

| # | Sheet | 来源 | 规则族 |
| --- | --- | --- | --- |
| 1 | PDF 与附件问题 | `rule_findings` category IN ('pdf_integrity','pdf_compare','attachment') | M3 PDF/ATT |
| 2 | 异常归档问题 | `archive_findings` JOIN `policy_documents` | M4 ARCH |
| 3 | 重复数据问题 | `duplicate_pairs` JOIN 两份 `policy_documents` | M4 DUP |
| 4 | 双渠道对账差异 | `reconciliation_matches` JOIN 市级/区级 `policy_documents` | M4 RECON |
| 5 | 阅办联动问题 | `reading_linkage_checks` WHERE rule_code != '' | M5 READ |
| 6 | 媒体资源问题 | `media_checks` WHERE rule_code != '' | M5 MEDIA |
| 7 | 政策解读问题 | `interpretation_checks` JOIN `policy_interpretations` | M5 INTERP |
| 8 | 整改任务跟踪 | `rectification_tasks` JOIN `policy_documents` | M5 RECT |

所有新 sheet 使用 `enrich_finding_row` + `is_customer_formal_problem` 计算"是否客户正式问题"列，与 M1/M2 客户口径严格一致。M4/M5 表缺失时优雅降级为空 sheet。

## 11. 生产副本全流程演练（scripts/rehearsal_backup_restore.py）

10 步骤全流程演练（在 tmp 副本上执行，不触碰真实 DB_PATH）：

| # | 步骤 | 结果 |
| --- | --- | --- |
| 1 | 拷贝源库到 pre_rehearsal.db | PASS |
| 2 | 对 pre_rehearsal 执行备份 | PASS |
| 3 | 校验备份（integrity/quick_check/fk_check/表计数） | PASS |
| 4 | 从备份恢复到 post_restore.db | PASS（SHA256 match） |
| 5 | 校验 post_restore | PASS |
| 6 | 比对 pre_rehearsal 与 post_restore 表行数 | PASS（49 表全一致） |
| 7 | M3-M5 规则目录完整性（74 规则，14 前缀全在） | PASS |
| 8 | M3-M5 规则表查询演练（8 张表） | PASS（行数 0，表结构 OK） |
| 9 | Excel 导出演练（job_id=18，37KB） | PASS |
| 10 | M6 端点冒烟（/metrics /health/deep /api/ocr-queue /api/system-settings /api/backups） | PASS（5/5 全部 200） |

overall: **PASS**

## 12. 测试结果

| 类别 | 数量 |
| --- | --- |
| 全量回归 | 347 passed, 0 failed |
| 用时 | 5 分 15 秒 |
| M6 端点冒烟（演练步骤 10） | 5/5 PASS |
| 演练整体 | 10/10 PASS |

## 13. 生产边界

| 边界 | 状态 |
| --- | --- |
| 生产数据库修改 | 否（仅开发库 + tmp 副本） |
| 生产服务重启 | 否 |
| 生产 Nginx/PM2 修改 | 否 |
| 生产证书/系统用户修改 | 否 |
| 高频外部扫描 | 否 |

## 14. 已知遗留

- M3-M5 规则表在开发库中均为 0 行（未在开发库上跑过 M3-M5 处理流水线）；演练步骤 8 验证表结构与查询可达性，行数 0 是预期。
- OCR worker 仍未常驻（M6 提供 API 入口和队列，但 worker 由 PM2/systemd 部署，属 M7 切换准备范围）。
- 1500 条最终评测样本的端到端跑批留待 M7 执行（M6 阶段仅保证口径、表结构、API、Excel 一致性已就绪）。

## 15. 自验结论

M6 全部门禁通过，进入 M7。
