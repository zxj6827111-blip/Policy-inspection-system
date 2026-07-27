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

