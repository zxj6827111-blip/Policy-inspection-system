# 07 最终迁移报告

> 主开发 AI 自验产出，待独立总验收
> 版本：V3.0
> 生成日期：2026-07-27
> 项目：上海区级政策文件安全巡检系统
> 当前分支：feat/full-policy-inspection-upgrade
> 当前 HEAD：02cb6e22341e2940abb0e657d27b9ba2c7b2cae9

---

## 1. 执行摘要

本报告汇总 M0-M7 全阶段数据库迁移方案、迁移清单、生产切换步骤与回滚预案，作为政策文件安全巡检系统进入独立总验收前的自验结论依据。

### 1.1 迁移总览

| 项 | 数值 |
|---|---|
| 迁移里程碑数 | 7（Phase1 + M2 + M3 + M4 + M5 + M6） |
| 新增表数 | 31 |
| 扩展表数 | 4（policy_documents、rule_versions、finding_reviews、rule_findings） |
| 新增规则种子 | 74（M1 14 + M3 10 + M4 24 + M5 26） |
| 性能索引 | 19（M6） |
| 系统设置种子 | 12（M6） |
| 破坏性变更 | 1（M4 删除 url UNIQUE 约束，表重建） |

### 1.2 迁移原则

- 幂等：所有迁移支持 INSERT OR REPLACE 重复执行
- 只增不删：字段只新增不删除（M4 url UNIQUE 删除为唯一例外）
- 向后兼容：旧数据在新 schema 下可正常工作
- 顺序执行：按 milestone 顺序串行执行
- 工作区先行：所有迁移在工作区数据库上验证，生产数据库未变

### 1.3 边界保持

- 工作区数据库 SHA256：5F35DE29396143B2F1EA5BE328505D37180A2AC2686898357D4BC309E76398F8
- 生产数据库 SHA256（未变）：13E6A8A412F69C13D149674A31076854C966F5B151E670FC9E22D0F53AB77AF3
- M0-M6 全程未修改生产数据库

---

## 2. 迁移机制

### 2.1 schema_migrations 追踪

`schema_migrations` 表记录已应用的迁移名与执行时间：

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
    name TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);
```

每个迁移块执行前检查 `schema_migrations` 是否已存在该 name，已存在则跳过；不存在则执行迁移并 INSERT OR REPLACE 记录。

### 2.2 迁移幂等

- 所有 DDL 使用 `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`
- 所有种子数据使用 `INSERT OR REPLACE`
- 列扩展使用 `ALTER TABLE ... ADD COLUMN`（SQLite 不支持 IF NOT EXISTS，通过 try-except 包装捕获重复列错误）
- 迁移可重复执行，不会产生重复数据或错误

### 2.3 Database.initialize() 执行顺序

```python
def initialize(self):
    self._connect()
    self._migrate()              # Phase 1 base + schema_migrations
    self._seed_agencies()        # 机关代字种子
    self._migrate_phase1()       # M1 规则与扩展字段
    self._migrate_m2()           # M2 复核与规则效果
    self._migrate_m3()           # M3 PDF/OCR/附件
    self._migrate_m4()           # M4 归档/重复/对账（含 url UNIQUE 删除）
    self._migrate_m5()           # M5 阅办/解读/整改
    self._migrate_m6()           # M6 性能/队列/可观测性
```

### 2.4 只增不删原则

- 字段：仅 ADD COLUMN，不 DROP COLUMN
- 表：仅 CREATE，不 DROP
- 索引：仅 CREATE，不 DROP
- 唯一例外：M4 删除 policy_documents.url 的 UNIQUE 约束（SQLite 不支持直接 DROP CONSTRAINT，通过表重建实现）

---

## 3. 迁移清单（按 milestone）

### 3.1 Phase1 (M1)

**新增表（12 张）：**

| 表名 | 用途 |
|---|---|
| schema_migrations | 迁移追踪 |
| users | 用户账号 |
| roles | 角色定义 |
| user_roles | 用户-角色关联 |
| sessions | 会话管理 |
| operation_audits | 操作审计日志 |
| rule_versions | 规则版本管理 |
| finding_evidence | 证据存储 |
| finding_reviews | 复核记录 |
| review_reason_codes | 复核原因码 |
| organizations | 机构主表 |
| organization_aliases | 机构别名 |
| document_symbols | 机关代字 |

**rule_findings 扩展列（6 列）：**

- machine_status
- human_status
- rule_version
- confidence
- allow_customer_output
- evidence_json

**规则种子：** 14 条 M1 规则

### 3.2 M2

**新增表（2 张）：**

| 表名 | 用途 |
|---|---|
| review_assignments | 复核任务分配 |
| rule_effect_snapshots | 规则效果快照 |

**列扩展：**

- finding_reviews 增 `review_round` 列
- rule_versions 增以下列：
  - auto_confirm_allowed
  - degraded
  - degraded_reason
  - degraded_at

**规则种子：** 无新规则（M2 复用 M1 规则，新增复核流程）

### 3.3 M3

**新增表（2 张）：**

| 表名 | 用途 |
|---|---|
| pdf_extractions | PDF 抽取记录 |
| attachment_records | 附件记录 |

**policy_documents 扩展列（11 列）：**

- pdf_url
- pdf_path
- pdf_hash
- pdf_text
- pdf_page_count
- pdf_metadata_json
- ocr_text
- pdf_status
- pdf_confidence
- pdf_extracted_at
- pdf_status

**规则种子：** 10 条 M3 规则

### 3.4 M4

**新增表（5 张）：**

| 表名 | 用途 |
|---|---|
| archive_observations | 异常归档观测 |
| archive_findings | 异常归档发现 |
| document_fingerprints | 文档指纹 |
| duplicate_pairs | 重复对 |
| reconciliation_matches | 双渠道对账匹配 |

**policy_documents 扩展列（8 列）：** 归档、重复检测、对账相关字段

**破坏性变更：** 删除 url UNIQUE 约束（通过表重建实现，已验证）

- 原因：同一 URL 可能对应多个政策文档版本
- 实现：SQLite 不支持 DROP CONSTRAINT，通过表重建
- 验证：M4 专项测试 45 用例全部通过 + M6-8 演练第 6 步 49 表行数比对一致

**规则种子：** 24 条 M4 规则

### 3.5 M5

**新增表（6 张）：**

| 表名 | 用途 |
|---|---|
| reading_linkage_checks | 阅办联动检查 |
| media_checks | 媒体检查 |
| policy_interpretations | 政策解读 |
| interpretation_checks | 解读检查 |
| rectification_tasks | 整改任务 |
| rectification_status_history | 整改状态历史 |

**policy_documents 扩展列（4 列）：** 阅办/媒体/解读/整改相关字段

**规则种子：** 26 条 M5 规则

### 3.6 M6

**新增表（3 张）：**

| 表名 | 用途 |
|---|---|
| ocr_queue | OCR 任务队列 |
| system_settings | 系统设置 |
| db_health_snapshots | 数据库健康快照 |

**性能索引：** 19 条（覆盖 rule_findings / policy_documents / review_assignments / archive_findings 等高频查询路径）

**系统设置种子：** 12 项

**规则种子：** 无新规则

---

## 4. 生产迁移步骤（M7 切换时执行）

### 4.1 步骤清单

| 步骤 | 操作 | 工具 | 通过条件 |
|---|---|---|---|
| 1 | 备份生产数据库 | backup_database | 备份文件存在 + SHA256 记录 |
| 2 | 在备份副本上执行完整迁移 | Database.initialize()（dry_run） | 副本迁移成功 |
| 3 | 校验副本完整性 | integrity_check / quick_check / foreign_key_check | 三项全部 ok |
| 4 | 在副本上运行全量回归 | pytest | 347 passed / 0 failed |
| 5 | 在副本上运行 1500 条评测 | 评测脚本 | P/R/F1/Acc = 1.0000 |
| 6 | 在副本上运行 10 步演练 | M6-8 演练脚本 | 10/10 PASS |
| 7 | 维护窗口内执行真实迁移 | Database.initialize() | 生产库迁移成功 |
| 8 | 运行冒烟脚本 | scripts/smoke_test.py | 全部断言通过 |
| 9 | 观察日志与 metrics | 1 小时 | 无 ERROR / 无指标异常 |

### 4.2 维护窗口

- 建议时长：2 小时（迁移 30 分钟 + 冒烟 15 分钟 + 观察 60 分钟 + 缓冲 15 分钟）
- 建议时段：业务低峰期（凌晨 02:00-04:00）
- 通知：迁移前 24 小时通知所有干系人

### 4.3 步骤 1 备份要求

```python
backup_path = backup_database(production_db_path)
# 记录 backup_path 与 SHA256
backup_sha = sha256(backup_path)
# 备份文件保留至独立总验收完成
```

### 4.4 步骤 2 dry_run

在备份副本上执行迁移，不触碰生产库。dry_run 通过后再进入步骤 7。dry_run 验证迁移脚本本身的可执行性与幂等性。

### 4.5 步骤 7 真实迁移

在维护窗口内对生产库执行 `Database.initialize()`，由于迁移幂等，可重复执行。若中途异常，立即触发回滚预案。

---

## 5. 回滚预案

### 5.1 回滚函数

```python
def rollback_to_pre_migration(db, backup_path, migration_name):
    """
    回滚到迁移前状态。
    1. 校验 backup_path 完整性
    2. 关闭当前 db 连接
    3. restore_database(db.path, backup_path)
    4. 重新打开 db
    5. 校验 schema_migrations 中 migration_name 不存在
    """
```

### 5.2 回滚触发条件

- 步骤 8 冒烟脚本失败
- 步骤 9 观察期内出现严重 ERROR
- 步骤 9 观察期内核心指标异常
- 任何步骤出现数据完整性校验失败

### 5.3 回滚前 dry_run 校验

- 在 backup_path 上执行 `restore_database` 到独立路径
- 校验恢复后的库完整性（integrity_check / quick_check / foreign_key_check）
- 校验 schema_migrations 中目标 migration_name 不存在
- 校验通过后方可对生产库执行回滚

### 5.4 回滚时间预算

- 回滚本身：5-10 分钟（restore + 校验）
- 回滚后冒烟：5 分钟
- 总预算：15-20 分钟

### 5.5 回滚后处理

- 通知所有干系人回滚完成
- 保留故障现场日志与 metrics 供分析
- 故障分析完成后制定修复计划，重新进入步骤 1

---

## 6. 数据保留与兼容

### 6.1 向后兼容原则

- 所有迁移向后兼容
- 字段只增不删（M4 url UNIQUE 删除为唯一破坏性变更，已在 M4 完成并验证）
- 旧数据在新 schema 下可正常工作

### 6.2 旧 finding 数据兼容

- 旧 finding 记录保留默认值：
  - `machine_status='confirmed'`
  - `human_status='pending'`
- 新规则不会误触发旧 finding
- 新规则版本通过 `rule_versions` 表独立管理

### 6.3 规则版本管理

- 每条规则在 `rule_versions` 表中有独立版本记录
- 规则升级通过新增版本实现，不修改历史版本
- finding 记录关联到具体的 `rule_version`，确保可追溯
- 降级规则通过 `degraded` / `degraded_reason` / `degraded_at` 字段标记

### 6.4 破坏性变更说明

M4 删除 `policy_documents.url` 的 UNIQUE 约束是唯一破坏性变更：

- 原因：同一 URL 可能对应多个政策文档版本
- 实现：SQLite 不支持 DROP CONSTRAINT，通过表重建
- 验证：
  - M4 专项测试 45 用例全部通过
  - M6-8 演练第 6 步 49 表行数比对一致
  - M7-3 评测 24 条 M4 规则全部 P/R/F1=1.0000

---

## 7. 生产边界保持证据

### 7.1 SHA256 对比

| 项 | SHA256 | 状态 |
|---|---|---|
| 工作区数据库 | 5F35DE29396143B2F1EA5BE328505D37180A2AC2686898357D4BC309E76398F8 | 持续演进 |
| 生产数据库 | 13E6A8A412F69C13D149674A31076854C966F5B151E670FC9E22D0F53AB77AF3 | M0-M6 未变 |

### 7.2 边界保持措施

- 所有迁移在工作区数据库上验证
- 生产数据库仅通过备份副本接触（M6-8 演练）
- 备份副本与生产库 SHA256 一致性已校验
- 生产库从未直接执行任何 DDL

### 7.3 M6-8 生产副本演练证据

M6-8 演练在生产副本（非生产库本身）上完成 10 步全 PASS：

- 第 1 步：源库一致副本（副本 SHA256 = 生产库 SHA256）
- 第 2-10 步：在副本上完成备份、迁移、校验、回归、评测、演练
- 演练过程中生产库 SHA256 持续未变

### 7.4 自验测试边界

- 347 测试全部在工作区数据库上执行
- 测试用 autouse fixture 提供独立临时数据库路径
- 测试不接触生产库或生产副本

---

## 8. 结论

### 8.1 自验结论

- M0-M6 迁移方案完整，覆盖 26 张新表、74 条规则、19 条索引、12 项系统设置
- 所有迁移幂等且向后兼容
- 唯一破坏性变更（M4 url UNIQUE 删除）已验证
- 生产数据库 M0-M6 全程未变
- M6-8 生产副本演练 10/10 PASS
- M7-3 评测 74 条规则全部 P/R/F1=1.0000

### 8.2 待独立总验收

本文档为**主开发 AI 自验产出**，待独立总验收。独立总验收应至少覆盖：

1. 生产备份完整性独立校验
2. 生产副本迁移演练独立复跑
3. 回滚预案独立演练
4. 生产切换实战（在独立总验收监督下）
5. 切换后 24 小时观察

### 8.3 交付状态

- 迁移代码：Database.initialize() 及各 _migrate_* 方法，已入库
- 迁移报告：本文件
- 回滚代码：rollback_to_pre_migration，已入库
- 备份恢复代码：backup_database / restore_database，已入库
- 生产副本演练记录：M6-8 演练报告（10/10 PASS）

---

## 附录 A：迁移名称对照

| Milestone | 迁移名前缀 | 主要内容 |
|---|---|---|
| Phase1 | phase1_ | schema_migrations + 12 张基础表 + 14 规则 |
| M2 | m2_ | review_assignments + rule_effect_snapshots + 列扩展 |
| M3 | m3_ | pdf_extractions + attachment_records + 11 列扩展 + 10 规则 |
| M4 | m4_ | 5 张归档/重复/对账表 + 8 列扩展 + url UNIQUE 删除 + 24 规则 |
| M5 | m5_ | 6 张阅办/解读/整改表 + 4 列扩展 + 26 规则 |
| M6 | m6_ | 3 张运维表 + 19 索引 + 12 系统设置 |

## 附录 B：相关 SHA256

| 项 | SHA256 |
|---|---|
| 工作区数据库 | 5F35DE29396143B2F1EA5BE328505D37180A2AC2686898357D4BC309E76398F8 |
| 生产数据库（未变） | 13E6A8A412F69C13D149674A31076854C966F5B151E670FC9E22D0F53AB77AF3 |

## 附录 C：生产切换检查清单

- [ ] 步骤 1：生产数据库备份完成 + SHA256 记录
- [ ] 步骤 2：副本迁移 dry_run 成功
- [ ] 步骤 3：副本完整性三项校验全部 ok
- [ ] 步骤 4：副本全量回归 347 passed / 0 failed
- [ ] 步骤 5：副本 1500 条评测 P/R/F1/Acc = 1.0000
- [ ] 步骤 6：副本 10 步演练 10/10 PASS
- [ ] 步骤 7：维护窗口内生产库迁移成功
- [ ] 步骤 8：冒烟脚本全部断言通过
- [ ] 步骤 9：1 小时观察期无 ERROR / 无指标异常
- [ ] 回滚预案就绪（backup_path + rollback_to_pre_migration）

## 附录 D：表数量统计

| 类别 | 数量 |
|---|---|
| Phase1 新增表 | 12 |
| M2 新增表 | 2 |
| M3 新增表 | 2 |
| M4 新增表 | 5 |
| M5 新增表 | 6 |
| M6 新增表 | 3 |
| **合计新增表** | **31** |
| 扩展表（含重复） | policy_documents / rule_versions / finding_reviews / rule_findings |
| M6-8 演练验证表数 | 49 |

---

文档结束。
