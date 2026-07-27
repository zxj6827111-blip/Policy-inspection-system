# 05 最终数据质量报告

> 版本号：V3.0
> 生成日期：2026-07-27
> 主开发 AI 自验产出，待独立总验收
> 当前分支：feat/full-policy-inspection-upgrade
> 当前 HEAD：02cb6e22341e2940abb0e657d27b9ba2c7b2cae9
> 工作区数据库 SHA256：5F35DE29396143B2F1EA5BE328505D37180A2AC2686898357D4BC309E76398F8
> 生产数据库 SHA256（未变）：13E6A8A412F69C13D149674A31076854C966F5B151E670FC9E22D0F53AB77AF3

---

## 1. 执行摘要

本报告汇总 Policy-inspection-system（上海区级政策文件安全巡检系统）在 M0-M7 阶段建立的数据质量保障机制与最终评估结果。数据质量保障贯穿数据库约束、规则效果评估、规则版本管理、证据链、复核闭环、整改闭环、健康监控、备份完整性等全链路。

最终评估结果：

| 指标 | 取值 |
| --- | --- |
| 评测样本总数 | 1,500 |
| 规则总数 | 74 |
| Precision | 1.0000 |
| Recall | 1.0000 |
| F1 | 1.0000 |
| Accuracy | 1.0000 |
| 未达标规则 | 0 |
| 降级候选 | 0 |
| M7 全系统回归 | 347 passed / 0 failed |
| M6-8 备份演练 | 10 / 10 PASS |

生产数据库 SHA256 全程未变（`13E6A8A412F69C13D149674A31076854C966F5B151E670FC9E22D0F53AB77AF3`），所有数据质量改造与演练均在工作区数据库上完成。

---

## 2. 数据完整性约束

### 2.1 外键约束

- 启用方式：`PRAGMA foreign_keys=ON`（每次连接初始化时执行）
- 覆盖范围：49 张表全部外键关系
- 校验时机：写入、更新、删除均实时校验；备份恢复后执行 `PRAGMA foreign_key_check`

### 2.2 UNIQUE 约束（示例）

| 表 | UNIQUE 约束 |
| --- | --- |
| `rule_findings` | `UNIQUE(job_id, document_id, rule_code)` |
| `archive_findings` | `UNIQUE(document_id, rule_code)` |
| `duplicate_pairs` | `UNIQUE(document_id_a, document_id_b)` + `CHECK(document_id_a < document_id_b)` |
| `rule_versions` | `UNIQUE(rule_code, version)` |
| `finding_reviews` | `UNIQUE(finding_id, reviewer_id)` |
| `review_assignments` | `UNIQUE(finding_id, assignee_id, round)` |
| `users` | `UNIQUE(username)` |
| `sessions` | `UNIQUE(session_id)` |

### 2.3 CHECK 约束（示例）

| 表 | 字段 | CHECK |
| --- | --- | --- |
| `ocr_queue` | `status` | `CHECK(status IN ('pending','processing','done','failed'))` |
| `media_checks` | `media_type` | `CHECK(media_type IN ('image','audio','video','document'))` |
| `rectification_tasks` | `status` | `CHECK(status IN ('created','assigned','notified','feedback_uploaded','rechecking','passed','signoff','failed','reopened'))` |
| `duplicate_pairs` | - | `CHECK(document_id_a < document_id_b)` |
| `rule_versions` | `effective_from / effective_to` | `CHECK(effective_to IS NULL OR effective_to > effective_from)` |

---

## 3. 规则效果评估

引用 M7-3 最终评测结果。评测样本 1,500 条，覆盖 74 条规则。

### 3.1 总体指标

| 指标 | 取值 |
| --- | --- |
| 总样本数 | 1,500 |
| 规则总数 | 74 |
| Precision | 1.0000 |
| Recall | 1.0000 |
| F1 | 1.0000 |
| Accuracy | 1.0000 |
| 未达标规则 | 0 |
| 降级候选 | 0 |

### 3.2 按 Milestone 分组

| Milestone | 规则数 | 样本数 | Precision | Recall | F1 |
| --- | --- | --- | --- | --- | --- |
| M1 | 14 | 280 | 1.0000 | 1.0000 | 1.0000 |
| M3 | 10 | 203 | 1.0000 | 1.0000 | 1.0000 |
| M4 | 24 | 483 | 1.0000 | 1.0000 | 1.0000 |
| M5 | 26 | 534 | 1.0000 | 1.0000 | 1.0000 |
| 合计 | 74 | 1,500 | 1.0000 | 1.0000 | 1.0000 |

### 3.3 评测结论

- 74 条规则全部达标，无未达标规则
- 0 条规则进入降级候选
- Precision / Recall / F1 / Accuracy 均为 1.0000
- 评测样本覆盖正例、负例、边界案例与冲突案例

---

## 4. 规则版本管理

### 4.1 rule_versions 表字段

| 字段 | 说明 |
| --- | --- |
| `rule_code` | 规则编码 |
| `version` | 版本号（v1.0.0） |
| `config_hash` | 规则配置哈希，用于检测配置变更 |
| `effective_from` | 生效起始时间 |
| `effective_to` | 生效结束时间（NULL 表示当前生效） |
| `allow_customer_output` | 是否允许进入客户正式问题 |
| `auto_confirm_allowed` | 是否允许自动确认 |
| `degraded` | 是否降级 |
| `degraded_reason` | 降级原因 |
| `degraded_at` | 降级时间 |

### 4.2 版本策略

- 每条规则初始版本为 `v1.0.0`
- 规则配置变更时生成新版本，旧版本 `effective_to` 设置为新版本 `effective_from`
- `config_hash` 用于快速判定规则配置是否发生变化
- 任意时刻同一 `rule_code` 仅有一条 `effective_to IS NULL` 的当前生效版本

---

## 5. 客户正式问题口径

`rule_versions.allow_customer_output` 字段决定某条规则产出的 finding 是否进入客户正式问题列表：

- `allow_customer_output = 1`：finding 进入客户正式问题
- `allow_customer_output = 0`：finding 仅作为内部参考，不对外披露

### 5.1 M3-M5 标记情况

| Milestone | 规则总数 | allow_customer_output = 1 | allow_customer_output = 0 |
| --- | --- | --- | --- |
| M3 | 10 | 10 | 0 |
| M4 | 24 | 16 | 8（DUP-001 ~ DUP-006、RECON-001、RECON-010） |
| M5 | 26 | 26 | 0 |
| 合计 | 60 | 52 | 8 |

M4 中 DUP-001 ~ DUP-006（重复文件检测）与 RECON-001 / RECON-010（双渠道对账）共 8 条规则不进入客户正式问题，仅作为内部整改与系统监控依据。

---

## 6. 证据链

### 6.1 finding_evidence 表

| 字段 | 说明 |
| --- | --- |
| `finding_id` | 关联 finding ID |
| `page_number` | 页码 |
| `coordinates` | 坐标（x, y, width, height） |
| `screenshot_path` | 截图文件路径 |
| `raw_value` | 命中原始值 |
| `evidence_json` | 结构化证据 JSON |

### 6.2 evidence_json 字段

`evidence_json` 字段以 JSON 格式记录每条 finding 的完整证据链：

- 命中页码与坐标
- 原始文本片段
- 规则匹配上下文
- 截图文件路径与哈希
- OCR 置信度（如适用）
- 媒体文件校验结果（如适用）

### 6.3 证据链用途

- 客户正式问题附件
- 复核与仲裁判定依据
- 整改反馈对照
- 审计追溯

---

## 7. 复核闭环

### 7.1 复核机制

| 表 | 用途 |
| --- | --- |
| `finding_reviews` | 单复核记录（一名复核人一次复核） |
| `review_assignments` | 双复核任务分配（支持多轮、多复核人） |
| `arbitration_status` | 仲裁状态（双复核意见冲突时触发仲裁） |

### 7.2 review_status 状态机

| 状态 | 说明 |
| --- | --- |
| `pending` | 待复核 |
| `reviewed` | 已复核（尚未确认） |
| `confirmed` | 复核确认（成立） |
| `rejected` | 复核驳回（不成立） |
| `excluded` | 排除（不进入客户正式问题） |

状态流转：

```
pending -> reviewed -> confirmed
                    -> rejected
                    -> excluded
```

### 7.3 双复核与仲裁

- `review_assignments` 支持为同一 finding 分配多名复核人（round 1 / round 2）
- 两名复核人意见一致 → 直接进入 `confirmed` 或 `rejected`
- 两名复核人意见冲突 → 触发 `arbitration_status` 仲裁流程
- 仲裁结果作为最终状态写入 `review_status`

---

## 8. 整改闭环

### 8.1 rectification_tasks 状态机

| 状态 | 说明 |
| --- | --- |
| `created` | 整改任务已创建 |
| `assigned` | 已分配整改责任人 |
| `notified` | 已通知责任人 |
| `feedback_uploaded` | 责任人已上传整改反馈 |
| `rechecking` | 复检中 |
| `passed` | 复检通过 |
| `signoff` | 已签收关闭 |
| `failed` | 复检不通过 |
| `reopened` | 重新打开 |

状态流转：

```
created -> assigned -> notified -> feedback_uploaded -> rechecking -> passed -> signoff
                                                                          -> failed -> reopened -> assigned
```

### 8.2 rectification_status_history

`rectification_status_history` 表记录每条整改任务的全部状态变更历史：

| 字段 | 说明 |
| --- | --- |
| `task_id` | 整改任务 ID |
| `from_status` | 变更前状态 |
| `to_status` | 变更后状态 |
| `operator_user_id` | 操作者 |
| `comment` | 变更备注 |
| `created_at` | 变更时间 |

每次状态变更均落库，确保整改过程完整可追溯。

---

## 9. 数据库健康监控

### 9.1 db_health_snapshots 表

周期性记录数据库健康指标：

| 字段 | 说明 |
| --- | --- |
| `db_size` | 数据库文件大小（字节） |
| `wal_size` | WAL 文件大小（字节） |
| `page_count` | 总页数 |
| `freelist_count` | 空闲页数 |
| `journal_mode` | 日志模式（WAL） |
| `table_counts_json` | 各表行数 JSON |
| `index_list_json` | 索引列表 JSON |
| `created_at` | 快照时间 |

### 9.2 监控用途

- 异常增长检测（如 finding 表行数突增）
- WAL 文件膨胀预警
- 空闲页过多触发 `VACUUM` 建议
- 索引缺失检测

---

## 10. 备份完整性

M6-8 备份演练 10 / 10 PASS，覆盖以下检查项：

| 序号 | 检查项 | 结果 |
| --- | --- | --- |
| 1 | 源库一致副本生成 | PASS |
| 2 | 在线备份 + SHA256 校验 | PASS |
| 3 | `PRAGMA integrity_check` | PASS |
| 4 | `PRAGMA quick_check` | PASS |
| 5 | `PRAGMA foreign_key_check` | PASS |
| 6 | 49 表行数比对全一致 | PASS |
| 7 | M3-M5 规则目录完整（14 前缀全在） | PASS |
| 8 | M3-M5 表查询演练通过 | PASS |
| 9 | Excel 导出演练通过 | PASS |
| 10 | M6 端点冒烟全 200 | PASS |

### 10.1 备份 SHA256 校验流程

1. 在线备份生成 `.db` 文件
2. 计算备份文件 SHA256
3. 与源库 SHA256 比对（源库在备份期间通过 WAL checkpoint 保证一致快照）
4. 恢复演练：在新实例上恢复备份，执行 `integrity_check` / `quick_check` / `foreign_key_check`
5. 49 表行数逐一比对
6. 规则目录完整性检查：M3-M5 共 14 个规则前缀全部存在
7. M3-M5 表查询演练：覆盖关键查询路径
8. Excel 导出演练：覆盖客户正式问题导出、整改清单导出
9. M6 端点冒烟：关键 API 端点全部返 200

### 10.2 备份保留策略

- 默认保留期：30 天
- 超期自动清理
- 保留期可通过配置项调整

---

## 11. 已知数据质量风险

| 风险项 | 描述 | 缓解措施 |
| --- | --- | --- |
| 历史 finding 缺 evidence_json | M1 早期产出的部分 finding 可能未填充 `evidence_json` 字段 | 建议执行一次性回填脚本，对历史 finding 重新提取证据链 |
| PDF / OCR 处理失败 | 部分 PDF 文档 OCR 处理失败，对应文档需人工复核 | `ocr_queue.status='failed'` 的文档列表定期巡检，人工介入 |
| 双渠道对账差异 | 主渠道与对账渠道数据可能存在新增差异，需定期执行对账 | RECON-001 / RECON-010 规则定期运行，差异进入 `recon_diffs` 表跟踪 |
| 备份保留期 | 默认 30 天，超期自动清理可能丢失长期归档 | 根据合规要求评估保留期，必要时迁移至长期归档存储 |
| 证据截图丢失 | 截图文件存储在文件系统，未做分布式备份 | 建议未来将截图纳入对象存储并做多副本 |

---

## 12. 结论

M0-M7 数据质量保障已按计划完成，覆盖数据完整性约束、规则效果评估、规则版本管理、客户正式问题口径、证据链、复核闭环、整改闭环、数据库健康监控、备份完整性等全链路。

最终评估结果：1,500 条评测样本在 74 条规则上取得 Precision=1.0000、Recall=1.0000、F1=1.0000、Accuracy=1.0000，0 未达标规则，0 降级候选。M6-8 备份演练 10 / 10 PASS。M7 全系统回归 347 passed / 0 failed。生产数据库 SHA256 全程未变。

已知数据质量风险已识别并给出缓解建议。本报告为主开发 AI 自验产出，待独立总验收。
