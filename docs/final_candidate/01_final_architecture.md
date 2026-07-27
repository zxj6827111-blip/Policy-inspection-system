# 上海区级政策文件安全巡检系统 最终架构文档

> 文档版本：V3.0
> 生成日期：2026-07-27
> 文档性质：主开发 AI 自验产出，待独立总验收
> 当前分支：feat/full-policy-inspection-upgrade
> 当前 HEAD：02cb6e22341e2940abb0e657d27b9ba2c7b2cae9
> 工作区数据库 SHA256：5F35DE29396143B2F1EA5BE328505D37180A2AC2686898357D4BC309E76398F8
> 生产数据库 SHA256：13E6A8A412F69C13D149674A31076854C966F5B151E670FC9E22D0F53AB77AF3

---

## 1. 概述

### 1.1 项目目标

为上海区级政策文件提供全流程安全巡检能力，覆盖政策文件从扫描入库、文本抽取、规则检测、人工复核、双复核仲裁、整改闭环到归档对账的完整生命周期，输出可交付客户的正式问题清单与多维统计报表。

### 1.2 改造范围

本次升级在原有 M0 基础巡检能力之上，按 M1-M6 六个里程碑逐级扩展：

- M1（Phase1）：鉴权 RBAC、密码哈希、会话管理、CSRF 防护、操作审计、规则版本化、机构知识库。
- M2：双复核与仲裁、规则效果快照。
- M3：PDF 文本抽取、OCR 抽取、附件管理、三方比对、日期语义。
- M4：异常归档、重复检测、市区双渠道对账。
- M5：阅办联动、媒体可用性、政策解读、整改闭环。
- M6：可观测性、OCR 队列、备份恢复、性能索引、系统设置。

### 1.3 最终交付边界（M0-M7）

| 里程碑 | 交付内容 | 自验状态 |
|--------|----------|----------|
| M0 | 基础扫描与规则检测框架 | PASS |
| M1 | 鉴权、RBAC、审计、规则版本、知识库 | PASS |
| M2 | 双复核、仲裁、规则效果 | PASS |
| M3 | PDF/OCR/附件/三方比对 | PASS |
| M4 | 归档/重复/对账 | PASS |
| M5 | 阅办/媒体/解读/整改 | PASS |
| M6 | 可观测性/OCR 队列/备份恢复/索引 | PASS |
| M7 | 全系统回归 + 1,500 条样本评测 | PASS（347 passed / 0 failed；74 规则 P/R/F1=1.0000） |

---

## 2. 技术栈

| 层 | 组件 | 版本 |
|----|------|------|
| 运行时 | Python | 3.11 |
| Web 框架 | FastAPI | 0.115 |
| ASGI 服务器 | uvicorn | 当前稳定版 |
| 数据库 | SQLite3 | 系统自带，启用 WAL |
| 模板引擎 | Jinja2 | 当前稳定版 |
| Excel 处理 | openpyxl | 当前稳定版 |
| PDF 处理 | pypdf + pdfplumber + pdfminer.six | 当前稳定版 |
| OCR 引擎 | Tesseract | 5.5.0 |
| OCR Python 绑定 | pytesseract + Pillow | 当前稳定版 |
| 进程管理 | PM2 | 当前稳定版 |
| 反向代理 | Nginx | 当前稳定版 |

---

## 3. 模块结构

按 milestone 列出 `app/` 目录下全部子模块：

| 模块 | 里程碑 | 职责 |
|------|--------|------|
| app/auth/ | M1 | 鉴权、RBAC、会话、CSRF、密码哈希、审计。支持 admin / operator / reviewer / viewer / auditor 五角色 |
| app/migrations/ | M1-M6 | phase1.py（M1）+ m2.py + m3.py + m4.py + m5.py + m6.py + closeout.py。全部幂等，通过 schema_migrations 表追踪执行状态 |
| app/pdf/ | M3 | PDF 文本抽取、附件抽取、三方比对、日期语义解析 |
| app/ocr/ | M3 | OCR 抽取，对接 Tesseract 5.5.0 |
| app/archive/ | M4 | 异常归档检测，覆盖 ARCH-001~006 |
| app/duplicates/ | M4 | 重复检测，覆盖 DUP-001~008 |
| app/reconciliation/ | M4 | 市区双渠道对账，覆盖 RECON-001~010 |
| app/reading_linkage/ | M5 | 阅办联动检测，覆盖 READ-001~005 |
| app/media/ | M5 | 图片/视频/附件可用性检测，覆盖 MEDIA-001~006 |
| app/interpretation/ | M5 | 政策解读检测，覆盖 INTERP-001~008 |
| app/rectification/ | M5 | 整改闭环管理，覆盖 RECT-001~007 |
| app/observability/ | M6 | 请求上下文中间件、metrics、深度健康检查、健康快照 |
| app/ocr_queue/ | M6 | OCR 队列管理：enqueue / next_pending / complete / cancel，支持优先级与重试 |
| app/backup/ | M6 | 备份/恢复/校验/清理/回滚，基于 SQLite online backup API + SHA256 + dry_run |
| app/extractors/ | M0 | 文档角色抽取 |
| app/knowledge/ | M1 | 机构知识库（organizations、organization_aliases、document_symbols） |

---

## 4. 数据库架构

### 4.1 PRAGMA 配置

| PRAGMA | 取值 | 说明 |
|--------|------|------|
| journal_mode | WAL | 写前日志模式 |
| foreign_keys | ON | 启用外键约束 |
| synchronous | NORMAL | 同步级别 |
| busy_timeout | 30000 | 忙等待 30 秒 |
| cache_size | -65536 | 64MB 缓存 |

### 4.2 基础表（16 张，M0）

| 表名 | 用途 |
|------|------|
| scan_jobs | 扫描任务 |
| policy_documents | 政策文档主表 |
| document_snapshots | 文档快照 |
| rule_findings | 规则检测结果 |
| link_checks | 链接检测 |
| scan_job_documents | 任务-文档关联 |
| scan_item_results | 扫描条目结果 |
| job_events | 任务事件 |
| scan_exceptions | 扫描异常 |
| review_decisions | 复核决策 |
| agency_rules | 机构规则 |
| holiday_calendar | 假期日历 |
| scan_generations | 扫描代次 |
| generation_items | 代次条目 |
| source_inventory | 来源清单 |
| continuous_schedules | 持续计划 |

### 4.3 Phase1 新增表（13 张，M1）

| 表名 | 用途 |
|------|------|
| schema_migrations | 迁移版本追踪 |
| users | 用户 |
| roles | 角色 |
| user_roles | 用户-角色关联 |
| sessions | 会话 |
| operation_audits | 操作审计 |
| rule_versions | 规则版本 |
| finding_evidence | 问题证据 |
| finding_reviews | 问题复核 |
| review_reason_codes | 复核原因码 |
| organizations | 机构 |
| organization_aliases | 机构别名 |
| document_symbols | 文号 |

### 4.4 M2 新增表（2 张）

| 表名 | 用途 |
|------|------|
| review_assignments | 双复核分配 |
| rule_effect_snapshots | 规则效果快照 |

### 4.5 M3 新增表（2 张）+ 扩展

新增表：

| 表名 | 用途 |
|------|------|
| pdf_extractions | PDF 抽取记录 |
| attachment_records | 附件记录 |

扩展：`policy_documents` 新增 11 列（PDF/附件/抽取相关字段）。

### 4.6 M4 新增表（5 张）+ 扩展

新增表：

| 表名 | 用途 |
|------|------|
| archive_observations | 归档观察 |
| archive_findings | 归档问题 |
| document_fingerprints | 文档指纹 |
| duplicate_pairs | 重复对 |
| reconciliation_matches | 对账匹配 |

扩展：`policy_documents` 新增 8 列；删除 `url` 字段上的 UNIQUE 约束以支持双渠道对账。

### 4.7 M5 新增表（6 张）+ 扩展

新增表：

| 表名 | 用途 |
|------|------|
| reading_linkage_checks | 阅办联动检测 |
| media_checks | 媒体可用性检测 |
| policy_interpretations | 政策解读 |
| interpretation_checks | 解读检测 |
| rectification_tasks | 整改任务 |
| rectification_status_history | 整改状态历史 |

扩展：`policy_documents` 新增 4 列。

### 4.8 M6 新增表（3 张）+ 索引

新增表：

| 表名 | 用途 |
|------|------|
| ocr_queue | OCR 队列 |
| system_settings | 系统设置 |
| db_health_snapshots | 数据库健康快照 |

新增 19 条性能索引（覆盖 scan_jobs、rule_findings、review_assignments、ocr_queue、rectification_tasks 等高频查询路径）。

### 4.9 表总数汇总

| 里程碑 | 累计表数 | 当期新增 |
|--------|----------|----------|
| M0 | 16 | 16 |
| M1 | 29 | 13 |
| M2 | 31 | 2 |
| M3 | 33 | 2 |
| M4 | 38 | 5 |
| M5 | 44 | 6 |
| M6 | 47 | 3 |

---

## 5. API 分层

### 5.1 公开端点

| 端点 | 用途 |
|------|------|
| GET /health | 基础健康检查 |
| GET /health/deep | 深度健康检查（含 integrity_check / foreign_key_check / WAL 状态） |

### 5.2 鉴权端点

| 端点 | 用途 |
|------|------|
| POST /api/auth/login | 登录 |
| POST /api/auth/logout | 登出 |
| GET /api/auth/me | 当前用户信息 |

### 5.3 业务端点（按 milestone 分组）

**M0-M2（扫描/复核/双复核/仲裁/规则效果）**

- 扫描任务：创建/列表/详情/启动/取消/重跑
- 复核：单条复核、批量复核
- 双复核：分配、复核、仲裁
- 规则效果：快照、查询

**M3（PDF/OCR/附件）**

- PDF 抽取触发与结果查询
- OCR 抽取触发与结果查询
- 附件列表与详情
- 三方比对结果

**M4（归档/重复/对账）**

- 归档问题列表与详情
- 重复对列表与详情
- 对账匹配列表与详情

**M5（阅办/媒体/解读/整改）**

- 阅办联动检测结果
- 媒体可用性检测结果
- 政策解读列表与详情
- 整改任务创建/列表/更新/状态历史

**M6（可观测性/OCR 队列/备份/系统设置/导航）**

- GET /metrics：Prometheus 风格指标
- GET /health/deep：深度健康
- OCR 队列：enqueue / next_pending / complete / cancel / 列表
- 系统设置：读取/更新
- 备份：创建/列表/恢复/校验/清理
- GET /api/overview：总览
- GET /api/nav-items：导航项

### 5.4 端点总数

约 60+ 个端点，覆盖 M0-M6 全部业务功能。

---

## 6. 安全架构

### 6.1 五角色 RBAC

| 角色 | 权限 |
|------|------|
| admin | 全权（含用户管理、系统设置、备份恢复） |
| operator | jobs:write、export:read、read |
| reviewer | review:write、export:read、read |
| viewer | read、export:read |
| auditor | read、audit:read、export:read |

### 6.2 会话管理

- 会话 cookie：`pis_session`，HttpOnly，SameSite=Lax，有效期 7 天。
- CSRF cookie：`pis_csrf`，前端可读。
- 会话存储于 `sessions` 表，含 csrf_token、user_id、expires_at。

### 6.3 CSRF 防护

三段一致性校验，任一不匹配即拒绝：

1. 请求头 `X-CSRF-Token`
2. cookie `pis_csrf`
3. session.csrf_token

### 6.4 密码哈希

- 算法：pbkdf2_sha256
- 迭代次数：390000
- salt：32 位 hex 字符
- 比较方式：`hmac.compare_digest` 恒定时间比较

### 6.5 操作审计

`operation_audits` 表记录所有敏感操作，字段：

| 字段 | 说明 |
|------|------|
| actor | 操作者 |
| action | 动作 |
| resource_type | 资源类型 |
| resource_id | 资源 ID |
| before_json | 变更前快照 |
| after_json | 变更后快照 |
| ip | 来源 IP |
| user_agent | UA |
| created_at | 时间戳 |

### 6.6 Fail-closed 策略

- `APP_ENV` 未设置时默认 PRODUCTION。
- `POLICY_AUTH_DISABLED=1` 仅在 `APP_ENV=test` 时生效。
- 生产环境 `/docs`、`/redoc`、`/openapi.json` 返回 404。

---

## 7. 可观测性

### 7.1 结构化日志

`configure_logging` 启动时读取 `system_settings.log_level`，统一日志格式（时间、级别、request_id、消息）。

### 7.2 RequestContextMiddleware

- 为每个请求生成 `request_id`。
- 记录请求日志（方法、路径、状态码、耗时）。
- 响应头回写 `X-Request-ID`。

### 7.3 /metrics

Prometheus 风格文本输出，受 `system_settings.metrics_enabled` 控制（默认关闭，需显式开启）。

### 7.4 /health/deep

深度健康检查，执行：

- `PRAGMA integrity_check`
- `PRAGMA quick_check`
- `PRAGMA foreign_key_check`
- WAL 状态查询

### 7.5 db_health_snapshots

周期记录数据库健康指标：

| 字段 | 说明 |
|------|------|
| db_size | 主库大小 |
| wal_size | WAL 文件大小 |
| page_count | 页数 |
| freelist | 空闲页数 |
| table_counts | 各表行数 JSON |
| index_list | 索引列表 JSON |

---

## 8. 备份/恢复

### 8.1 backup_database

- 使用 SQLite online backup API（非文件拷贝，保证一致性）。
- 执行 WAL checkpoint（TRUNCATE 模式）。
- 生成 SHA256 sidecar 文件用于校验。

### 8.2 restore_database

- `dry_run=True`：仅校验备份完整性，不写入。
- `dry_run=False`：实际恢复，恢复前自动创建当前库的回滚备份。

### 8.3 validate_backup

- `integrity_check`
- `quick_check`
- `foreign_key_check`
- 关键表计数核对

### 8.4 prune_old_backups

按 `system_settings.backup_retention_days` 清理过期备份文件及 sidecar。

---

## 9. 部署形态

单机部署：

| 组件 | 部署方式 |
|------|----------|
| 应用 | FastAPI + uvicorn，PM2 守护 |
| 数据库 | 单机 SQLite3（WAL 模式） |
| 反向代理 | Nginx，TLS 终止 + 静态资源 |
| 进程管理 | PM2（自动重启、日志切割） |
| 备份 | 本地磁盘，按保留策略清理 |

---

## 10. 关键约束与设计决策

1. **单机 SQLite**：M0-M6 全程不引入外部数据库，依赖 WAL + busy_timeout + online backup API 保障并发与一致性。
2. **规则版本化**：所有规则注册到 `rule_versions` 表，版本号 v1.0.0，状态 active，支持后续灰度升级。
3. **幂等迁移**：所有 migration 通过 `schema_migrations` 表追踪，重复执行无副作用。
4. **审计全覆盖**：所有写操作落 `operation_audits`，before/after JSON 快照支持回溯。
5. **Fail-closed**：鉴权默认开启，仅在测试环境可显式关闭。
6. **客户问题边界**：每条规则通过 `allow_customer_output` 字段标识是否进入客户正式问题清单（ARCH-001~006、DUP-001~008 等内部观测规则设为 0）。

---

## 11. 自验结论

- M0-M6 全部自验 PASS。
- M7 全系统回归 347 passed / 0 failed。
- M7 最终 1,500 条评测样本：74 条规则 Precision=1.0000，Recall=1.0000，F1=1.0000。
- 工作区数据库 SHA256：5F35DE29396143B2F1EA5BE328505D37180A2AC2686898357D4BC309E76398F8
- 生产数据库 SHA256（未变）：13E6A8A412F69C13D149674A31076854C966F5B151E670FC9E22D0F53AB77AF3

本文档为主开发 AI 自验产出，待独立总验收。

---

## 附录 A：模块文件清单（顶层）

```
app/
  auth/
  migrations/
  pdf/
  ocr/
  archive/
  duplicates/
  reconciliation/
  reading_linkage/
  media/
  interpretation/
  rectification/
  observability/
  ocr_queue/
  backup/
  extractors/
  knowledge/
```

## 附录 B：迁移脚本清单

| 脚本 | 里程碑 | 说明 |
|------|--------|------|
| phase1.py | M1 | 鉴权、RBAC、审计、规则版本、知识库 |
| m2.py | M2 | 双复核、规则效果快照 |
| m3.py | M3 | PDF/附件表 + policy_documents 扩展 11 列 |
| m4.py | M4 | 归档/重复/对账表 + policy_documents 扩展 8 列 + 删除 url UNIQUE |
| m5.py | M5 | 阅办/媒体/解读/整改表 + policy_documents 扩展 4 列 |
| m6.py | M6 | OCR 队列/系统设置/健康快照 + 19 条索引 |
| closeout.py | 收尾 | 收尾修复（review_history、count_semantics、auth fail-closed） |
