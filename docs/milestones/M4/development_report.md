# M4 开发报告：异常归档 / 重复数据 / 双渠道对账

> 里程碑：M4
> 方案依据：`政策巡检系统完整改造实施方案 V3.0（统一执行版）` §10
> 分支：`feat/full-policy-inspection-upgrade`
> HEAD（M4 完成）：`ece71332aa9ef09c8a22260fbd7165683d199a57`
> HEAD（M4 开始前）：`79df3af614cf71db4db0800e4a6ed3205cccf891`
> 日期：2026-07-27
> 生产边界：未修改生产数据库 / 未重启生产服务 / 未修改生产 Nginx、PM2、证书

## 1. 范围

M4 实现 V3.0 方案 §10 全部能力：

1. **异常归档（ARCH-001~006）**
   - 现行有效文件连续两轮消失（ARCH-001）
   - 页面仍更新但栏目归档（ARCH-002）
   - 长期保留栏目异常归档（ARCH-003）
   - 市级存在区级长期缺失（ARCH-004）
   - 区级存在市级长期缺失（ARCH-005）
   - 迁移后原栏目无正确跳转（ARCH-006）

2. **重复数据（DUP-001~008）**
   - 技术重复 / 同栏目重复 / 不同 URL 同一政策文件
   - 跨栏目正常重复 / 跨渠道正常同步 / 同 PDF 不同页面重复
   - 文号相同内容不同 / 文号不同内容高度相似

3. **市级和区级双渠道匹配与差异对账（RECON-001~010）**
   - 市区一致 / 市级缺失 / 区级缺失
   - 标题、文号、日期、正文、附件不一致
   - 更新延迟 / 无法匹配（数据不足）

4. **文档身份**
   - 文档指纹（normalized doc number / title / body hash / simhash / fingerprint hash）

## 2. 设计要点

### 2.1 数据库迁移（幂等、可重入）

新增 5 张表：

| 表 | 用途 |
|---|---|
| `archive_observations` | 每轮每渠道对文档存在性的观察记录 |
| `archive_findings` | ARCH-001~006 异常归档 finding |
| `document_fingerprints` | 文档身份指纹（唯一约束 on document_id） |
| `duplicate_pairs` | DUP-001~008 重复对（a<b 防自反） |
| `reconciliation_matches` | RECON-001~010 市区对账结果 |

`policy_documents` 新增 8 列：`archive_status`、`is_archived`、`normal_archive`、`archive_reason`、`migration_target_url`、`cross_channel_document_id`、`fingerprint_hash`、`channel_level`。

**关键迁移：去除 `policy_documents.url` 的 UNIQUE 约束**
DUP-001 要求允许同一 URL 在跨轮次重扫时产生独立快照。SQLite 的 `UNIQUE` 列由 auto-index (`sqlite_autoindex_policy_documents_N`) 实现，不能用 `DROP INDEX` 删除。采用 SQLite 官方推荐的表重建方案：
- 检测 `policy_documents` 是否仍带 `url TEXT NOT NULL UNIQUE`；
- 捕获 `PRAGMA index_list` 中所有非 auto-index 的索引定义；
- 重建新表（去除 UNIQUE）→ 拷贝全量数据 → drop 旧表 → rename；
- 重新创建手动索引。

幂等：迁移检测到 UNIQUE 已去除则跳过。

### 2.2 ARCH 异常归档核心规则（§10.4 保证）

- **单轮缺失不判 ARCH-001**：必须连续 ≥ 2 轮缺失才发出 finding，避免单次扫描抖动。
- **正常排除**：`archive_status` 为 `repealed`（废止）/ `expired`（失效）自动标记 `normal_archive=1`，不进 finding。
- **栏目迁移**：`archive_status='migrated'` 且 `migration_target_url` 存在时不判 ARCH-001；迁移目标 URL 不可达时判 ARCH-006。
- **跨渠道对照**：ARCH-004（市级有但区级长期缺失）、ARCH-005（区级有但市级长期缺失）需读取对方渠道的 observations。
- **证据完整**：finding 的 `evidence_json` 同时给出 `last_present` 和 `last_missing` 两条 observation id，支持审计回溯。

### 2.3 DUP 重复数据分类顺序（§10.4 保证）

按优先级判断，避免误分类：

1. **DUP-001**（技术重复）：same url + same body_hash → `is_normal=True`，自动排除。
2. **DUP-002**（同栏目重复）：same target_key + same body_hash + diff url。
3. **DUP-005**（跨渠道正常同步）：跨 `channel_level` 且 same doc_number 或 same body_hash → `is_normal=True`，自动排除。**优先于 DUP-003 判定**，避免跨渠道同步被误判为不同 URL 同一政策。
4. **DUP-007**（文号相同内容不同）：same doc_number + diff body_hash → 严重冲突。**不要求 body_sim 阈值**，相同文号不同正文即冲突。
5. **DUP-008**（文号不同内容相同）：diff doc_number + same body_hash → 严重冲突。**优先于 DUP-003 判定**。
6. **DUP-003**（不同 URL 同一政策文件）：same doc_number + same title + (same agency 或 same body_hash)，**要求 same_doc_number** 以避免与 DUP-008 冲突。

### 2.4 RECON 双渠道对账

- 字段级证据：`title_match`、`doc_number_match`、`date_match`、`body_match`、`attachment_match` 全部记录到 `evidence_json`，含双方 URL。
- **RECON-010（数据不足）**：只有当至少一个字段实际匹配时才视为有匹配信号；字段非空但不匹配不算信号。evidence 包含所有字段对比和双方 URL。
- **RECON-009（更新延迟）**：字段全部匹配但 authored_date 差异超过阈值。
- **RECON-003（区级缺失）**：市级有文档但区级无候选。

### 2.5 API

新增 12 个端点（均带 `require_login` / `require_write("jobs:write")` / `require_roles("admin")` 权限和 CSRF，写入 `operation_audits`）：

| 端点 | 用途 |
|---|---|
| `POST /api/archive/observations/{doc_id}` | 记录一轮归档观察 |
| `POST /api/archive/evaluate/{doc_id}` | 评估 ARCH-001~006 |
| `GET /api/archive/findings` | 列出归档 findings |
| `POST /api/archive/findings/{id}/exclude` | 排除 finding |
| `POST /api/fingerprints/{doc_id}` | 计算并持久化指纹 |
| `GET /api/fingerprints/{doc_id}` | 读取指纹 |
| `POST /api/duplicates/detect/{doc_id}` | 检测重复 |
| `GET /api/duplicates` | 列出重复对 |
| `POST /api/duplicates/{id}/exclude` | 排除重复对 |
| `POST /api/reconciliation/reconcile/{municipal_id}` | 对账一份市级文档 |
| `POST /api/reconciliation/reconcile-all` | 全量对账 |
| `GET /api/reconciliation/matches` | 列出对账结果 |

## 3. 测试

### 3.1 M4 新增测试

文件：`tests/test_m4_archive_duplicates_recon.py`

- `TestM4Migration`：4 项（表创建、列添加、规则版本种子、幂等）。
- `TestArchiveObservations`：2 项（记录持久化、按渠道过滤）。
- `TestArchiveAnomalyDetection`：11 项（含 §10.4 保证：单轮不判 / 废止排除 / 失效排除 / 迁移正常 / ARCH-002~006 / 排除标记）。
- `TestFingerprints`：5 项（规范化、标题括号剥离、simhash 确定性、相似度、持久化）。
- `TestDuplicateClassification`：9 项（DUP-001/002/003/005/007/008 / 无重复 / 排除 / §10.4 跨渠道不判）。
- `TestReconciliation`：10 项（RECON-001/003/004/005/006/007/009/010 / 全量摘要 / 双方 URL 证据）。
- `TestM4API`：5 项（auth 必需 smoke：archive/fingerprint admin/duplicates login/recon login/archive findings login）。

**M4 新增通过数：45**

### 3.2 全量回归

```
cd "e:\Software Development\Policy inspection system"
.\.venv\Scripts\python.exe -m pytest tests/ -v --tb=short -p no:cacheprovider
```

结果：**300 passed in 213.24s**（M3 完成时为 255，M4 新增 45）

无回归。M0~M3 全部既有用例继续通过。

## 4. 生产边界确认

| 项 | 状态 |
|---|---|
| 修改生产数据库 | 否 |
| 重启生产服务 | 否 |
| 修改生产 Nginx / PM2 / 证书 | 否 |
| 高频外部扫描 | 否 |

M4 仅在测试用 tmp 数据库中验证迁移与功能；未触碰生产数据库 SHA256（`13e6a8a412f69c13d149674a31076854c966f5b151e670fc9e22d0f53ab77af3`）。

## 5. 已知限制 / 后续

- M4 仅产出 finding / duplicate_pairs / reconciliation_matches 数据模型与 API，UI 面板在 M6 统一渲染。
- OCR 与 PDF 文本层在 M3 已完成；M4 的 `body_hash` 直接复用 M3 写入的 `content_hash`。
- 规则效果中心（M2）未扩展到 ARCH/DUP/RECON 规则；将在 M6 评估是否纳入。

## 6. 交付物清单

- `app/migrations/m4.py`
- `app/archive/__init__.py`
- `app/duplicates/__init__.py`
- `app/reconciliation/__init__.py`
- `app/main.py`（新增 12 端点）
- `app/db.py`（新增 `_migrate_m4` 调用）
- `tests/test_m4_archive_duplicates_recon.py`
- `docs/milestones/M4/development_report.md`
- `docs/milestones/M4/self_acceptance_report.md`
- `tmp/program_control/M4_self_acceptance.json`
- `tmp/program_control/M4_commands.txt`
- `tmp/program_control/M4_test_output.txt`
