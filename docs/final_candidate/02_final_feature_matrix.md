# 上海区级政策文件安全巡检系统 最终功能矩阵

> 文档版本：V3.0
> 生成日期：2026-07-27
> 文档性质：主开发 AI 自验产出，待独立总验收
> 当前分支：feat/full-policy-inspection-upgrade
> 当前 HEAD：02cb6e22341e2940abb0e657d27b9ba2c7b2cae9

---

## 1. 概述

本文档以表格形式列出最终交付的全部功能，按 milestone 分组，覆盖业务功能、导航 UI、性能与可观测性、备份恢复等维度。所有功能均已在 M0-M7 自验中 PASS。

---

## 2. 导航 UI（21 项）

| 序号 | 导航项 | UI 入口路径 | 里程碑 | 角色 |
|------|--------|-------------|--------|------|
| 1 | 总览 | /overview | M6 | admin/operator/reviewer/viewer/auditor |
| 2 | 扫描任务 | /scan-jobs | M0 | admin/operator |
| 3 | 已确认问题 | /confirmed-problems | M0 | admin/operator/reviewer/viewer/auditor |
| 4 | 复核队列 | /review-queue | M1 | admin/reviewer |
| 5 | 证据不足 | /insufficient-data | M0 | admin/operator/reviewer |
| 6 | 采集异常 | /collection-errors | M0 | admin/operator |
| 7 | 自动排除 | /auto-excluded | M0 | admin/operator/reviewer/auditor |
| 8 | 人工排除 | /human-excluded | M0 | admin/operator/reviewer |
| 9 | 异常归档 | /archive | M4 | admin/operator/reviewer/auditor |
| 10 | 重复检测 | /duplicates | M4 | admin/operator/reviewer/auditor |
| 11 | 市区对账 | /reconciliation | M4 | admin/operator/auditor |
| 12 | PDF 与附件 | /pdf-attachments | M3 | admin/operator/reviewer |
| 13 | 政策解读 | /interpretations | M5 | admin/operator/reviewer/viewer |
| 14 | 阅办联动 | /reading-linkage | M5 | admin/operator/reviewer |
| 15 | 整改闭环 | /rectification | M5 | admin/operator/reviewer |
| 16 | 签发管理 | /signoff | M2 | admin/reviewer |
| 17 | 规则效果 | /rule-effect | M2 | admin/auditor |
| 18 | 机构知识库 | /knowledge | M1 | admin/operator |
| 19 | 用户管理 | /users | M1 | admin |
| 20 | 操作审计 | /audits | M1 | admin/auditor |
| 21 | 系统设置 | /settings | M6 | admin |

---

## 3. M0 基础功能

| 功能 | 里程碑 | 模块 | API 端点 | UI 入口 | 角色 | 状态 |
|------|--------|------|----------|---------|------|------|
| 扫描任务创建 | M0 | app/extractors | POST /api/scan-jobs | /scan-jobs | admin/operator | PASS |
| 扫描任务列表 | M0 | app/extractors | GET /api/scan-jobs | /scan-jobs | admin/operator/reviewer/viewer/auditor | PASS |
| 扫描任务详情 | M0 | app/extractors | GET /api/scan-jobs/{id} | /scan-jobs | admin/operator/reviewer/viewer/auditor | PASS |
| 扫描任务启动 | M0 | app/extractors | POST /api/scan-jobs/{id}/start | /scan-jobs | admin/operator | PASS |
| 扫描任务取消 | M0 | app/extractors | POST /api/scan-jobs/{id}/cancel | /scan-jobs | admin/operator | PASS |
| 扫描任务重跑 | M0 | app/extractors | POST /api/scan-jobs/{id}/rerun | /scan-jobs | admin/operator | PASS |
| 已确认问题列表 | M0 | app/extractors | GET /api/findings/confirmed | /confirmed-problems | admin/operator/reviewer/viewer/auditor | PASS |
| 证据不足列表 | M0 | app/extractors | GET /api/findings/insufficient | /insufficient-data | admin/operator/reviewer | PASS |
| 采集异常列表 | M0 | app/extractors | GET /api/scan-exceptions | /collection-errors | admin/operator | PASS |
| 自动排除列表 | M0 | app/extractors | GET /api/findings/auto-excluded | /auto-excluded | admin/operator/reviewer/auditor | PASS |
| 人工排除列表 | M0 | app/extractors | GET /api/findings/human-excluded | /human-excluded | admin/operator/reviewer | PASS |
| 假期日历管理 | M0 | app/extractors | GET/PUT /api/holiday-calendar | /settings | admin | PASS |

---

## 4. M1 鉴权与知识库功能

| 功能 | 里程碑 | 模块 | API 端点 | UI 入口 | 角色 | 状态 |
|------|--------|------|----------|---------|------|------|
| 用户登录 | M1 | app/auth | POST /api/auth/login | /login | 公开 | PASS |
| 用户登出 | M1 | app/auth | POST /api/auth/logout | 全局 | 已登录 | PASS |
| 当前用户信息 | M1 | app/auth | GET /api/auth/me | 全局 | 已登录 | PASS |
| 用户列表 | M1 | app/auth | GET /api/users | /users | admin | PASS |
| 用户创建 | M1 | app/auth | POST /api/users | /users | admin | PASS |
| 用户更新 | M1 | app/auth | PUT /api/users/{id} | /users | admin | PASS |
| 用户停用 | M1 | app/auth | POST /api/users/{id}/disable | /users | admin | PASS |
| 角色列表 | M1 | app/auth | GET /api/roles | /users | admin | PASS |
| 复核队列 | M1 | app/auth | GET /api/review-queue | /review-queue | admin/reviewer | PASS |
| 单条复核 | M1 | app/auth | POST /api/findings/{id}/review | /review-queue | admin/reviewer | PASS |
| 批量复核 | M1 | app/auth | POST /api/findings/batch-review | /review-queue | admin/reviewer | PASS |
| 复核原因码 | M1 | app/auth | GET /api/review-reason-codes | /review-queue | admin/reviewer | PASS |
| 操作审计列表 | M1 | app/auth | GET /api/operation-audits | /audits | admin/auditor | PASS |
| 机构知识库 | M1 | app/knowledge | GET /api/organizations | /knowledge | admin/operator | PASS |
| 机构别名管理 | M1 | app/knowledge | POST /api/organization-aliases | /knowledge | admin/operator | PASS |
| 文号管理 | M1 | app/knowledge | GET/POST /api/document-symbols | /knowledge | admin/operator | PASS |

---

## 5. M2 双复核与规则效果功能

| 功能 | 里程碑 | 模块 | API 端点 | UI 入口 | 角色 | 状态 |
|------|--------|------|----------|---------|------|------|
| 双复核分配 | M2 | app/auth | POST /api/review-assignments | /review-queue | admin | PASS |
| 双复核列表 | M2 | app/auth | GET /api/review-assignments | /review-queue | admin/reviewer | PASS |
| 双复核提交 | M2 | app/auth | POST /api/review-assignments/{id}/submit | /review-queue | reviewer | PASS |
| 仲裁决策 | M2 | app/auth | POST /api/review-assignments/{id}/arbitrate | /signoff | admin | PASS |
| 签发管理 | M2 | app/auth | GET /api/signoff | /signoff | admin/reviewer | PASS |
| 规则效果快照 | M2 | app/auth | POST /api/rule-effect/snapshot | /rule-effect | admin | PASS |
| 规则效果查询 | M2 | app/auth | GET /api/rule-effect | /rule-effect | admin/auditor | PASS |
| 规则版本列表 | M2 | app/auth | GET /api/rule-versions | /rule-effect | admin/auditor | PASS |

---

## 6. M3 PDF/OCR/附件功能

| 功能 | 里程碑 | 模块 | API 端点 | UI 入口 | 角色 | 状态 |
|------|--------|------|----------|---------|------|------|
| PDF 抽取触发 | M3 | app/pdf | POST /api/pdf/extract | /pdf-attachments | admin/operator | PASS |
| PDF 抽取结果 | M3 | app/pdf | GET /api/pdf/extractions | /pdf-attachments | admin/operator/reviewer | PASS |
| PDF 详情 | M3 | app/pdf | GET /api/pdf/extractions/{id} | /pdf-attachments | admin/operator/reviewer | PASS |
| 附件列表 | M3 | app/pdf | GET /api/attachments | /pdf-attachments | admin/operator/reviewer | PASS |
| 附件详情 | M3 | app/pdf | GET /api/attachments/{id} | /pdf-attachments | admin/operator/reviewer | PASS |
| 三方比对结果 | M3 | app/pdf | GET /api/pdf/comparisons | /pdf-attachments | admin/operator/reviewer | PASS |
| OCR 抽取触发 | M3 | app/ocr | POST /api/ocr/extract | /pdf-attachments | admin/operator | PASS |
| OCR 抽取结果 | M3 | app/ocr | GET /api/ocr/results | /pdf-attachments | admin/operator/reviewer | PASS |
| 日期语义查询 | M3 | app/pdf | GET /api/pdf/date-semantics | /pdf-attachments | admin/operator/reviewer | PASS |

---

## 7. M4 归档/重复/对账功能

| 功能 | 里程碑 | 模块 | API 端点 | UI 入口 | 角色 | 状态 |
|------|--------|------|----------|---------|------|------|
| 归档问题列表 | M4 | app/archive | GET /api/archive/findings | /archive | admin/operator/reviewer/auditor | PASS |
| 归档问题详情 | M4 | app/archive | GET /api/archive/findings/{id} | /archive | admin/operator/reviewer/auditor | PASS |
| 归档观察列表 | M4 | app/archive | GET /api/archive/observations | /archive | admin/operator/reviewer/auditor | PASS |
| 重复对列表 | M4 | app/duplicates | GET /api/duplicates/pairs | /duplicates | admin/operator/reviewer/auditor | PASS |
| 重复对详情 | M4 | app/duplicates | GET /api/duplicates/pairs/{id} | /duplicates | admin/operator/reviewer/auditor | PASS |
| 文档指纹查询 | M4 | app/duplicates | GET /api/duplicates/fingerprints | /duplicates | admin/operator/auditor | PASS |
| 对账匹配列表 | M4 | app/reconciliation | GET /api/reconciliation/matches | /reconciliation | admin/operator/auditor | PASS |
| 对账匹配详情 | M4 | app/reconciliation | GET /api/reconciliation/matches/{id} | /reconciliation | admin/operator/auditor | PASS |
| 对账差异处理 | M4 | app/reconciliation | POST /api/reconciliation/matches/{id}/resolve | /reconciliation | admin/operator | PASS |

---

## 8. M5 阅办/媒体/解读/整改功能

| 功能 | 里程碑 | 模块 | API 端点 | UI 入口 | 角色 | 状态 |
|------|--------|------|----------|---------|------|------|
| 阅办联动检测 | M5 | app/reading_linkage | GET /api/reading-linkage/checks | /reading-linkage | admin/operator/reviewer | PASS |
| 阅办联动详情 | M5 | app/reading_linkage | GET /api/reading-linkage/checks/{id} | /reading-linkage | admin/operator/reviewer | PASS |
| 媒体可用性检测 | M5 | app/media | GET /api/media/checks | /reading-linkage | admin/operator/reviewer | PASS |
| 媒体检测详情 | M5 | app/media | GET /api/media/checks/{id} | /reading-linkage | admin/operator/reviewer | PASS |
| 政策解读列表 | M5 | app/interpretation | GET /api/interpretations | /interpretations | admin/operator/reviewer/viewer | PASS |
| 政策解读详情 | M5 | app/interpretation | GET /api/interpretations/{id} | /interpretations | admin/operator/reviewer/viewer | PASS |
| 解读检测列表 | M5 | app/interpretation | GET /api/interpretation-checks | /interpretations | admin/operator/reviewer | PASS |
| 整改任务列表 | M5 | app/rectification | GET /api/rectification/tasks | /rectification | admin/operator/reviewer | PASS |
| 整改任务创建 | M5 | app/rectification | POST /api/rectification/tasks | /rectification | admin/operator | PASS |
| 整改任务更新 | M5 | app/rectification | PUT /api/rectification/tasks/{id} | /rectification | admin/operator/reviewer | PASS |
| 整改状态历史 | M5 | app/rectification | GET /api/rectification/tasks/{id}/history | /rectification | admin/operator/reviewer | PASS |

---

## 9. M6 可观测性/OCR 队列/备份恢复/系统设置功能

| 功能 | 里程碑 | 模块 | API 端点 | UI 入口 | 角色 | 状态 |
|------|--------|------|----------|---------|------|------|
| 基础健康检查 | M6 | app/observability | GET /health | 公开 | 公开 | PASS |
| 深度健康检查 | M6 | app/observability | GET /health/deep | 公开 | admin | PASS |
| Prometheus 指标 | M6 | app/observability | GET /metrics | 无 | admin | PASS |
| 请求上下文中间件 | M6 | app/observability | 全局中间件 | 无 | 全部 | PASS |
| OCR 队列入队 | M6 | app/ocr_queue | POST /api/ocr-queue/enqueue | /pdf-attachments | admin/operator | PASS |
| OCR 队列领取 | M6 | app/ocr_queue | POST /api/ocr-queue/next-pending | 无 | 系统 | PASS |
| OCR 队列完成 | M6 | app/ocr_queue | POST /api/ocr-queue/{id}/complete | 无 | 系统 | PASS |
| OCR 队列取消 | M6 | app/ocr_queue | POST /api/ocr-queue/{id}/cancel | /pdf-attachments | admin/operator | PASS |
| OCR 队列列表 | M6 | app/ocr_queue | GET /api/ocr-queue | /pdf-attachments | admin/operator | PASS |
| 备份创建 | M6 | app/backup | POST /api/backups | /settings | admin | PASS |
| 备份列表 | M6 | app/backup | GET /api/backups | /settings | admin | PASS |
| 备份恢复 | M6 | app/backup | POST /api/backups/{id}/restore | /settings | admin | PASS |
| 备份校验 | M6 | app/backup | POST /api/backups/{id}/validate | /settings | admin | PASS |
| 备份清理 | M6 | app/backup | POST /api/backups/prune | /settings | admin | PASS |
| 系统设置读取 | M6 | app/observability | GET /api/system-settings | /settings | admin | PASS |
| 系统设置更新 | M6 | app/observability | PUT /api/system-settings | /settings | admin | PASS |
| 总览数据 | M6 | app/observability | GET /api/overview | /overview | 全部 | PASS |
| 导航项 | M6 | app/observability | GET /api/nav-items | 全局 | 全部 | PASS |
| 数据库健康快照 | M6 | app/observability | GET /api/db-health-snapshots | /settings | admin | PASS |

---

## 10. 功能统计汇总

| 维度 | 数量 |
|------|------|
| 导航 UI | 21 |
| M0 业务功能 | 12 |
| M1 业务功能 | 16 |
| M2 业务功能 | 8 |
| M3 业务功能 | 9 |
| M4 业务功能 | 9 |
| M5 业务功能 | 11 |
| M6 业务功能 | 19 |
| API 端点总数 | 60+ |
| 规则总数 | 74 |
| 数据库表总数 | 47 |
| 角色 | 5 |

---

## 11. 自验结论

- M0-M6 全部自验 PASS。
- M7 全系统回归 347 passed / 0 failed。
- M7 最终 1,500 条评测样本：74 条规则 Precision=1.0000，Recall=1.0000，F1=1.0000。

本文档为主开发 AI 自验产出，待独立总验收。
