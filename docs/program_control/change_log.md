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

## 2026-07-27 M2 启动

### M2-1 基线记录

- 分支：`feat/full-policy-inspection-upgrade`
- 开始 HEAD：`9ddcf2117cd55e55c49e50309560e2cb31d35f8d`
- 生产数据库 SHA256：`13e6a8a412f69c13d149674a31076854c966f5b151e670fc9e22d0f53ab77af3`（未变）

### M2-2 数据库迁移

- `app/migrations/m2.py`：新增 `review_assignments`、`rule_effect_snapshots` 表
- `finding_reviews` 新增 `review_round` 列；`rule_versions` 新增 `auto_confirm_allowed`/`degraded`/`degraded_reason`/`degraded_at` 列
- 通过 `Database.initialize()` 自动幂等执行
- 阈值常量：`AUTO_CONFIRM_PRECISION_THRESHOLD=0.95`、`SUSPECTED_PRECISION_THRESHOLD=0.80`

### M2-3 服务层

- `app/review_batch.py`：`batch_review` + `cluster_findings`
- `app/review_dual.py`：`assign_dual_review` / `submit_dual_review` / `arbitrate` / `list_dual_assignments` / `compute_consistency_rate`
- `app/rule_effect.py`：`compute_rule_effectiveness` + `snapshot_rule_effectiveness`（修复 SQL WHERE 重复语法错误）
- `app/rule_degradation.py`：`evaluate_rule_degradation`

### M2-4 API 端点

- 10 个 M2 端点加入 `app/main.py`，全部经 `require_write("review:write")` 或 `require_login` 校验
- 写操作落 `operation_audits`

### M2-5 1,000 条样本评测

- `scripts/generate_m2_samples.py`：分配 AGENCY-001 300 / DATE-001 200 / DOC-001 200 / META-001 100 / LINK-001 200
- 代理标签基于 R-007 启发式精度
- `evaluate_samples` 调用规则效果中心输出 precision/FPR

### M2-6 测试

- `tests/test_m2_review_rule_effect.py` 15 用例
- 修复 SQL 语法错误、快照参数计数、双用户 CSRF 切换、动态 user_id
- 全部通过

### M2 完成

- M2 专项：15 passed
- 全量回归：211 passed
- 产物：
  - `docs/milestones/M2/development_report.md`
  - `docs/milestones/M2/self_acceptance_report.md`
  - `tmp/program_control/M2_self_acceptance.json`
  - `tmp/program_control/M2_commands.txt`
  - `tmp/program_control/M2_test_output.txt`
- status.json 更新：M2=DONE，M3=IN_PROGRESS
- 自动进入 M3

---

## 2026-07-27 M3 启动

### M3-1 基线记录

- 分支：`feat/full-policy-inspection-upgrade`
- 开始 HEAD：（待 M2 提交后记录）
- 生产数据库 SHA256：未变

