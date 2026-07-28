# M1 开发报告

> 里程碑：M1 安全底座与可信 finding 验证
> 主开发 AI：GLM-5.2（唯一主开发）
> 执行日期：2026-07-27
> 分支：`feat/full-policy-inspection-upgrade`

---

## 一、范围

- 登录、RBAC、CSRF、审计；
- 生产 fail-closed；
- 保护 API 文档；
- 多角色真实浏览器验证（本里程碑在 API 层完成，UI 路由可达性已验证，真实浏览器端到端验证留待 M7 生产切换准备）；
- finding、evidence、rule version；
- review lineage；
- 统计口径；
- 机构和机关代字规则；
- 指定样本回归。

## 二、开始基线

| 项 | 值 |
|---|---|
| 开始分支 | `feat/full-policy-inspection-upgrade` |
| 开始 HEAD | `ac0310d`（M0 结束） |
| 生产数据库 SHA256 | `13e6a8a412f69c13d149674a31076854c966f5b151e670fc9e22d0f53ab77af3` |
| 基线测试数 | 184 passed |

## 三、设计

Phase1 已完成并独立验收，登录/RBAC/CSRF/审计/fail-closed/finding 模型/review lineage/统计口径/机构规则/样本回归均已在 `test_phase1_security_org.py`、`test_phase1_closeout.py` 覆盖。M1 聚焦两个缺口：

1. **API 文档保护**：`app/main.py` 中 `FastAPI()` 默认暴露 `/docs`、`/redoc`、`/openapi.json`，生产环境需 fail-closed。
2. **五角色权限矩阵完整性**：现有测试覆盖 admin/viewer/reviewer，需补全 operator/auditor 显式边界测试。

## 四、代码变更

### 4.1 API 文档保护中间件

`app/main.py` 新增 `protect_api_docs` 中间件：

- 拦截 `/docs`、`/redoc`、`/openapi.json` 三路径；
- 当 `APP_ENV=production` 时返回 `404 {"detail":"API 文档在生产环境已禁用"}`；
- 非生产环境（test/development）保持可访问，便于开发与测试；
- 通过 `get_runtime_settings()` 在请求时动态判断，与 lifespan 中重置的设置一致。

### 4.2 API 文档保护测试

`tests/test_api_doc_protection.py`（7 项）：

- `test_docs_accessible_in_test_env[/docs]`
- `test_docs_accessible_in_test_env[/redoc]`
- `test_docs_accessible_in_test_env[/openapi.json]`
- `test_docs_blocked_in_production_env[/docs]`
- `test_docs_blocked_in_production_env[/redoc]`
- `test_docs_blocked_in_production_env[/openapi.json]`
- `test_health_and_login_still_work_in_production`

### 4.3 五角色权限矩阵测试

`tests/test_role_permission_matrix.py`（5 项）：

- `test_operator_can_write_jobs_but_not_review`：operator 可读 findings/scan-sites，不可复核、不可管用户；
- `test_auditor_can_read_audits_but_not_write_or_review`：auditor 可读审计/finding，不可创建任务、不可复核；
- `test_viewer_cannot_read_audits`：viewer 无 `audit:read`；
- `test_role_capability_matrix_contract`：锁定 `ROLE_CAPABILITIES` 合约；
- `test_ui_shell_and_static_reachable`：UI 首页与静态资源可达。

### 4.4 测试配置

`pyproject.toml` 注册 `real_auth` pytest mark，消除既有告警。

### 4.5 数据库迁移

M1 不涉及数据库迁移。生产数据库 SHA256 未变。

## 五、已验证项（继承自 Phase1 + M1 新增）

### 5.1 登录 / RBAC / CSRF / 审计

| 项 | 测试 | 结果 |
|---|---|---|
| 未登录写被拒 | `test_unauthenticated_write_is_rejected` | PASS |
| viewer 不可写/复核 | `test_viewer_cannot_call_write_or_review` | PASS |
| reviewer 可复核、不可管用户 | `test_reviewer_can_review_but_not_manage_users` | PASS |
| admin 可建用户且审计记录 | `test_admin_can_create_user_and_audit_recorded` | PASS |
| CSRF 强制 | `test_csrf_required_on_cookie_writes` | PASS |
| 健康接口不暴露 DB 路径 | `test_health_hides_database_path` | PASS |
| operator 边界 | `test_operator_can_write_jobs_but_not_review`（M1 新增） | PASS |
| auditor 边界 | `test_auditor_can_read_audits_but_not_write_or_review`（M1 新增） | PASS |
| viewer 不可读审计 | `test_viewer_cannot_read_audits`（M1 新增） | PASS |
| 角色能力合约 | `test_role_capability_matrix_contract`（M1 新增） | PASS |
| UI 可达 | `test_ui_shell_and_static_reachable`（M1 新增） | PASS |

### 5.2 生产 fail-closed

| 项 | 测试 | 结果 |
|---|---|---|
| production 禁止 POLICY_AUTH_DISABLED=1 | `test_auth_fail_closed_production_forbids_disabled` | PASS |
| 生产误配拒绝启动 | `test_production_misconfig_refuses_app_startup` | PASS |
| 未设 APP_ENV 默认 production | `test_auth_fail_closed_production_forbids_disabled` | PASS |
| API 文档生产环境 404 | `test_docs_blocked_in_production_env`（M1 新增） | PASS |
| 健康与登录生产可用 | `test_health_and_login_still_work_in_production`（M1 新增） | PASS |

### 5.3 finding / evidence / rule version / review lineage

| 项 | 测试 | 结果 |
|---|---|---|
| finding 机器/人工状态迁移、复核只追加 | `test_finding_state_migration_and_review_append` | PASS |
| 重扫继承人工结论 | `test_human_conclusion_inherited_on_resave` | PASS |
| 普委办41号 rule_version 非空 | `test_puweiban_2013_41_joint_forward_no_org001` | PASS |
| 普委办41号 evidence_fields 含联合发文机关 | `test_puweiban_2013_41_joint_forward_no_org001` | PASS |
| 证据弹窗动态字段 | `test_finding_state_migration_and_review_append` | PASS |

### 5.4 统计口径

| 项 | 测试 | 结果 |
|---|---|---|
| AGENCY-001 不进正式问题 | `test_agency_001_not_customer_formal` | PASS |
| ORG-002 hint 不进正式问题 | `test_puweiban_2013_41_joint_forward_no_org001` | PASS |
| 正式问题计数语义 | `test_phase1_closeout.py` 计数测试 | PASS |

### 5.5 机构与机关代字 + 样本回归

| 项 | 测试 | 结果 |
|---|---|---|
| 普委办〔2013〕41号 转发不误报 ORG-001 | `test_puweiban_2013_41_joint_forward_no_org001` | PASS |
| 普宜办〔2020〕3号 街道办匹配 | `test_street_office_and_historical_alias_symbols` | PASS |
| 长街办〔2018〕2号 历史别名匹配 | `test_street_office_and_historical_alias_symbols` | PASS |
| 普委办转发仍通过 | `test_puweiban_and_street_still_pass` | PASS |

## 六、测试

### 6.1 全量回归

```
.venv\Scripts\python.exe -m pytest -q
196 passed, 1 warning in 242.44s
```

完整输出见 `tmp/program_control/M1_test_output.txt`。

### 6.2 增量测试

| 文件 | 用例数 | 状态 |
|---|---:|---|
| tests/test_api_doc_protection.py | 7 | PASS |
| tests/test_role_permission_matrix.py | 5 | PASS |

## 七、门禁达成情况

| 门禁 | 要求 | 实际 | 结果 |
|---|---|---|---|
| 生产 fail-closed | POLICY_AUTH_DISABLED 仅 APP_ENV=test | 已实现并测试 | PASS |
| API 文档生产受保护 | /docs /redoc /openapi.json 生产 404 | 中间件实现 + 测试 | PASS |
| 五角色权限矩阵 | admin/operator/reviewer/viewer/auditor 边界 | API 层全覆盖 | PASS |
| finding 携带 rule_version | 是 | 测试验证非空 | PASS |
| 重扫保留复核历史且 is_current 正确 | 是 | 测试验证 | PASS |
| 正式问题与线索统计口径分离 | 是 | 测试验证 | PASS |
| 普委办41号不误报 ORG-001 | 是 | 测试验证 | PASS |
| 普宜办/长街办匹配 | 是 | 测试验证 | PASS |
| 全量测试通过 | ≥184 | 196 passed | PASS |
| 生产数据库 SHA256 不变 | `13e6a8...77af3` | 未变 | PASS |
| 未 push | 是 | 未执行 | PASS |
| 未合并 main | 是 | 未执行 | PASS |
| 未重启生产服务 | 是 | 未执行 | PASS |

## 八、结束状态

| 项 | 值 |
|---|---|
| 结束分支 | `feat/full-policy-inspection-upgrade` |
| 结束 HEAD | `9ddcf2117cd55e55c49e50309560e2cb31d35f8d` |
| 生产数据库 SHA256 | `13e6a8a412f69c13d149674a31076854c966f5b151e670fc9e22d0f53ab77af3`（未变） |
| 工作区状态 | 干净 |
| 测试 | 196 passed |

## 九、遗留说明

- 真实浏览器端到端验证（Playwright headless）留待 M7 生产切换准备阶段，因 M0～M6 不重启生产服务，且 M1 在开发分支验证。API 层权限矩阵 + UI 路由可达性已覆盖核心安全边界。
- `PytestCacheWarning: could not create cache path` 为 Windows 权限告警，不影响测试结果。

## 十、自验结论

M1 全部门禁达成，自验 PASS，可进入 M2。
