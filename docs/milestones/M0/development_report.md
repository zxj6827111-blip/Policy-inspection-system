# M0 开发报告

> 里程碑：M0 Git 正式化与 Phase1 固化
> 主开发 AI：GLM-5.2（唯一主开发）
> 执行日期：2026-07-27
> 分支：`feat/full-policy-inspection-upgrade`

---

## 一、范围

- Git 正式化；
- 创建 `feat/full-policy-inspection-upgrade` 主开发分支；
- 固化 Phase1 已验收树；
- 工作区干净；
- 无敏感文件被跟踪；
- 全量测试通过。

## 二、开始基线

| 项 | 值 |
|---|---|
| 开始分支 | `feat/phase1-security-findings-org` |
| 开始 HEAD | `f862c75ed1916dfce67d9dea90667b5775a2d789` |
| 生产数据库 SHA256 | `13e6a8a412f69c13d149674a31076854c966f5b151e670fc9e22d0f53ab77af3` |
| 基线测试数 | 184 passed |
| 工作区状态 | 仅 `.gitignore` 修改 |

## 三、设计

M0 不引入业务功能代码，目标是建立 M0～M7 的执行底座：

1. 在 `docs/program_control/` 建立统一总控文件，固化 V3.0 等价方案（整合 V2.0 业务目标与 M0～M7 里程碑结构）。
2. 从 Phase1 已验收分支创建 `feat/full-policy-inspection-upgrade` 作为后续所有里程碑的主开发分支。
3. 提交 `.gitignore` 改进（扩展规划文档忽略规则），使工作区干净。
4. 严守生产边界：不 push、不合并 main、不修改生产数据库、不重启生产服务。

## 四、代码与文档变更

### 4.1 新增总控文件

- `docs/program_control/full_upgrade_plan_v3.md`：V3.0 统一执行方案（M0～M7 里程碑、门禁、生产边界、技术架构、数据库迁移原则、Git 策略、总任务矩阵）。
- `docs/program_control/status.json`：里程碑状态、基线、生产边界标志。
- `docs/program_control/change_log.md`：变更日志。
- `docs/program_control/risk_register.md`：风险登记册（10 条风险，含 OCR 依赖、样本标注、浏览器验证等）。
- `docs/program_control/final_task_matrix.md`：M0～M7 全部任务与最终交付物跟踪表。

### 4.2 .gitignore 改进

- 扩展规划文档忽略规则：`*实施方案*.md` / `*实施方*.md`，覆盖根目录本地规划文档。
- V2.0 方案文件 `政策巡检系统完整改造实施方案_V2.0.md` 已被正确忽略，未入库。

### 4.3 数据库迁移

M0 不涉及数据库迁移。生产数据库 SHA256 未变。

### 4.4 API / UI

M0 不涉及 API / UI 变更。

## 五、测试

### 5.1 全量回归

```
.venv\Scripts\python.exe -m pytest -q
184 passed, 3 warnings in 191.29s
```

完整输出见 `tmp/program_control/M0_test_output.txt`。

### 5.2 警告说明

- `PytestUnknownMarkWarning: Unknown pytest.mark.real_auth`：Phase1 已知告警，不影响通过。
- `PytestCacheWarning: could not create cache path .pytest_cache`：Windows 权限告警，不影响测试结果。

## 六、敏感文件检查

```
git ls-files | findstr /R "\.db$ \.env \.key \.pem \.log secret"
```
无匹配 → 无敏感文件被跟踪。

`data/` 目录下仅跟踪 6 张 PNG 导出预览图（文档用途），未跟踪任何 `.db` 文件。

## 七、门禁达成情况

| 门禁 | 要求 | 实际 | 结果 |
|---|---|---|---|
| 工作区干净 | 是 | 干净 | PASS |
| 提交后树与验收前树内容一致 | 是 | 仅新增总控文档与 .gitignore 改进，未改动 Phase1 业务代码 | PASS |
| 全量测试不少于 184 passed | ≥184 | 184 passed | PASS |
| 生产数据库 SHA256 不变 | `13e6a8...77af3` | `13E6A8...77AF3` | PASS |
| 未 push | 是 | 未执行 git push | PASS |
| 未合并 main | 是 | 未执行 git merge main | PASS |
| 未重启生产服务 | 是 | 未执行 pm2/systemctl/nginx | PASS |
| 无敏感文件入库 | 是 | 检查通过 | PASS |

## 八、结束状态

| 项 | 值 |
|---|---|
| 结束分支 | `feat/full-policy-inspection-upgrade` |
| 结束 HEAD | `e9c2fb24fb46fb79f814b253e6b1482fc0a58b48` |
| 生产数据库 SHA256 | `13e6a8a412f69c13d149674a31076854c966f5b151e670fc9e22d0f53ab77af3`（未变） |
| 工作区状态 | 干净 |
| 测试 | 184 passed |

## 九、自验结论

M0 全部门禁达成，自验 PASS，可进入 M1。

## 十、下一里程碑衔接

M1 将在本分支 `feat/full-policy-inspection-upgrade` 上继续，验证并补强安全底座与可信 finding 模型，严守生产边界。
