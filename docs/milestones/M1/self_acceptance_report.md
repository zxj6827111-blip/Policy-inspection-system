# M1 自验报告

> 里程碑：M1 安全底座与可信 finding 验证
> 自验执行者：唯一主开发 AI（GLM-5.2）
> 自验日期：2026-07-27
> 自验结论：**PASS**

---

## 一、自验依据

依据《政策巡检系统完整改造实施方案 V3.0（统一执行版）》M1 章节门禁：

- 生产 fail-closed；
- API 文档受保护；
- 五角色权限矩阵；
- finding 携带 rule_version；
- 重扫保留复核历史且 is_current 正确；
- 正式问题与线索统计口径分离；
- 普委办〔2013〕41号 不误报 ORG-001；普宜办、长街办 匹配正确；
- 全量测试通过；
- 生产数据库 SHA256 不变。

## 二、自验项与结果

| 序号 | 自验项 | 验证方法 | 期望 | 实际 | 结果 |
|---:|---|---|---|---|---|
| 1 | API 文档保护中间件已实现 | 读取 app/main.py | 生产环境 /docs /redoc /openapi.json 返回 404 | 中间件存在 | PASS |
| 2 | API 文档生产 404 | test_docs_blocked_in_production_env ×3 | 404 | 404 | PASS |
| 3 | API 文档非生产可访问 | test_docs_accessible_in_test_env ×3 | 200 | 200 | PASS |
| 4 | 生产健康/登录仍可用 | test_health_and_login_still_work_in_production | 200 | 200 | PASS |
| 5 | 未登录写被拒 | test_unauthenticated_write_is_rejected | 401/403 | 401/403 | PASS |
| 6 | viewer 不可写/复核 | test_viewer_cannot_call_write_or_review | 403 | 403 | PASS |
| 7 | reviewer 可复核、不可管用户 | test_reviewer_can_review_but_not_manage_users | 复核200/建用户403 | 一致 | PASS |
| 8 | admin 可建用户且审计 | test_admin_can_create_user_and_audit_recorded | 200 + 审计行 | 一致 | PASS |
| 9 | CSRF 强制 | test_csrf_required_on_cookie_writes | 403 + CSRF | 一致 | PASS |
| 10 | 健康不暴露 DB 路径 | test_health_hides_database_path | "configured" | "configured" | PASS |
| 11 | operator 边界 | test_operator_can_write_jobs_but_not_review | 读200/复核403/建用户403 | 一致 | PASS |
| 12 | auditor 边界 | test_auditor_can_read_audits_but_not_write_or_review | 审计200/建任务403/复核403 | 一致 | PASS |
| 13 | viewer 不可读审计 | test_viewer_cannot_read_audits | 403 | 403 | PASS |
| 14 | 角色能力合约锁定 | test_role_capability_matrix_contract | 合约匹配 | 匹配 | PASS |
| 15 | UI shell 与静态可达 | test_ui_shell_and_static_reachable | 200 | 200 | PASS |
| 16 | production 禁止 POLICY_AUTH_DISABLED | test_auth_fail_closed_production_forbids_disabled | ConfigurationError | 抛错 | PASS |
| 17 | 生产误配拒绝启动 | test_production_misconfig_refuses_app_startup | ConfigurationError | 抛错 | PASS |
| 18 | finding 状态迁移+复核只追加 | test_finding_state_migration_and_review_append | 2 条复核、confirmed | 一致 | PASS |
| 19 | 重扫继承人工结论 | test_human_conclusion_inherited_on_resave | human_dismissed 保留 | 一致 | PASS |
| 20 | 普委办41号 rule_version 非空 | test_puweiban_2013_41... | rule_version 非空 | 非空 | PASS |
| 21 | 普委办41号 evidence_fields 含联合发文机关 | test_puweiban_2013_41... | 含字段 | 含 | PASS |
| 22 | AGENCY-001 不进正式问题 | test_agency_001_not_customer_formal | allow_customer_output=False | False | PASS |
| 23 | ORG-002 hint 不进正式问题 | test_puweiban_2013_41... | allow_customer_output=False | False | PASS |
| 24 | 普宜办匹配 | test_street_office_and_historical_alias_symbols | 无 ORG-001 | 无 | PASS |
| 25 | 长街办匹配 | test_street_office_and_historical_alias_symbols | 无 ORG-001 | 无 | PASS |
| 26 | 普委办转发仍通过 | test_puweiban_and_street_still_pass | 无误报 | 无 | PASS |
| 27 | 全量测试 | pytest -q | ≥184 | 196 passed | PASS |
| 28 | 生产数据库 SHA256 未变 | Get-FileHash | `13e6a8...77af3` | `13E6A8...77AF3` | PASS |
| 29 | 未 push | 命令历史 | 未执行 | 未执行 | PASS |
| 30 | 未合并 main | 命令历史 | 未执行 | 未执行 | PASS |
| 31 | 未重启生产服务 | 命令历史 | 未执行 | 未执行 | PASS |

## 三、生产边界确认

| 边界 | status.json 字段 | 值 |
|---|---|---|
| 生产数据库被修改 | `production_database_modified` | `false` |
| 生产服务被重启 | `production_service_restarted` | `false` |
| 生产 Nginx 被修改 | `production_nginx_modified` | `false` |
| 生产 PM2 被修改 | `production_pm2_modified` | `false` |
| 生产证书被修改 | `production_certificates_modified` | `false` |
| 高频外部扫描 | `high_frequency_external_scanning` | `false` |

## 四、阻断项

无。

## 五、遗留说明

- 真实浏览器端到端验证留待 M7 生产切换准备（M0～M6 不重启生产服务）。
- Windows PytestCache 权限告警不影响测试结果。

## 六、自验结论

**M1 自验 PASS。**

全部门禁达成，生产边界严守，196 测试通过，未触及生产。自动进入 M2。
