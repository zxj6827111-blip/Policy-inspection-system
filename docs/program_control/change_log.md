# 变更日志（Change Log）

> 本文件按时间顺序记录 M0～M7 主开发过程中所有代码、数据库、配置、文档变更。
> 主开发 AI 每完成一个里程碑步骤即追加一条记录。
> 严守生产边界：M0～M6 不修改生产数据库、不重启生产服务、不修改生产 Nginx/PM2/证书。

---

## 2026-07-27 M0 启动

### 基线快照（M0 开始时）

- 当前分支：`feat/phase1-security-findings-org`
- 当前 HEAD：`f862c75ed1916dfce67d9dea90667b5775a2d789`
- 工作区状态：仅 `.gitignore` 有修改（扩展规划文档忽略规则，无敏感文件入库）
- 本地数据库 SHA256：`13E6A8A412F69C13D149674A31076854C966F5B151E670FC9E22D0F53AB77AF3`（与生产基线一致）
- 测试基线：184 passed
- 已跟踪敏感文件检查：无 `.db` / `.env` / `.key` / `.pem` / `.log` / `secret` 被跟踪

### M0-1 创建总控文件

- 新增 `docs/program_control/full_upgrade_plan_v3.md`（V3.0 等价方案，整合 V2.0 业务目标与 M0～M7 里程碑结构）
- 新增 `docs/program_control/status.json`
- 新增 `docs/program_control/change_log.md`（本文件）
- 新增 `docs/program_control/risk_register.md`
- 新增 `docs/program_control/final_task_matrix.md`

### M0-2 创建主开发分支

- 从 `feat/phase1-security-findings-org`（HEAD `f862c75`）创建 `feat/full-policy-inspection-upgrade`
- 不 push、不合并 main

### M0-3 提交 .gitignore 改进

- `chore(git): expand planning-doc ignore patterns and formalize workspace`
- 工作区随后干净

### M0-4 自验

- 工作区干净
- 184 测试通过
- 生产数据库 SHA256 未变
- 未 push、未合并 main、未重启生产服务
- 自验 PASS

### M0 完成

- HEAD：`e9c2fb24fb46fb79f814b253e6b1482fc0a58b48`
- 产物：`docs/milestones/M0/development_report.md`、`docs/milestones/M0/self_acceptance_report.md`、`tmp/program_control/M0_self_acceptance.json`、`tmp/program_control/M0_commands.txt`、`tmp/program_control/M0_test_output.txt`
- status.json 更新：M0=DONE，M1=IN_PROGRESS
- 自动进入 M1

---

## 2026-07-27 M1 启动

### M1-1 基线记录

- 分支：`feat/full-policy-inspection-upgrade`
- 开始 HEAD：`e9c2fb24fb46fb79f814b253e6b1482fc0a58b48`
- 生产数据库 SHA256：`13e6a8a412f69c13d149674a31076854c966f5b151e670fc9e22d0f53ab77af3`（未变）

### M1-2 实现 API 文档保护

- `app/main.py` 新增 `protect_api_docs` 中间件
- 生产环境 `/docs` `/redoc` `/openapi.json` 返回 404
- 非生产环境保持可访问

### M1-3 多角色权限矩阵测试

- 新增 `tests/test_role_permission_matrix.py`（5 项：operator/auditor/viewer 边界 + 能力合约 + UI 可达）
- 注册 `real_auth` pytest mark

### M1-4 全量回归

- 196 passed（184 + 7 文档保护 + 5 角色矩阵）
- 生产数据库 SHA256 未变

### M1 完成

- HEAD：`9ddcf2117cd55e55c49e50309560e2cb31d35f8d`
- 产物：`docs/milestones/M1/development_report.md`、`docs/milestones/M1/self_acceptance_report.md`、`tmp/program_control/M1_self_acceptance.json`、`tmp/program_control/M1_commands.txt`、`tmp/program_control/M1_test_output.txt`
- status.json 更新：M1=DONE，M2=IN_PROGRESS
- 自动进入 M2

---

## 2026-07-27 M3 启动

### M3-1 基线记录

- 分支：`feat/full-policy-inspection-upgrade`
- 开始 HEAD：`39d652624753c95a7b70d61050adc65862e8f291`
- 生产数据库 SHA256：`13e6a8a412f69c13d149674a31076854c966f5b151e670fc9e22d0f53ab77af3`（未变）

### M3-2 依赖安装

新增 `requirements.txt`：pypdf、pdfplumber、pdfminer.six、Pillow、pytesseract
Tesseract 二进制 v5.5.0 系统已安装。

### M3-3 数据库迁移

- `app/migrations/m3.py`：新增 `pdf_extractions`、`attachment_records` 表
- `policy_documents` 新增 11 列：pdf_url/path/hash/text/page_count/metadata_json、ocr_text/status/confidence、pdf_extracted_at/pdf_status
- 种子 M3 规则版本：PDF-001~007、ATT-001~003 (v1.0.0)
- 通过 `Database.initialize()` 自动幂等执行

### M3-4 模块结构

```
app/
├── migrations/m3.py
├── pdf/__init__.py、compare.py、semantics.py、date_semantics.py、attachments.py
├── ocr/__init__.py
└── pdf_service.py
```

### M3-5 API 端点

10 个 M3 端点加入 `app/main.py`：
- GET /api/pdf/documents/{doc_id}、/pages/{page_no}、/compare、/evidence
- GET /api/pdf/ocr/status/{doc_id}
- POST /api/pdf/ocr/retry/{doc_id}、/api/pdf/documents/{doc_id}/process、/api/attachments/{doc_id}/process
- GET /api/attachments/{doc_id}、/api/findings/pdf

### M3-6 测试

- `tests/test_m3_pdf_ocr_compare.py` 44 用例
- 修复加密 PDF 检测（使用非空 user_password）、外键约束（先 seed scan_jobs）、文号语义 before/after 上下文窗口
- 全部通过

### M3 完成

- M3 专项：44 passed
- 全量回归：255 passed
- 产物：
  - `docs/milestones/M3/development_report.md`
  - `docs/milestones/M3/self_acceptance_report.md`
  - `tmp/program_control/M3_self_acceptance.json`
  - `tmp/program_control/M3_commands.txt`
  - `tmp/program_control/M3_test_output.txt`
- status.json 更新：M3=DONE，M4=IN_PROGRESS
- 自动进入 M4

---

## 2026-07-27 M4 启动

### M4-1 基线记录

- 分支：`feat/full-policy-inspection-upgrade`
- 开始 HEAD：`79df3af614cf71db4db0800e4a6ed3205cccf891`（M3 完成后）
- 生产数据库 SHA256：`13e6a8a412f69c13d149674a31076854c966f5b151e670fc9e22d0f53ab77af3`（未变）

### M4-2 数据库迁移

- 新增 5 张表：`archive_observations`、`archive_findings`、`document_fingerprints`、`duplicate_pairs`、`reconciliation_matches`
- `policy_documents` 新增 8 列：`archive_status`、`is_archived`、`normal_archive`、`archive_reason`、`migration_target_url`、`cross_channel_document_id`、`fingerprint_hash`、`channel_level`
- 种子 M4 规则版本：ARCH-001~006、DUP-001~008、RECON-001~010 (v1.0.0)
- **关键：去除 `policy_documents.url` 的 UNIQUE 约束**。SQLite auto-index 不能用 `DROP INDEX` 删除，采用 SQLite 官方推荐的表重建方案：检测 UNIQUE → 捕获手动索引定义 → 创建新表 → 拷贝数据 → drop 旧表 → rename → 重建索引。幂等。

### M4-3 模块结构

```
app/
├── migrations/m4.py
├── archive/__init__.py
├── duplicates/__init__.py
└── reconciliation/__init__.py
```

### M4-4 API 端点

12 个 M4 端点加入 `app/main.py`（全部带 auth/CSRF/audit）：
- archive/observations、archive/evaluate、archive/findings（含 exclude）
- fingerprints（compute / get）
- duplicates/detect、duplicates（含 exclude）
- reconciliation/reconcile、reconcile-all、matches

### M4-5 测试

- `tests/test_m4_archive_duplicates_recon.py` 45 用例
- 修复 DUP-001 / 排除测试中 UNIQUE 约束冲突（表重建方案）
- 全部通过

### M4 完成

- M4 专项：45 passed
- 全量回归：300 passed（M3 完成时 255，M4 新增 45）
- HEAD（M4 完成）：`ece71332aa9ef09c8a22260fbd7165683d199a57`
- 产物：
  - `docs/milestones/M4/development_report.md`
  - `docs/milestones/M4/self_acceptance_report.md`
  - `tmp/program_control/M4_self_acceptance.json`
  - `tmp/program_control/M4_commands.txt`
  - `tmp/program_control/M4_test_output.txt`
- status.json 更新：M4=DONE，M5=IN_PROGRESS
- 自动进入 M5

---

## 2026-07-27 M5 启动

### M5-1 基线记录

- 分支：`feat/full-policy-inspection-upgrade`
- 开始 HEAD：`ece71332aa9ef09c8a22260fbd7165683d199a57`（M4 完成后）
- 生产数据库 SHA256：未变
- 范围：阅办联动深度检查 / 多类型政策解读 / 整改、复查、销号 / 超期和复发

### M5-2 数据库迁移

- 新增 6 张表：`reading_linkage_checks`、`media_checks`、`policy_interpretations`、`interpretation_checks`、`rectification_tasks`、`rectification_status_history`
- `policy_documents` 新增 4 列：`reading_check_status`、`interpretation_count`、`has_interpretation_mismatch`、`rectification_status`
- 种子 M5 规则版本 26 条：READ-001~005、MEDIA-001~006、INTERP-001~008、RECT-001~007 (v1.0.0)
- 迁移幂等：`test_migration_is_idempotent` 验证

### M5-3 模块结构

```
app/
├── migrations/m5.py
├── reading_linkage/__init__.py
├── media/__init__.py
├── interpretation/__init__.py
└── rectification/__init__.py
```

### M5-4 规则实现

- READ-001~005：阅办按钮缺失 / 不可点击 / 跳转失败 / 空白页 / 暂无数据
- MEDIA-001~006：图片缺失 / 破损 / 视频缺失 / 失效 / 空资源 / 403-429 拦截
- INTERP-001~008：解读链接失效 / 对象错误 / 发布时间缺失 / 错挂 / 图片失效 / 视频失效 / 空白 / 类型缺失
- RECT-001~007：无责任单位 / 超期 / 证据缺失 / 未通过未重发 / 销号后复发 / 状态历史缺失 / 期限缺失
- 403/429/验证码/临时网络问题 → `transient_error`，`allow_customer_output=false`，不进入客户正式问题
- 解读对象匹配：规范化文号 + 规范化标题计算置信度，低于阈值则 `has_interpretation_mismatch=1`

### M5-5 API 端点

9 个 M5 端点加入 `app/main.py`（全部带 auth/CSRF/audit）：
- POST /api/reading-checks/{doc_id}
- POST /api/media-checks/{doc_id}
- POST /api/interpretations/{doc_id}、/api/interpretation-checks/{interp_id}
- POST /api/rectification/tasks、/api/rectification/tasks/{task_id}/transition
- GET /api/rectification/tasks/{task_id}、/api/rectification/overdue、/api/rectification/recurrence

### M5-6 测试

- `tests/test_m5_reading_interp_rectification.py` 47 用例
- 修复 `MediaCheckInput` dataclass 字段顺序错误（非默认参数 `media_type` 必须在默认参数 `job_id` 之前）
- 全部通过

### M5 完成

- M5 专项：47 passed
- 全量回归：347 passed（M4 完成时 300，M5 新增 47）
- V3.0 §11.5 门禁全部 PASS：
  - 正常重定向不误报
  - 403/429 不进入正式问题
  - 视频页面打开但视频失效可识别
  - 200 空页面可识别
  - 解读对象错误可识别
  - 所有正式问题可创建整改任务
  - 整改前后证据完整
  - 状态可追溯
  - 销号后复发可识别
- 产物：
  - `docs/milestones/M5/development_report.md`
  - `docs/milestones/M5/self_acceptance_report.md`
  - `tmp/program_control/M5_self_acceptance.json`
  - `tmp/program_control/M5_commands.txt`
  - `tmp/program_control/M5_test_output.txt`
- status.json 更新：M5=DONE，M6=IN_PROGRESS
- 自动进入 M6

---

## 2026-07-27 M6 启动

### M6-1 基线记录

- 分支：`feat/full-policy-inspection-upgrade`
- 开始 HEAD：M5 完成后提交点
- 生产数据库 SHA256：未变
- 范围：最终 UI / 口径 / 性能 / OCR 队列 / 数据库锁治理 / 日志监控 / 备份恢复回滚 / 生产副本演练

