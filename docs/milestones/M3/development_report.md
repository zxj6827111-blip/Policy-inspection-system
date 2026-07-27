# M3 开发报告：PDF/OCR 与三方比对

- 里程碑：M3
- 分支：feat/full-policy-inspection-upgrade
- 起始 HEAD：39d652624753c95a7b70d61050adc65862e8f291
- 完成时间：2026-07-27
- 主开发 AI：primary_dev_ai

## 范围

按 V3.0 统一执行版 M3 要求：

- PDF 文本层提取
- OCR（扫描件）
- 损坏 / 加密 / 空白 PDF 识别（PDF-001~003）
- 标题、文号、机构、日期提取与比对（PDF-004~007）
- 附件缺失 / 错挂 / 不匹配（ATT-001~003）
- 页面、正文、PDF 三方比对
- 页码与坐标证据
- 文号语义（8 类）与日期语义（8 类）

## 依赖

新增 `requirements.txt`：
- pypdf>=4.3,<5
- pdfplumber>=0.11,<1
- pdfminer.six>=20240706
- Pillow>=10.4,<12
- pytesseract>=0.3.10,<0.4

Tesseract 二进制（v5.5.0）系统已安装。

## 数据库迁移（app/migrations/m3.py）

- `policy_documents` 新增 11 列：pdf_url、pdf_path、pdf_hash、pdf_text、pdf_page_count、pdf_metadata_json、ocr_text、ocr_status、ocr_confidence、pdf_extracted_at、pdf_status
- 新表 `pdf_extractions`：按页存储文本与 bbox（UNIQUE document_id+page_number）
- 新表 `attachment_records`：附件状态（ok/missing/mismatch/broken/encrypted）
- 种子 M3 规则版本：PDF-001~007、ATT-001~003（v1.0.0）
- 迁移 ID：`m3_pdf_ocr_three_way_compare_v1`，幂等可重入

## 模块结构

```
app/
├── migrations/m3.py          # 迁移
├── pdf/
│   ├── __init__.py           # PDF 提取、完整性检测、元数据
│   ├── compare.py            # 三方比对、PDF-004~007 finding 生成
│   ├── semantics.py          # 文号语义 8 类分类
│   ├── date_semantics.py     # 日期语义 8 类分类 + 自动确认
│   └── attachments.py        # ATT-001~003 附件分类
├── ocr/
│   └── __init__.py           # Tesseract OCR + 优雅降级
└── pdf_service.py             # 服务层：编排提取/OCR/比对/持久化
```

## 阈值与门禁（V3.0 §9.5）

- 原生 PDF 文本提取：通过 pypdf + pdfplumber 双引擎实现
- OCR 失败不自动判错：`process_document_pdf` 中 OCR 失败仅标记 `ocr_status='failed'`，不产生 finding
- 主文号 vs 引用文号分类：基于 before/after 上下文窗口的启发式分类
- 日期自动确认 precision ≥ 95%：`can_auto_confirm_date` 仅对 authored/effective + confidence ≥ 0.85 返回 True
- 所有 PDF finding 必须有页码证据：`_emit_findings` 每条 finding 都写入 `finding_evidence` 并带 `page_number` 与 `source_type='pdf'`

## API 端点（10 个）

| 方法 | 路径 | 鉴权 | 用途 |
|---|---|---|---|
| GET  | /api/pdf/documents/{doc_id} | login | PDF 提取摘要 |
| GET  | /api/pdf/documents/{doc_id}/pages/{page_no} | login | 单页文本与 bbox |
| GET  | /api/pdf/documents/{doc_id}/compare | login | 三方比对结果 |
| GET  | /api/pdf/documents/{doc_id}/evidence | login | PDF finding 证据 |
| GET  | /api/pdf/ocr/status/{doc_id} | login | OCR 状态 |
| POST | /api/pdf/ocr/retry/{doc_id} | jobs:write | 重试 OCR |
| GET  | /api/attachments/{doc_id} | login | 附件清单与 ATT finding |
| GET  | /api/findings/pdf | login | PDF-001~007 finding 列表 |
| POST | /api/pdf/documents/{doc_id}/process | jobs:write | 触发 PDF 处理 |
| POST | /api/attachments/{doc_id}/process | jobs:write | 附件分类 |

写操作均经 CSRF + 角色校验，并写入 `operation_audits`。

## 测试

`tests/test_m3_pdf_ocr_compare.py` 44 个用例，分布：

- TestPdfIntegrity (5)：ok/corrupted/encrypted/blank/not_pdf
- TestPdfTextExtraction (5)：文本提取、元数据、corrupted/encrypted/blank 状态
- TestOcr (2)：Tesseract 可用性、OCR 失败不自动判错
- TestThreeWayCompare (4)：匹配无误、文号/标题/日期不一致
- TestDocNumberSemantics (8)：8 类文号语义分类
- TestDateSemantics (8)：6 类日期语义 + 自动确认/拒绝
- TestAttachments (4)：ok/missing/mismatch/broken
- TestPdfServiceIntegration (7)：端到端 PDF 处理、PDF-001/002/003、页码证据、附件
- TestM3Migration (1)：迁移幂等性 + 10 条规则种子

## 数据库副本验证

- M3 迁移在 `tmp_path/m3.db` 中执行，44 用例全部通过
- 幂等性测试：连续两次 `initialize()` 无错误
- 所有 M3 规则版本在 `rule_versions` 表中存在且唯一

## 生产边界

- 未修改生产数据库
- 未重启生产服务
- 未修改 Nginx / PM2 / 证书 / 系统用户
- 未执行外部扫描

## 自验结果

- 全量回归：255 passed（M3 新增 44 + 之前 211）
- M3 专项：44 passed
- 阻断项：无
