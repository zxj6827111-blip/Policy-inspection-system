# M5 开发报告：阅办联动、政策解读与整改闭环

> 里程碑：M5
> 分支：`feat/full-policy-inspection-upgrade`
> 方案依据：`docs/program_control/full_upgrade_plan_v3.md` §十一
> 完成日期：2026-07-27
> 主开发 AI：连续推进，自验 PASS

---

## 一、基线记录

| 项 | 值 |
|---|---|
| 开始分支 | `feat/full-policy-inspection-upgrade` |
| 开始 HEAD | `35d51f4`（M4 收尾提交） |
| 生产数据库 SHA256 | `13e6a8a412f69c13d149674a31076854c966f5b151e670fc9e22d0f53ab77af3`（未变） |
| M4 全量回归基线 | 300 passed |

---

## 二、范围与交付

M5 落实 V3.0 §十一的业务目标，覆盖阅办联动、附件与媒体、政策解读、整改闭环四大主题，共实现 **26 条规则**（READ ×5、MEDIA ×6、INTERP ×8、RECT ×7），全部带 `rule_version=1.0.0`，可创建整改任务、可追溯、可销号。

### 2.1 阅办联动深度检查（READ-001 ~ READ-005）

- 检查按钮存在、可点击、跳转成功、最终页面有效、不是空白或错误、不是只返回 200 但显示「暂无数据」。
- **关键门禁**：
  - 正常重定向不误报（`test_redirect_is_not_misflagged`）；
  - 403 / 429 不进入正式问题（`test_403_does_not_enter_confirmed_problem`、`test_429_does_not_enter_confirmed_problem`），机器状态为 `transient_error`，`allow_customer_output=false`。
- 模块：[app/reading_linkage/__init__.py](file:///e:/Software%20Development/Policy%20inspection%20system/app/reading_linkage/__init__.py)

### 2.2 图片 / 视频 / 附件可用性检查（MEDIA-001 ~ MEDIA-006）

- 区分 `image` / `video` / `attachment` 三类，识别缺失、破损、失效、无法播放、整体为空、403/429 拦截。
- 403/429 为 `transient_error`，不进入客户正式问题。
- **关键门禁**：视频页面打开但视频失效可识别（`test_media_004_video_playback_failed`）。
- 模块：[app/media/__init__.py](file:///e:/Software%20Development/Policy%20inspection%20system/app/media/__init__.py)

### 2.3 多类型政策解读检查（INTERP-001 ~ INTERP-008）

- 支持解读类型：图文 / 一图读懂 / 视频 / 问答 / 负责人 / 专家 / 新闻发布会 / 媒体 / 未知。
- 检查链接有效、图片有效、视频可播放、页面不为空、解读对象与原政策一致、解读发布时间、解读链接是否错挂。
- **解读对象匹配**：基于规范化文号 + 规范化标题计算匹配置信度（`MATCH_CONFIDENCE_THRESHOLD`），不匹配时 `has_interpretation_mismatch=1`。
- **关键门禁**：解读对象错误可识别（`test_interpretation_doc_number_mismatch`）。
- 模块：[app/interpretation/__init__.py](file:///e:/Software%20Development/Policy%20inspection%20system/app/interpretation/__init__.py)

### 2.4 整改闭环（RECT-001 ~ RECT-007）

- 状态机：`created → assigned → notified → feedback_uploaded → rechecking → passed → signoff`；失败 / 复发回路 `rechecking → failed → reopened → assigned`。
- 整改前后证据、状态历史、责任单位 / 责任人、整改期限、超期提醒、复发识别。
- **关键门禁**：
  - 整改前后证据完整（`test_rect_003_after_evidence_missing`）；
  - 状态可追溯（`test_get_task_with_history`）；
  - 销号后复发可识别（`test_rect_005_recurrence_after_signoff`，emit RECT-005 并 `recurrence_count + 1`）；
  - 超期检测（`test_rect_002_overdue_detection`）。
- 模块：[app/rectification/__init__.py](file:///e:/Software%20Development/Policy%20inspection%20system/app/rectification/__init__.py)

---

## 三、数据库迁移

### 3.1 迁移脚本

- [app/migrations/m5.py](file:///e:/Software%20Development/Policy%20inspection%20system/app/migrations/m5.py)
- 注册入口：[app/db.py](file:///e:/Software%20Development/Policy%20inspection%20system/app/db.py) `_migrate_m5`

### 3.2 新增表（7 张）

| 表 | 用途 |
|---|---|
| `reading_linkage_checks` | 阅办按钮 / 跳转 / 页面有效性检查 |
| `media_checks` | 图片 / 视频 / 附件可用性检查 |
| `policy_interpretations` | 政策解读记录与对象匹配 |
| `interpretation_checks` | 解读链接 / 图片 / 视频检查 |
| `rectification_tasks` | 整改任务主体 |
| `rectification_status_history` | 整改状态流转历史 |

### 3.3 policy_documents 扩展列（4 列）

| 列 | 用途 |
|---|---|
| `reading_check_status` | 文档级阅办检查状态 |
| `interpretation_count` | 解读数量 |
| `has_interpretation_mismatch` | 是否存在解读对象不匹配 |
| `rectification_status` | 文档级整改状态 |

### 3.4 规则版本种子

`rule_versions` 表注入 26 条 M5 规则（READ ×5、MEDIA ×6、INTERP ×8、RECT ×7），全部 `version=1.0.0`、`enabled=1`。

### 3.5 迁移原则遵守

- 幂等：`test_migration_is_idempotent` 验证重复执行不重复插入；
- 不删除旧字段；
- 人工复核只追加；
- 所有 finding 带 rule_version；
- 仅在临时数据库副本（pytest `tmp_path`）验证，未触碰生产库。

---

## 四、API

在 [app/main.py](file:///e:/Software%20Development/Policy%20inspection%20system/app/main.py) 新增 M5 端点（全部受 `jobs:write` 或登录鉴权保护，并写入 `operation_audits`）：

| 端点 | 方法 | 鉴权 | 用途 |
|---|---|---|---|
| `/api/reading-checks/{doc_id}` | POST | `jobs:write` | 记录阅办联动检查 |
| `/api/media-checks/{doc_id}` | POST | `jobs:write` | 记录媒体可用性检查 |
| `/api/interpretations/{doc_id}` | POST | `jobs:write` | 创建政策解读 |
| `/api/interpretation-checks/{interp_id}` | POST | `jobs:write` | 记录解读检查 |
| `/api/rectification/tasks` | POST | `jobs:write` | 创建整改任务 |
| `/api/rectification/tasks/{task_id}/transition` | POST | `jobs:write` | 状态流转 |
| `/api/rectification/tasks/{task_id}` | GET | 登录 | 查询任务（含状态历史） |
| `/api/rectification/overdue` | GET | 登录 | 超期任务列表 |
| `/api/rectification/recurrence` | GET | 登录 | 复发任务列表 |

测试覆盖：`TestM5API` 验证未授权访问全部返回 401/403。

---

## 五、UI

M5 的 UI 模块在 M6「最终 UI」统一落地。M5 阶段已完成：
- 数据模型与 API 就绪；
- 证据 JSON 结构与 M6 证据弹窗动态显示字段对齐（页面公开主体 / 上传单位 / 实际发文机关 / 文号机关代字 / 页面字段 / 正文证据 / PDF 页码 / OCR 置信度 / 链接轨迹 / 规则版本 / 机器状态 / 人工状态 / 排除原因 / 复核历史）。

---

## 六、测试

### 6.1 测试文件

- [tests/test_m5_reading_interp_rectification.py](file:///e:/Software%20Development/Policy%20inspection%20system/tests/test_m5_reading_interp_rectification.py)

### 6.2 测试结果

| 类别 | 用例数 | 结果 |
|---|---:|---|
| Migration（幂等 / 列 / 规则种子） | 4 | PASS |
| ReadingLinkage（READ-001~005 + 403/429 + 重定向 + OK） | 11 | PASS |
| MediaChecks（MEDIA-001~006 + OK） | 7 | PASS |
| Interpretation（匹配 + INTERP-001/003/005/006/007 + OK） | 9 | PASS |
| Rectification（RECT-001~007 + 状态机 + 历史） | 11 | PASS |
| M5API（鉴权边界） | 5 | PASS |
| **合计** | **47** | **PASS** |

### 6.3 全量回归

```
347 passed, 1 warning in 535.22s (0:08:55)
```

- M4 基线 300 + M5 新增 47 = 347，无回归、无失败。

---

## 七、门禁对照（V3.0 §11.5）

| 门禁 | 实现 | 验证用例 | 状态 |
|---|---|---|---|
| 正常重定向不误报 | `page_status=redirect` 不触发 READ-003 | `test_redirect_is_not_misflagged` | PASS |
| 403/429 不进入正式问题 | `transient_error` + `allow_customer_output=false` | `test_403_does_not_enter_confirmed_problem`、`test_429_does_not_enter_confirmed_problem` | PASS |
| 视频页面打开但视频失效可识别 | MEDIA-004 `playback_failed` | `test_media_004_video_playback_failed` | PASS |
| 200 空页面可识别 | READ-004 / READ-005 | `test_read_004_blank_page_after_jump`、`test_read_005_no_data_placeholder` | PASS |
| 解读对象错误可识别 | `has_interpretation_mismatch=1` | `test_interpretation_doc_number_mismatch` | PASS |
| 所有正式问题可创建整改任务 | `create_task` 接受任意 finding_id | `test_create_task_no_issues` | PASS |
| 整改前后证据完整 | `before_evidence_json` / `after_evidence_json` + RECT-003 | `test_rect_003_after_evidence_missing` | PASS |
| 状态可追溯 | `rectification_status_history` + RECT-006 | `test_get_task_with_history`、`test_rect_006_status_history_missing` | PASS |
| 销号后复发可识别 | signoff → reopened emit RECT-005 + `recurrence_count+1` | `test_rect_005_recurrence_after_signoff` | PASS |

---

## 八、生产边界

| 边界 | 状态 |
|---|---|
| 修改生产数据库 | 否 |
| 重启生产服务 | 否 |
| 修改生产 Nginx/PM2/证书 | 否 |
| 高频外部扫描 | 否 |
| 数据库迁移 | 仅在 pytest `tmp_path` 副本 |
| `production_database_modified` | `false` |
| `production_service_restarted` | `false` |

---

## 九、风险与遗留

- 无 P0/P1 阻断；
- UI 落地推迟到 M6（按 V3.0 计划）；
- 真实浏览器阅办跳转验证依赖 M6 Playwright（R-008 持续监控）。

---

## 十、结论

M5 开发完成，自验 PASS，可进入 M6。
