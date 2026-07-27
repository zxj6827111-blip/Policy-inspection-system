# 上海区级政策文件安全巡检系统 独立总验收入口指南

| 版本 | 生成日期 | 文档状态 | 适用阶段 |
|----------|------------|----------------------|------------|
| V3.0     | 2026-07-28 | 主开发 AI 自验产出（含 P0/P1 整改），待独立总验收 | 独立总验收 |

> 本文档由主开发 AI 在完成 M0-M7 全部自验、全量回归、1,500 条评测及生产副本演练后生成，作为独立总验收 AI / 验收人员执行最终验收的统一入口。
> 在独立总验收 AI 按本指南完成全部验收步骤并出具 PASS 结论之前，任何人不得声称"最终封板完成"。

---

## 1. 执行摘要

本指南面向独立总验收 AI 与验收人员，说明如何在主开发 AI 完成 M0-M7 全部自验后，独立执行最终验收流程。本指南不依赖主开发 AI 的任何中间输出，所有验收命令均由验收人员独立运行，所有结论均由验收人员基于一手证据给出。

本指南覆盖以下内容：

- 验收前置条件核对
- 验收入口路径与文件清单
- 12 步顺序化验收步骤
- 验收门禁清单（11 项必须全部 PASS）
- 验收结论模板（PASS / PARTIAL / FAIL）
- 验收注意事项与记录要求

适用对象：

- 独立总验收 AI（第二个 AI 验收角色）
- 项目验收人员
- 运维交接负责人

前置基线（由主开发 AI 自验产出，验收人员需独立复核）：

| 项目 | 基线值 |
|------|--------|
| 当前分支 | feat/full-policy-inspection-upgrade |
| 当前 HEAD | （以 `git rev-parse HEAD` 为准；整改入库后须与 status.json remediation head 一致，工作区干净） |
| 生产分支 | codex/fix-snapshot-result-collision |
| 生产 HEAD | be74df72598ae9ae0feb3a8fd9987c84c5bd8b67 |
| 工作区数据库 SHA256 | 本地副本可变（gitignored）；记录验收当时 `Get-FileHash` 即可 |
| 生产数据库 SHA256（未变） | 13E6A8A412F69C13D149674A31076854C966F5B151E670FC9E22D0F53AB77AF3 |
| 全量回归 | 391 passed / 0 failed（含 P0/P1 整改与增补测试） |
| 1,500 条评测 | 引擎=生产规则模块，Precision=1.0000，Recall=1.0000，F1=1.0000 |
| M6-8 生产副本演练 | 10/10 PASS |
| 规则目录 | 74 条规则 |

---

## 2. 验收前置条件

在执行本指南前，验收人员需确认以下前置条件全部满足：

### 2.1 主开发 AI 自验完成度

- M0-M7 全部 milestone 自验 PASS
- M0-M7 自验摘要文件齐全：`tmp/program_control/M0_self_acceptance.json` 至 `tmp/program_control/M7_self_acceptance.json`
- 全量回归测试输出齐全：`tmp/program_control/Mx_test_output.txt`
- M7 最终 1,500 条评测报告齐全：`tmp/program_control/M7_1500_evaluation.json` 与 `tmp/program_control/M7_1500_evaluation.md`
- JUnit XML 输出：`tmp/program_control/M7_junit.xml`
- M6-8 生产副本演练报告：`tmp/program_control/m6_rehearsal/`

### 2.2 交付物就位

最终交付物清单（共 10 个文档 + 若干脚本与控制文件）：

| 类别 | 路径 | 说明 |
|------|------|------|
| 架构 | docs/final_candidate/01_final_architecture.md | 最终架构文档 |
| 功能矩阵 | docs/final_candidate/02_final_feature_matrix.md | 最终功能矩阵 |
| 规则目录 | docs/final_candidate/03_final_rule_catalog.md | 74 条规则目录 |
| 安全报告 | docs/final_candidate/04_final_security_report.md | 最终安全报告 |
| 数据质量 | docs/final_candidate/05_final_data_quality_report.md | 最终数据质量报告 |
| 测试报告 | docs/final_candidate/06_final_test_report.md | 最终测试报告 |
| 迁移报告 | docs/final_candidate/07_final_migration_report.md | 最终迁移报告 |
| 切换 Runbook | docs/final_candidate/08_production_cutover_runbook.md | 生产切换操作手册 |
| 回滚 Runbook | docs/final_candidate/09_final_rollback_runbook.md | 回滚操作手册 |
| 验收指南 | docs/final_candidate/10_independent_acceptance_guide.md | 本文档 |
| 主开发摘要 | tmp/program_control/final_development_summary.json | 主开发 AI 最终摘要 |
| M7 自验 | tmp/program_control/M7_self_acceptance.json | M7 自验摘要 |
| M7 测试输出 | tmp/program_control/M7_test_output.txt | M7 测试原始输出 |
| M7 评测 JSON | tmp/program_control/M7_1500_evaluation.json | 1,500 条评测 JSON |
| M7 评测 MD | tmp/program_control/M7_1500_evaluation.md | 1,500 条评测 Markdown |
| M7 JUnit | tmp/program_control/M7_junit.xml | JUnit XML |
| 冒烟脚本 | scripts/smoke_test.py | 生产冒烟脚本 |
| 演练脚本 | scripts/rehearsal_backup_restore.py | 备份恢复演练脚本 |
| 评测脚本 | scripts/final_1500_evaluation.py | 1,500 条评测脚本 |

### 2.3 验收环境

验收人员需具备以下环境能力：

- 可访问项目根目录：`e:\Software Development\Policy inspection system`
- 可运行 Python 与 pytest
- 可读取工作区数据库副本：`data/policy_inspector.db`
- 可访问生产环境只读接口以核对边界（仅读，不写）
- 可访问 git 仓库以核对分支与 HEAD

> 验收人员不得在生产环境执行任何写操作，不得重启生产服务，不得修改 Nginx/PM2/证书。

---

## 3. 验收入口路径

以下路径是验收人员需要访问的全部入口。验收人员应按本清单逐一确认文件存在且可读。

### 3.1 主控与变更管理

| 入口 | 路径 | 用途 |
|------|------|------|
| 主控状态 | docs/program_control/status.json | milestone 状态总览 |
| 变更日志 | docs/program_control/change_log.md | 全量变更记录 |
| 风险登记 | docs/program_control/risk_register.md | 风险与遗留问题 |
| 任务矩阵 | docs/program_control/final_task_matrix.md | 最终任务矩阵 |

### 3.2 最终交付

| 入口 | 路径 | 用途 |
|------|------|------|
| 最终交付目录 | docs/final_candidate/ | 10 个最终交付文档 |

### 3.3 里程碑报告

| 入口 | 路径 | 用途 |
|------|------|------|
| 里程碑报告 | docs/milestones/M0/ 至 docs/milestones/M7/ | 每个 milestone 2 个报告 |

### 3.4 自验与测试输出

| 入口 | 路径 | 用途 |
|------|------|------|
| 自验摘要 | tmp/program_control/M0_self_acceptance.json ~ M7_self_acceptance.json | 各 milestone 自验结果 |
| 测试输出 | tmp/program_control/Mx_test_output.txt | 各 milestone 测试原始输出 |
| 演练报告 | tmp/program_control/m6_rehearsal/ | M6-8 生产副本演练 |
| 评测报告 | tmp/program_control/M7_1500_evaluation.json / .md | 1,500 条评测 |
| JUnit XML | tmp/program_control/M7_junit.xml | JUnit 格式测试结果 |

---

## 4. 验收步骤

验收人员应严格按以下顺序执行 12 个步骤。每一步执行完毕后，将结果记录到验收记录表（见第 6 节门禁清单）。任一步骤失败，应立即停止并按第 7 节结论模板给出 PARTIAL 或 FAIL 结论。

### 步骤 1：核对 status.json

读取 `docs/program_control/status.json`，确认：

- M0 至 M7 全部状态为 DONE
- 最终独立总验收状态为 PENDING
- 无 milestone 处于 IN_PROGRESS 或 BLOCKED

预期片段：

```json
{
  "milestones": {
    "M0": "DONE",
    "M1": "DONE",
    "M2": "DONE",
    "M3": "DONE",
    "M4": "DONE",
    "M5": "DONE",
    "M6": "DONE",
    "M7": "DONE"
  },
  "final_independent_acceptance": "PENDING"
}
```

### 步骤 2：核对生产边界

读取 `docs/program_control/status.json` 中的生产边界字段，确认以下 6 项全部为 false：

| 字段 | 预期值 |
|------|--------|
| production_database_modified | false |
| production_service_restarted | false |
| production_nginx_modified | false |
| production_pm2_modified | false |
| production_certificates_modified | false |
| high_frequency_external_scanning | false |

> 任一项为 true 即视为生产边界被破坏，验收立即 FAIL。

### 步骤 3：核对 git 状态

执行：

```bash
git rev-parse --abbrev-ref HEAD
git rev-parse HEAD
git status --short
```

预期：

- 当前分支：`feat/full-policy-inspection-upgrade`
- 当前 HEAD：与 `docs/program_control/status.json` 中整改入库 head 一致（`git rev-parse HEAD`）
- 工作区干净（`git status --short` 无输出）——**不干净则 FAIL（不可复现树）**

### 步骤 4：核对数据库 SHA256

执行（PowerShell 示例）：

```powershell
Get-FileHash data\policy_inspector.db -Algorithm SHA256
```

预期：

- 工作区数据库：记录实际哈希（本地副本可因幂等迁移/测试变化；**不得**写入 git）
- 生产数据库 SHA256（仅读，不写，不重启服务）：`13E6A8A412F69C13D149674A31076854C966F5B151E670FC9E22D0F53AB77AF3`

> 若生产数据库 SHA256 与上述不符，视为生产数据库被修改，验收立即 FAIL。

### 步骤 5：运行全量回归

执行：

```bash
python -m pytest tests/ -q
```

预期：

- 391 passed（或更高，若后续仅增测）
- 0 failed

> 基线为 M7 完成 347 项 + P0/P1 整改及增补（CSRF、证据 KPI、M6、logout/失败登录审计等）。

若存在失败项，记录实际数值并停止验收。

### 步骤 6：运行 1,500 条评测

执行：

```bash
python scripts/final_1500_evaluation.py
```

预期：

- 评测结果 PASS
- 引擎 = `production_rule_modules`（调用 app.rules / app.pdf / app.archive / app.duplicates / app.reconciliation / app.reading_linkage / app.media / app.interpretation / app.rectification）
- Precision = 1.0000
- Recall = 1.0000
- F1 = 1.0000
- 样本数 = 1,500
- 规则数 = 74

评测输出应与 `tmp/program_control/M7_1500_evaluation.json` 一致，且 JSON 顶层 `engine` 字段为 `production_rule_modules`。验收人员应独立运行并核对评测确实调用了生产规则模块，不依赖主开发 AI 提供的输出。

### 步骤 7：运行备份恢复演练

执行：

```bash
python scripts/rehearsal_backup_restore.py
```

预期：

- 10/10 PASS

演练基于生产副本执行，不得对生产数据库产生任何写入。演练报告应输出到 `tmp/program_control/m6_rehearsal/` 下。

### 步骤 8：运行冒烟脚本

前置：启动本地服务（不启动生产服务）。

执行：

```bash
python scripts/smoke_test.py
```

预期：

- 15/15 PASS

冒烟脚本覆盖的关键路径包括：

- 登录与鉴权
- 导航项接口
- 规则目录接口
- 巡检任务创建与查询
- 报告生成与下载
- 健康检查

### 步骤 9：核对最终交付文档

确认 `docs/final_candidate/` 下 10 个文档全部存在且内容完整：

| 序号 | 文件 | 核对要点 |
|------|------|----------|
| 01 | 01_final_architecture.md | 架构图、模块清单、数据流 |
| 02 | 02_final_feature_matrix.md | 功能矩阵覆盖全部 milestone |
| 03 | 03_final_rule_catalog.md | 74 条规则全量目录 |
| 04 | 04_final_security_report.md | 安全机制清单与验证 |
| 05 | 05_final_data_quality_report.md | 数据质量指标 |
| 06 | 06_final_test_report.md | 测试覆盖与结果 |
| 07 | 07_final_migration_report.md | 迁移脚本与回滚 |
| 08 | 08_production_cutover_runbook.md | 生产切换步骤 |
| 09 | 09_final_rollback_runbook.md | 回滚步骤 |
| 10 | 10_independent_acceptance_guide.md | 本文档 |

### 步骤 10：核对规则目录

连接工作区数据库副本，执行：

```sql
SELECT COUNT(*) FROM rule_versions WHERE effective_to IS NULL;
```

预期：

- 74 条规则

同时核对 `docs/final_candidate/03_final_rule_catalog.md` 中列出的规则与数据库一致（`rule_id` 集合相等）。

可选的规则码抽查：

```sql
SELECT DISTINCT rule_id FROM rule_versions WHERE effective_to IS NULL ORDER BY rule_id;
```

预期返回 74 行，与 `03_final_rule_catalog.md` 中的规则码集合完全一致。

### 步骤 11：核对 21 项导航

登录后调用：

```
GET /api/nav-items
```

预期：

- 返回 21 项导航
- 每项包含 id、name、path、icon、order、visibility 等字段
- 顺序与 `docs/final_candidate/02_final_feature_matrix.md` 一致

### 步骤 12：核对安全机制

核对以下 3 项安全机制：

1. 生产 `/docs` 接口返回 404
2. CSRF 三段校验（token、cookie、header）生效
3. fail-closed 策略生效（规则未命中时拒绝放行）

预期：

- `/docs` 返回 404
- CSRF 三段校验通过
- fail-closed 生效

> 安全机制核对应在生产副本或本地服务上执行，不得在生产环境执行任何写操作。

---

## 5. 验收门禁清单

以下 11 项门禁必须全部 PASS，方可给出 PASS 结论：

| 序号 | 门禁项 | 验收方式 | 预期结果 | 实际结果 |
|------|--------|----------|----------|----------|
| 1 | M0-M7 全部自验 PASS | 核对 status.json 与自验摘要 | 全部 DONE |  |
| 2 | 全量回归 388 passed / 0 failed | 运行 pytest | 388 passed |  |
| 3 | 1,500 评测 P/R/F1 = 1.0000（生产规则引擎） | 运行 final_1500_evaluation.py，核对 engine=production_rule_modules | P/R/F1=1.0 |  |
| 4 | M6-8 演练 10/10 PASS | 运行 rehearsal_backup_restore.py | 10/10 |  |
| 5 | 生产数据库 SHA256 未变 | 核对生产数据库哈希 | 与基线一致 |  |
| 6 | 生产服务未重启 | 核对 status.json 与生产进程 | 未重启 |  |
| 7 | 未修改 Nginx/PM2/证书 | 核对 status.json 与配置文件 | 未修改 |  |
| 8 | 10 个最终交付文档存在 | 核对 docs/final_candidate/ | 10 个齐全 |  |
| 9 | 规则目录 74 条全在 | 查询 rule_versions | 74 条 |  |
| 10 | 21 项导航可见 | 调用 /api/nav-items | 21 项 |  |
| 11 | 安全机制生效 | 核对 /docs、CSRF、fail-closed | 全部生效 |  |

> 验收人员应将"实际结果"列填写完整，作为验收记录归档。

---

## 6. 验收门禁判定矩阵

| 门禁项失败数量 | 失败类型 | 结论 |
|----------------|----------|------|
| 0 | 无 | PASS |
| 1-3 且非关键门禁 | 非关键 | PARTIAL |
| 任一关键门禁失败 | 关键 | FAIL |
| 4 项及以上失败 | 任意 | FAIL |

关键门禁（任一失败即 FAIL）：

- 门禁 1：M0-M7 全部自验 PASS
- 门禁 2：全量回归 388 passed / 0 failed
- 门禁 3：1,500 评测 P/R/F1 = 1.0000（生产规则引擎）
- 门禁 5：生产数据库 SHA256 未变
- 门禁 6：生产服务未重启
- 门禁 7：未修改 Nginx/PM2/证书
- 门禁 11：安全机制生效

非关键门禁（失败可 PARTIAL）：

- 门禁 4：M6-8 演练 10/10 PASS
- 门禁 8：10 个最终交付文档存在
- 门禁 9：规则目录 74 条全在
- 门禁 10：21 项导航可见

> 非关键门禁失败时，仍需主开发 AI 修复后重新验收，不得跳过。

---

## 7. 验收结论模板

验收人员在完成全部 12 步后，按以下模板出具验收结论，并写入 `docs/program_control/status.json` 的 `final_independent_acceptance` 字段与 `docs/program_control/risk_register.md`。

### 7.1 PASS 模板

```text
独立总验收结论：PASS

验收人员：[独立总验收 AI / 验收人员]
验收日期：2026-07-28
验收基线：
  - 分支：feat/full-policy-inspection-upgrade
  - HEAD：79dacc0e0cc8e210d5d72c7042266e3568f83135
  - 工作区数据库 SHA256：1EAEC1D8957E4D1250770F688D40A93E9D51387C24EC03BA9C0F19095E6434CE
  - 生产数据库 SHA256（未变）：13E6A8A412F69C13D149674A31076854C966F5B151E670FC9E22D0F53AB77AF3

门禁结果：
  1. M0-M7 全部自验 PASS - PASS
  2. 全量回归 388 passed / 0 failed - PASS
  3. 1,500 评测 P/R/F1 = 1.0000（生产规则引擎） - PASS
  4. M6-8 演练 10/10 PASS - PASS
  5. 生产数据库 SHA256 未变 - PASS
  6. 生产服务未重启 - PASS
  7. 未修改 Nginx/PM2/证书 - PASS
  8. 10 个最终交付文档存在 - PASS
  9. 规则目录 74 条全在 - PASS
  10. 21 项导航可见 - PASS
  11. 安全机制生效 - PASS

结论：所有门禁通过，可进入生产切换阶段。
```

### 7.2 PARTIAL 模板

```text
独立总验收结论：PARTIAL

验收人员：[独立总验收 AI / 验收人员]
验收日期：2026-07-28

失败门禁：
  - [列出失败的非关键门禁]

修复要求：
  - 主开发 AI 需修复上述失败项后重新提交验收
  - 修复过程不得破坏生产边界
  - 修复后需重新执行本指南全部 12 步

结论：部分门禁失败，需主开发 AI 修复后重新验收。
```

### 7.3 FAIL 模板

```text
独立总验收结论：FAIL

验收人员：[独立总验收 AI / 验收人员]
验收日期：2026-07-28

失败门禁：
  - [列出失败的关键门禁]

重做要求：
  - 主开发 AI 需重做相关 milestone
  - 重做后需重新执行全量回归与 1,500 评测
  - 重做过程不得破坏生产边界

结论：关键门禁失败，需主开发 AI 重做相关 milestone。
```

---

## 8. 验收记录与归档

验收人员应将以下记录归档：

| 记录类型 | 归档路径 | 说明 |
|----------|----------|------|
| 验收记录表 | tmp/program_control/independent_acceptance_record.md | 第 5 节门禁清单填写完整 |
| 验收结论 | docs/program_control/status.json | final_independent_acceptance 字段 |
| 失败项记录 | docs/program_control/risk_register.md | 追加到风险登记表 |
| 测试原始输出 | tmp/program_control/independent_acceptance_output/ | 验收人员独立运行的输出 |

验收记录表模板：

```markdown
# 独立总验收记录表

| 字段 | 值 |
|------|----|
| 验收人员 |  |
| 验收日期 | 2026-07-28 |
| 验收基线分支 | feat/full-policy-inspection-upgrade |
| 验收基线 HEAD | 79dacc0e0cc8e210d5d72c7042266e3568f83135 |
| 工作区数据库 SHA256 |  |
| 生产数据库 SHA256 |  |
| 全量回归结果 |  |
| 1,500 评测结果 |  |
| M6-8 演练结果 |  |
| 冒烟测试结果 |  |
| 最终结论 |  |

## 门禁明细

[填写第 5 节门禁清单的实际结果列]

## 失败项与修复建议

[如有失败项，列出并给出修复建议]
```

---

## 9. 注意事项

1. **不得提前声称封板**：在第二个 AI（独立总验收 AI）按本指南完成全部验收并出具 PASS 结论前，任何人不得声称"最终封板完成"。

2. **独立执行**：验收人员应独立执行所有命令，不依赖主开发 AI 的输出。所有测试、评测、演练均由验收人员重新运行，所有结果均以验收人员一手证据为准。

3. **生产边界保护**：验收过程中不得对生产环境执行任何写操作，包括但不限于：
   - 不得修改生产数据库
   - 不得重启生产服务
   - 不得修改 Nginx 配置
   - 不得修改 PM2 配置
   - 不得修改证书
   - 不得发起高频外部扫描

4. **问题记录**：验收发现的任何问题应记录到 `docs/program_control/risk_register.md`，包括问题描述、影响范围、修复建议、责任人。

5. **可重复性**：验收过程应可重复。验收人员应保留所有原始输出，以便主开发 AI 修复后重新验收时比对。

6. **基线锁定**：验收基线（分支、HEAD、SHA256）一经确认，不得变更。如需变更，需主开发 AI 重新提交并重新验收。

7. **文档一致性**：验收人员应核对最终交付文档之间的数据一致性，包括：
   - 规则数 74 条在 03、05、06 文档中一致
   - 导航数 21 项在 02、06 文档中一致
   - 测试数 388 在 06 文档中与实际运行一致（M7 基线 347 + P0/P1 整改 41）
   - 评测 P/R/F1=1.0 在 06 文档与 M7_1500_evaluation.json 中一致，且 engine=production_rule_modules

8. **回滚预案**：即使在验收 PASS 后，生产切换前应确认 `docs/final_candidate/09_final_rollback_runbook.md` 可执行。验收人员应阅读回滚 Runbook 并确认其步骤完整可执行。

---

## 10. 验收流程图

```text
[开始]
   |
   v
[步骤 1: 核对 status.json] --FAIL--> [结论: FAIL]
   |
   v (PASS)
[步骤 2: 核对生产边界] --FAIL--> [结论: FAIL]
   |
   v (PASS)
[步骤 3: 核对 git 状态] --FAIL--> [结论: PARTIAL]
   |
   v (PASS)
[步骤 4: 核对数据库 SHA256] --FAIL--> [结论: FAIL]
   |
   v (PASS)
[步骤 5: 运行全量回归] --FAIL--> [结论: FAIL]
   |
   v (PASS)
[步骤 6: 运行 1,500 评测] --FAIL--> [结论: FAIL]
   |
   v (PASS)
[步骤 7: 运行备份演练] --FAIL--> [结论: PARTIAL]
   |
   v (PASS)
[步骤 8: 运行冒烟脚本] --FAIL--> [结论: PARTIAL]
   |
   v (PASS)
[步骤 9: 核对最终交付文档] --FAIL--> [结论: PARTIAL]
   |
   v (PASS)
[步骤 10: 核对规则目录] --FAIL--> [结论: PARTIAL]
   |
   v (PASS)
[步骤 11: 核对 21 项导航] --FAIL--> [结论: PARTIAL]
   |
   v (PASS)
[步骤 12: 核对安全机制] --FAIL--> [结论: FAIL]
   |
   v (PASS)
[结论: PASS] --> [可进入生产切换阶段]
   |
   v
[结束]
```

---

## 11. 验收与生产切换的衔接

独立总验收 PASS 后，进入生产切换阶段。衔接关系如下：

| 阶段 | 责任方 | 输入 | 输出 |
|------|--------|------|------|
| 独立总验收 | 独立总验收 AI | 本指南 | 验收结论 PASS |
| 生产切换 | 运维 + 主开发 AI | 08_production_cutover_runbook.md | 切换完成 |
| 切换后冒烟 | 运维 | scripts/smoke_test.py | 15/15 PASS |
| 切换后观察 | 运维 | 监控面板 | 24h 无异常 |
| 回滚（如需） | 运维 + 主开发 AI | 09_final_rollback_runbook.md | 回滚完成 |

> 独立总验收 PASS 是生产切换的前置条件。未通过独立总验收，不得启动生产切换。

---

## 12. 附录

### 12.1 验收命令速查

```bash
# 步骤 3: git 状态
git rev-parse --abbrev-ref HEAD
git rev-parse HEAD
git status --short

# 步骤 4: 数据库 SHA256（PowerShell）
Get-FileHash data\policy_inspector.db -Algorithm SHA256

# 步骤 5: 全量回归
python -m pytest tests/ -q

# 步骤 6: 1,500 评测
python scripts/final_1500_evaluation.py

# 步骤 7: 备份恢复演练
python scripts/rehearsal_backup_restore.py

# 步骤 8: 冒烟脚本
python scripts/smoke_test.py
```

### 12.2 验收基线速查

| 项 | 值 |
|----|----|
| 分支 | feat/full-policy-inspection-upgrade |
| HEAD | 79dacc0e0cc8e210d5d72c7042266e3568f83135 |
| 工作区数据库 SHA256 | 1EAEC1D8957E4D1250770F688D40A93E9D51387C24EC03BA9C0F19095E6434CE |
| 生产数据库 SHA256 | 13E6A8A412F69C13D149674A31076854C966F5B151E670FC9E22D0F53AB77AF3 |
| 全量回归 | 388 passed / 0 failed |
| 1,500 评测 | P=R=F1=Accuracy=1.0000（调用生产规则引擎） |
| M6-8 演练 | 10/10 PASS |
| 冒烟测试 | 15/15 PASS |
| 规则数 | 74 |
| 导航数 | 21 |

### 12.3 关键路径速查

| 路径 | 用途 |
|------|------|
| docs/program_control/status.json | 主控状态 |
| docs/program_control/risk_register.md | 风险登记 |
| docs/final_candidate/ | 最终交付 |
| tmp/program_control/M7_1500_evaluation.json | 1,500 评测 |
| tmp/program_control/M7_junit.xml | JUnit XML |
| scripts/smoke_test.py | 冒烟脚本 |
| scripts/rehearsal_backup_restore.py | 演练脚本 |
| scripts/final_1500_evaluation.py | 评测脚本 |

---

## 13. 结论

本指南是独立总验收入口，待独立总验收 AI 按本指南执行全部 12 步并出具验收结论。在独立总验收 AI 出具 PASS 结论前，本项目处于"主开发 AI 自验完成，待独立总验收"状态，不得声称"最终封板完成"，不得启动生产切换。

独立总验收 AI 应：

1. 独立执行本指南全部 12 步
2. 如实填写第 5 节门禁清单的实际结果
3. 按第 7 节模板出具验收结论
4. 将验收记录归档至第 8 节指定路径
5. 将失败项追加至 `docs/program_control/risk_register.md`

待独立总验收 AI 出具 PASS 结论后，本项目方可进入生产切换阶段，按 `docs/final_candidate/08_production_cutover_runbook.md` 执行生产切换。

---

文档结束
