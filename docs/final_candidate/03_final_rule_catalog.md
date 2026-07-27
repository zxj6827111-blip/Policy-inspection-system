# 上海区级政策文件安全巡检系统 最终规则目录

> 文档版本：V3.0（修订版，修复 R-P0-3）
> 修订日期：2026-07-28
> 文档性质：主开发 AI 自验产出，待独立总验收
> 当前分支：feat/full-policy-inspection-upgrade
> 当前 HEAD：79dacc0e0cc8e210d5d72c7042266e3568f83135
> 真源：`data/policy_inspector.db` 的 `rule_versions` 表 + 代码实现

---

## 1. 概述

本文档完整列出最终交付的全部 74 条规则，按 milestone 分章节，每个章节内按规则码排序。

**真源约定**：规则码、描述（description）、`allow_customer_output`、版本均以 `rule_versions` 表为唯一真源；milestone 与 category 依据代码模块归属标注。本文档不含任何 DB 中不存在的规则码。

### 1.1 规则总览

| Milestone | 规则数 | 规则码区间 |
|-----------|--------|------------|
| M1 | 14 | AGENCY / DATE / DOC / META / ORG |
| M3 | 10 | ATT / PDF |
| M4 | 24 | ARCH-001~006、DUP-001~008、RECON-001~010 |
| M5 | 26 | READ-001~005、MEDIA-001~006、INTERP-001~008、RECT-001~007 |
| 合计 | 74 | — |

### 1.2 字段说明

| 字段 | 说明 |
|------|------|
| 规则码 | `rule_versions.rule_id`，唯一标识，形如 `ARCH-001` |
| Milestone | 引入该规则的里程碑（依据代码模块归属） |
| Category | 规则类别（依据规则码前缀与代码 category 标注） |
| 名称 | 规则短名（取自 DB description） |
| 描述 | 规则检测逻辑描述，与 `rule_versions.description` 字段一致（保留中文原文） |
| allow_customer_output | 0=不进入客户正式问题清单；1=进入 |
| 版本 | `rule_versions.version`，全部为 1.0.0（文档展示为 v1.0.0） |
| 状态 | 规则状态，全部为 active |

### 1.3 客户问题边界

`allow_customer_output=0` 的规则为内部观测规则，结果不进入客户正式问题清单，仅用于系统内部统计、运营分析与人工复核线索。共 20 条：

- **M1（9 条）**：AGENCY-001、AGENCY-002、AGENCY-003、DOC-003、ORG-001、ORG-002、ORG-003、ORG-004、ORG-005
- **M3（3 条）**：PDF-001、PDF-002、PDF-003
- **M4（8 条）**：DUP-001、DUP-002、DUP-003、DUP-004、DUP-005、DUP-006、RECON-001、RECON-010
- **M5（0 条）**

其余 54 条规则 `allow_customer_output=1`，结果进入客户正式问题清单。

> 修订说明（R-P0-3）：旧版文档将 M4 全部 24 条（ARCH/DUP/RECON）标记为 `allow_customer_output=0`，与 DB 不符。DB 实际语义为：ARCH-001~006 全部进入客户正式问题；DUP-007/008、RECON-002~009 进入客户正式问题。本文档已按 DB 修正。

---

## 2. M1 基础规则（14 条）

> 代码实现：`app/rules.py`（evaluate_record、evaluate_organization_rules）、`app/rules_legacy_agency.py`

| 规则码 | Milestone | Category | 名称 | 描述 | allow_customer_output | 版本 | 状态 |
|--------|-----------|----------|------|------|-----------------------|------|------|
| AGENCY-001 | M1 | 机构代字 | 机构与代字疑似不匹配 | 兼容保留：机构与代字疑似不匹配，不进入客户正式问题 | 0 | v1.0.0 | active |
| AGENCY-002 | M1 | 机构代字 | 标题主体提示 | 兼容保留：标题主体提示 | 0 | v1.0.0 | active |
| AGENCY-003 | M1 | 机构代字 | 落款提示 | 兼容保留：落款提示 | 0 | v1.0.0 | active |
| DATE-001 | M1 | 日期 | 公开超期 | 公开超期 | 1 | v1.0.0 | active |
| DATE-002 | M1 | 日期 | 日期倒置 | 日期倒置 | 1 | v1.0.0 | active |
| DOC-001 | M1 | 文号 | 页面文号与正文不一致 | 页面文号与正文不一致 | 1 | v1.0.0 | active |
| DOC-002 | M1 | 文号 | 文号年份与成文年份不一致 | 文号年份与成文年份不一致 | 1 | v1.0.0 | active |
| DOC-003 | M1 | 文号 | 多候选文号 | 多候选文号，仅线索 | 0 | v1.0.0 | active |
| META-001 | M1 | 元数据 | 元数据缺失 | 元数据缺失，严格确认 | 1 | v1.0.0 | active |
| ORG-001 | M1 | 机构 | 主发文机关与机关代字不匹配 | 主发文机关与机关代字不匹配（待复核） | 0 | v1.0.0 | active |
| ORG-002 | M1 | 机构 | 公开主体与实际发文机关不同 | 公开主体与实际发文机关不同（提示） | 0 | v1.0.0 | active |
| ORG-003 | M1 | 机构 | 联合发文机关证据不一致 | 联合发文机关证据不一致 | 0 | v1.0.0 | active |
| ORG-004 | M1 | 机构 | 标题主体与实际发文机关冲突 | 标题主体与实际发文机关冲突 | 0 | v1.0.0 | active |
| ORG-005 | M1 | 机构 | 正文落款与实际发文机关冲突 | 正文落款与实际发文机关冲突 | 0 | v1.0.0 | active |

---

## 3. M3 PDF/附件规则（10 条）

> 代码实现：`app/pdf/__init__.py`（PDF-001~003 完整性）、`app/pdf/compare.py`（PDF-004~007 三方比对）、`app/pdf/attachments.py`（ATT-001~003）

| 规则码 | Milestone | Category | 名称 | 描述 | allow_customer_output | 版本 | 状态 |
|--------|-----------|----------|------|------|-----------------------|------|------|
| ATT-001 | M3 | 附件 | 附件缺失 | 附件缺失 | 1 | v1.0.0 | active |
| ATT-002 | M3 | 附件 | 附件错挂 | 附件错挂 | 1 | v1.0.0 | active |
| ATT-003 | M3 | 附件 | 附件名称与内容不匹配 | 附件名称与内容不匹配 | 1 | v1.0.0 | active |
| PDF-001 | M3 | PDF | PDF 损坏 | PDF 损坏 | 0 | v1.0.0 | active |
| PDF-002 | M3 | PDF | PDF 加密无法读取 | PDF 加密无法读取 | 0 | v1.0.0 | active |
| PDF-003 | M3 | PDF | PDF 空白 | PDF 空白 | 0 | v1.0.0 | active |
| PDF-004 | M3 | PDF | 页面文号与 PDF 文号不一致 | 页面文号与 PDF 文号不一致 | 1 | v1.0.0 | active |
| PDF-005 | M3 | PDF | 页面标题与 PDF 标题不一致 | 页面标题与 PDF 标题不一致 | 1 | v1.0.0 | active |
| PDF-006 | M3 | PDF | 页面日期与 PDF 日期不一致 | 页面日期与 PDF 日期不一致 | 1 | v1.0.0 | active |
| PDF-007 | M3 | PDF | 页面机构与 PDF 机构不一致 | 页面机构与 PDF 机构不一致 | 1 | v1.0.0 | active |

---

## 4. M4 归档/重复/对账规则（24 条）

> 代码实现：`app/archive/__init__.py`（ARCH-001~006 evaluate_archive_anomalies）、`app/duplicates/__init__.py`（DUP-001~008 _classify_duplicate）、`app/reconciliation/__init__.py`（RECON-001~010 _classify_match）

### 4.1 异常归档（ARCH-001~006，6 条）

| 规则码 | Milestone | Category | 名称 | 描述 | allow_customer_output | 版本 | 状态 |
|--------|-----------|----------|------|------|-----------------------|------|------|
| ARCH-001 | M4 | 归档 | 现行有效文件连续两轮消失 | 现行有效文件连续两轮消失 | 1 | v1.0.0 | active |
| ARCH-002 | M4 | 归档 | 页面仍更新但栏目归档 | 页面仍更新但栏目归档 | 1 | v1.0.0 | active |
| ARCH-003 | M4 | 归档 | 长期保留栏目异常归档 | 长期保留栏目异常归档 | 1 | v1.0.0 | active |
| ARCH-004 | M4 | 归档 | 市级存在区级长期缺失 | 市级存在区级长期缺失 | 1 | v1.0.0 | active |
| ARCH-005 | M4 | 归档 | 区级存在市级长期缺失 | 区级存在市级长期缺失 | 1 | v1.0.0 | active |
| ARCH-006 | M4 | 归档 | 迁移后原栏目无正确跳转 | 迁移后原栏目无正确跳转 | 1 | v1.0.0 | active |

> 修订说明（R-P0-3）：旧版文档将 ARCH-001 错误描述为"检测政策文件是否在发布后 30 个自然日内完成归档"，且将 ARCH-001~006 全部标记为 `allow_customer_output=0`。DB 实际语义为"现行有效文件连续两轮消失"（代码常量 `MIN_MISSING_ROUNDS_FOR_ARCH_001=2`），且 6 条均进入客户正式问题。本文档已按 DB 修正。

### 4.2 重复检测（DUP-001~008，8 条）

| 规则码 | Milestone | Category | 名称 | 描述 | allow_customer_output | 版本 | 状态 |
|--------|-----------|----------|------|------|-----------------------|------|------|
| DUP-001 | M4 | 重复 | 技术重复 | 技术重复（同 URL 同内容同哈希） | 0 | v1.0.0 | active |
| DUP-002 | M4 | 重复 | 同栏目重复 | 同栏目重复（同栏目内不同 URL 同一文件） | 0 | v1.0.0 | active |
| DUP-003 | M4 | 重复 | 不同 URL 同一政策文件 | 不同 URL 同一政策文件 | 0 | v1.0.0 | active |
| DUP-004 | M4 | 重复 | 跨栏目正常重复 | 跨栏目正常重复（应排除） | 0 | v1.0.0 | active |
| DUP-005 | M4 | 重复 | 跨渠道正常同步 | 跨渠道正常同步（应排除） | 0 | v1.0.0 | active |
| DUP-006 | M4 | 重复 | 同 PDF 不同页面重复 | 同 PDF 不同页面重复 | 0 | v1.0.0 | active |
| DUP-007 | M4 | 重复 | 文号相同内容不同 | 文号相同内容不同 | 1 | v1.0.0 | active |
| DUP-008 | M4 | 重复 | 文号不同内容高度相似 | 文号不同内容高度相似 | 1 | v1.0.0 | active |

### 4.3 市区双渠道对账（RECON-001~010，10 条）

| 规则码 | Milestone | Category | 名称 | 描述 | allow_customer_output | 版本 | 状态 |
|--------|-----------|----------|------|------|-----------------------|------|------|
| RECON-001 | M4 | 对账 | 市区一致 | 市区一致 | 0 | v1.0.0 | active |
| RECON-002 | M4 | 对账 | 市级缺失 | 市级缺失（区级有但市级无） | 1 | v1.0.0 | active |
| RECON-003 | M4 | 对账 | 区级缺失 | 区级缺失（市级有但区级无） | 1 | v1.0.0 | active |
| RECON-004 | M4 | 对账 | 标题不一致 | 标题不一致 | 1 | v1.0.0 | active |
| RECON-005 | M4 | 对账 | 文号不一致 | 文号不一致 | 1 | v1.0.0 | active |
| RECON-006 | M4 | 对账 | 日期不一致 | 日期不一致 | 1 | v1.0.0 | active |
| RECON-007 | M4 | 对账 | 正文不一致 | 正文不一致 | 1 | v1.0.0 | active |
| RECON-008 | M4 | 对账 | 附件不一致 | 附件不一致 | 1 | v1.0.0 | active |
| RECON-009 | M4 | 对账 | 更新延迟 | 更新延迟 | 1 | v1.0.0 | active |
| RECON-010 | M4 | 对账 | 无法匹配 | 无法匹配（数据不足） | 0 | v1.0.0 | active |

---

## 5. M5 阅办/媒体/解读/整改规则（26 条）

> 代码实现：`app/reading_linkage/__init__.py`（READ-001~005 _classify）、`app/media/__init__.py`（MEDIA-001~006 _classify）、`app/interpretation/__init__.py`（INTERP-001~008 _classify_check + create_interpretation）、`app/rectification/__init__.py`（RECT-001~007 create_task、transition_status、detect_overdue_tasks）

### 5.1 阅办联动（READ-001~005，5 条）

| 规则码 | Milestone | Category | 名称 | 描述 | allow_customer_output | 版本 | 状态 |
|--------|-----------|----------|------|------|-----------------------|------|------|
| READ-001 | M5 | 阅办联动 | 阅办按钮不存在 | 阅办按钮不存在 | 1 | v1.0.0 | active |
| READ-002 | M5 | 阅办联动 | 阅办按钮存在但不可点击 | 阅办按钮存在但不可点击 | 1 | v1.0.0 | active |
| READ-003 | M5 | 阅办联动 | 阅办跳转失败 | 阅办跳转失败（HTTP 错误或页面失效） | 1 | v1.0.0 | active |
| READ-004 | M5 | 阅办联动 | 阅办跳转成功但页面空白或错误 | 阅办跳转成功但页面空白或错误 | 1 | v1.0.0 | active |
| READ-005 | M5 | 阅办联动 | 阅办页面返回 200 但显示暂无数据 | 阅办页面返回 200 但显示暂无数据 | 1 | v1.0.0 | active |

### 5.2 媒体可用性（MEDIA-001~006，6 条）

| 规则码 | Milestone | Category | 名称 | 描述 | allow_customer_output | 版本 | 状态 |
|--------|-----------|----------|------|------|-----------------------|------|------|
| MEDIA-001 | M5 | 媒体 | 图片缺失 | 图片缺失 | 1 | v1.0.0 | active |
| MEDIA-002 | M5 | 媒体 | 图片破损 | 图片破损 | 1 | v1.0.0 | active |
| MEDIA-003 | M5 | 媒体 | 视频缺失 | 视频缺失 | 1 | v1.0.0 | active |
| MEDIA-004 | M5 | 媒体 | 视频失效或无法播放 | 视频失效或无法播放 | 1 | v1.0.0 | active |
| MEDIA-005 | M5 | 媒体 | 页面资源整体为空 | 页面资源整体为空 | 1 | v1.0.0 | active |
| MEDIA-006 | M5 | 媒体 | 图片/视频被 403/429 拦截 | 图片/视频被 403/429 拦截 | 1 | v1.0.0 | active |

### 5.3 政策解读（INTERP-001~008，8 条）

| 规则码 | Milestone | Category | 名称 | 描述 | allow_customer_output | 版本 | 状态 |
|--------|-----------|----------|------|------|-----------------------|------|------|
| INTERP-001 | M5 | 解读 | 解读链接失效 | 解读链接失效 | 1 | v1.0.0 | active |
| INTERP-002 | M5 | 解读 | 解读对象错误 | 解读对象错误（与原政策不匹配） | 1 | v1.0.0 | active |
| INTERP-003 | M5 | 解读 | 解读发布时间缺失 | 解读发布时间缺失 | 1 | v1.0.0 | active |
| INTERP-004 | M5 | 解读 | 解读链接错挂 | 解读链接错挂 | 1 | v1.0.0 | active |
| INTERP-005 | M5 | 解读 | 解读图片失效 | 解读图片失效 | 1 | v1.0.0 | active |
| INTERP-006 | M5 | 解读 | 解读视频失效 | 解读视频失效 | 1 | v1.0.0 | active |
| INTERP-007 | M5 | 解读 | 解读页面空白 | 解读页面空白 | 1 | v1.0.0 | active |
| INTERP-008 | M5 | 解读 | 解读类型缺失或无法识别 | 解读类型缺失或无法识别 | 1 | v1.0.0 | active |

### 5.4 整改闭环（RECT-001~007，7 条）

| 规则码 | Milestone | Category | 名称 | 描述 | allow_customer_output | 版本 | 状态 |
|--------|-----------|----------|------|------|-----------------------|------|------|
| RECT-001 | M5 | 整改 | 整改任务未指定责任单位 | 整改任务未指定责任单位 | 1 | v1.0.0 | active |
| RECT-002 | M5 | 整改 | 整改任务超期 | 整改任务超期 | 1 | v1.0.0 | active |
| RECT-003 | M5 | 整改 | 整改后证据缺失 | 整改后证据缺失 | 1 | v1.0.0 | active |
| RECT-004 | M5 | 整改 | 整改未通过但未重新下发 | 整改未通过但未重新下发 | 1 | v1.0.0 | active |
| RECT-005 | M5 | 整改 | 销号后复发 | 销号后复发 | 1 | v1.0.0 | active |
| RECT-006 | M5 | 整改 | 整改状态历史缺失 | 整改状态历史缺失 | 1 | v1.0.0 | active |
| RECT-007 | M5 | 整改 | 整改期限缺失 | 整改期限缺失 | 1 | v1.0.0 | active |

---

## 6. 规则统计汇总

### 6.1 按 Milestone 分布

| Milestone | 规则数 | allow_customer_output=1 | allow_customer_output=0 |
|-----------|--------|--------------------------|--------------------------|
| M1 | 14 | 5 | 9 |
| M3 | 10 | 7 | 3 |
| M4 | 24 | 16 | 8 |
| M5 | 26 | 26 | 0 |
| 合计 | 74 | 54 | 20 |

### 6.2 按 Category 分布

| Category | 规则数 |
|----------|--------|
| 机构代字 | 3 |
| 日期 | 2 |
| 文号 | 3 |
| 元数据 | 1 |
| 机构 | 5 |
| 附件 | 3 |
| PDF | 7 |
| 归档 | 6 |
| 重复 | 8 |
| 对账 | 10 |
| 阅办联动 | 5 |
| 媒体 | 6 |
| 解读 | 8 |
| 整改 | 7 |
| 合计 | 74 |

### 6.3 按版本与状态

| 字段 | 取值 | 数量 |
|------|------|------|
| 版本 | v1.0.0 | 74 |
| 状态 | active | 74 |

### 6.4 客户输出边界汇总（allow_customer_output=0，共 20 条）

| Milestone | 规则码 | 数量 |
|-----------|--------|------|
| M1 | AGENCY-001、AGENCY-002、AGENCY-003、DOC-003、ORG-001、ORG-002、ORG-003、ORG-004、ORG-005 | 9 |
| M3 | PDF-001、PDF-002、PDF-003 | 3 |
| M4 | DUP-001、DUP-002、DUP-003、DUP-004、DUP-005、DUP-006、RECON-001、RECON-010 | 8 |
| M5 | — | 0 |
| 合计 | — | 20 |

进入客户正式问题清单的规则（allow_customer_output=1）共 54 条。

---

## 7. 修订记录（R-P0-3）

本次修订（V3.0 修订版，2026-07-28）修复独立总验发现的文档与 DB 漂移问题：

1. **删除不存在的规则码**：移除 LINK-001/002、SYMBOL-001/002、ATTACH-001/002/003、DATE-SEMANTIC-001/002、OCR-001/002、TITLE-001、CONTENT-001、ATTACH-REF-001、POLICY-NUM-001、DATE-003 等 DB 中不存在的规则码。
2. **补齐 DB 实际规则码**：补入 AGENCY-001/002/003、DOC-001/002/003、ORG-001~005、ATT-001~003、PDF-001~007 等 DB 实际存在但旧文档缺失或误标的规则码。
3. **修正 ARCH-001 语义**：由错误的"30 日内归档"更正为 DB 实际语义"现行有效文件连续两轮消失"（代码 `MIN_MISSING_ROUNDS_FOR_ARCH_001=2`）。
4. **修正客户输出边界**：ARCH-001~006 由 `allow_customer_output=0` 更正为 `1`；DUP-007/008、RECON-002~009 由 `0` 更正为 `1`。实际 `allow_customer_output=0` 共 20 条、`=1` 共 54 条。
5. **描述字段对齐 DB**：所有规则描述与 `rule_versions.description` 逐字一致。

本文档为主开发 AI 自验产出，待独立总验收。
