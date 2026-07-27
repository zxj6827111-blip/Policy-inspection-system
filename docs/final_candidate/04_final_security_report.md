# 04 最终安全报告

> 版本号：V3.0
> 生成日期：2026-07-27
> 主开发 AI 自验产出，待独立总验收
> 当前分支：feat/full-policy-inspection-upgrade
> 当前 HEAD：02cb6e22341e2940abb0e657d27b9ba2c7b2cae9
> 工作区数据库 SHA256：5F35DE29396143B2F1EA5BE328505D37180A2AC2686898357D4BC309E76398F8
> 生产数据库 SHA256（未变）：13E6A8A412F69C13D149674A31076854C966F5B151E670FC9E22D0F53AB77AF3

---

## 1. 执行摘要

本报告汇总 Policy-inspection-system（上海区级政策文件安全巡检系统）在 M0-M7 阶段实施的安全改造范围、最终安全态势以及生产边界保持情况。M0-M6 全部自验 PASS，M7 全系统回归 347 passed / 0 failed，M7 最终 1,500 条评测样本在 74 条规则上取得 Precision=1.0000、Recall=1.0000、F1=1.0000，0 未达标规则，0 降级候选。

安全改造覆盖以下核心领域：

| 领域 | 关键内容 | 状态 |
| --- | --- | --- |
| 身份认证 | 会话管理、登录、登出、密码哈希 | 完成 |
| 授权（RBAC） | 5 角色能力矩阵、能力直通 | 完成 |
| CSRF 防护 | 三段一致性校验、仅写类端点强制 | 完成 |
| 密码安全 | pbkdf2_sha256、390,000 迭代、恒定时间比较 | 完成 |
| 审计日志 | operation_audits 全字段记录 | 完成 |
| API 文档保护 | 生产环境 /docs /redoc /openapi.json 返 404 | 完成 |
| Fail-Closed 配置 | APP_ENV 默认 PRODUCTION、互斥校验、运行期开关 | 完成 |
| 数据完整性 | WAL 模式、外键启用、SHA256 校验 | 完成 |

生产边界保持：M0-M6 全程未修改生产数据库（SHA256 未变）、未重启生产服务、未修改 Nginx/PM2/证书。所有改造均在工作区数据库上完成。

---

## 2. 身份认证

### 2.1 会话管理

| 属性 | 取值 |
| --- | --- |
| Cookie 名称 | `pis_session` |
| HttpOnly | 是 |
| SameSite | `Lax` |
| 有效期 | 7 天 |
| Secure | 取决于 `POLICY_COOKIE_SECURE`（默认 False） |
| 路径 | `/` |

会话在服务端存储于 `sessions` 表，记录 `user_id`、`csrf_token`、`created_at`、`expires_at`、`revoked` 等字段。Cookie 仅承载会话 ID，不携带任何业务数据。

### 2.2 会话 ID 生成

- 算法：`secrets.token_urlsafe(32)`
- 长度：43 字符 URL-safe Base64
- 熵：256 bit
- 用途：登录成功后写入 `pis_session` Cookie 并落库

### 2.3 登出机制

| 步骤 | 行为 |
| --- | --- |
| 1 | 调用 `revoke_session(session_id)`，置 `sessions.revoked = 1` |
| 2 | 不物理删除行，保留审计轨迹 |
| 3 | 同时调用 `delete_cookie('pis_session')` 清除客户端 Cookie |
| 4 | 同步清除 `pis_csrf` Cookie |

登出后所有后续请求携带的会话 ID 均被识别为已撤销，立即返 401。

---

## 3. 授权（RBAC）

### 3.1 5 角色能力矩阵

| 角色 | 能力集合 |
| --- | --- |
| admin | `jobs:write`, `review:write`, `export:read`, `users:admin`, `audit:read`, `read` |
| operator | `jobs:write`, `export:read`, `read` |
| reviewer | `review:write`, `export:read`, `read` |
| viewer | `read`, `export:read` |
| auditor | `read`, `audit:read`, `export:read` |

### 3.2 依赖函数

| 函数 | 职责 |
| --- | --- |
| `require_login` | 校验会话有效且未撤销，未登录返 401 |
| `require_roles(*roles)` | 校验当前用户角色属于给定集合，否则 403 |
| `require_capability(cap)` | 校验当前用户拥有给定能力，否则 403 |
| `require_write` | 等价于同时校验登录 + 写类能力 + CSRF（参见第 4 节） |

### 3.3 admin 直通

admin 角色永远直通所有能力校验：在 `require_capability` 与 `require_roles` 内部判定中，若当前用户角色为 admin，则直接放行，不需要逐项匹配能力集合。该规则确保 admin 在运维场景下的可操作性，同时其所有操作仍被审计日志完整记录。

---

## 4. CSRF 防护

### 4.1 三段一致性校验

CSRF 校验在 `require_write` 装饰器内部执行，要求三段令牌完全一致：

| 段 | 来源 |
| --- | --- |
| 段 1 | 请求头 `X-CSRF-Token` |
| 段 2 | Cookie `pis_csrf` |
| 段 3 | 服务端 `session.csrf_token` |

### 4.2 校验规则

| 条件 | 行为 |
| --- | --- |
| 三段任一为空 | 拒绝，返 403 |
| 三段完全一致 | 通过 |
| 三段不一致 | 拒绝，返 403 |

### 4.3 适用范围

- 仅 `require_write` 类端点强制校验（HTTP 方法为 POST/PUT/DELETE 的端点）
- GET / HEAD / OPTIONS 等读类端点不强制 CSRF
- 仅在写类端点入口处执行，避免对静态资源与读接口的额外开销

### 4.4 测试豁免

当 `is_auth_disabled()` 返回 True 时跳过 CSRF 校验。`is_auth_disabled()` 仅在 `APP_ENV=test` 时为 True，生产环境永不豁免。

---

## 5. 密码安全

### 5.1 算法参数

| 参数 | 取值 |
| --- | --- |
| 算法 | `pbkdf2_sha256` |
| 迭代次数 | 390,000 |
| 盐 | `secrets.token_hex(16)`（32 hex 字符） |
| 摘要长度 | 256 bit |
| 存储格式 | `pbkdf2_sha256${iterations}${salt}${digest}` |

### 5.2 密码校验

| 校验项 | 规则 |
| --- | --- |
| 长度 | >= 8 位 |
| 比较 | `hmac.compare_digest`（恒定时间比较，防时序攻击） |
| 盐生成 | 每个密码独立盐，不复用 |
| 迭代次数 | 与 OWASP 2023 推荐值对齐 |

### 5.3 存储与验证流程

1. 用户注册或改密时，生成 32 hex 字符盐
2. 使用 `pbkdf2_sha256` 以 390,000 迭代计算摘要
3. 拼接为 `pbkdf2_sha256${iterations}${salt}${digest}` 字符串存入 `users.password_hash`
4. 登录验证时解析存储字符串，重算摘要并以 `hmac.compare_digest` 比较

---

## 6. 审计日志

### 6.1 operation_audits 表字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | INTEGER PK | 自增主键 |
| `actor_user_id` | INTEGER | 操作者用户 ID |
| `actor_username` | VARCHAR | 操作者用户名 |
| `action` | VARCHAR | 操作类型（如 login、logout、create_job、review、export、user_create、user_update 等） |
| `resource_type` | VARCHAR | 资源类型（如 job、finding、user、rule、backup 等） |
| `resource_id` | VARCHAR | 资源 ID |
| `before_json` | TEXT | 操作前状态 JSON |
| `after_json` | TEXT | 操作后状态 JSON |
| `detail_json` | TEXT | 操作详情 JSON |
| `ip` | VARCHAR | 请求来源 IP |
| `user_agent` | VARCHAR | 请求 User-Agent |
| `created_at` | DATETIME | 记录时间 |

### 6.2 记录范围

所有敏感操作均记录至 `operation_audits`，包括但不限于：

- 登录 / 登出 / 登录失败
- 作业创建 / 启动 / 删除
- 规则配置变更 / 规则版本发布
- 复核与仲裁操作
- 整改任务状态变更
- 用户创建 / 角色变更 / 密码重置
- 数据导出（Excel、备份下载等）
- 系统设置变更（如 metrics_enabled 切换）

---

## 7. API 文档保护

生产环境通过 `protect_api_docs` 中间件保护 API 文档端点：

| 路径 | 生产环境响应 | 非生产环境响应 |
| --- | --- | --- |
| `/docs` | 404 | Swagger UI |
| `/redoc` | 404 | ReDoc |
| `/openapi.json` | 404 | OpenAPI Schema |

判定逻辑：当 `APP_ENV` 为 `production`（或未设置，默认按 PRODUCTION 处理）时，所有 API 文档路径均返 404；仅 `APP_ENV=dev` / `test` 时开放。该中间件在路由注册之前生效，确保无任何文档元数据泄露。

---

## 8. Fail-Closed 配置

### 8.1 环境变量默认值

| 变量 | 未设置时默认 | 说明 |
| --- | --- | --- |
| `APP_ENV` | `production` | 应用环境，未设置即按生产处理 |
| `POLICY_AUTH_DISABLED` | `0` | 是否关闭鉴权，仅在 `APP_ENV=test` 生效 |
| `POLICY_COOKIE_SECURE` | `False` | 是否启用 Cookie Secure 标记 |
| `system_settings.metrics_enabled` | 由数据库配置决定 | `/metrics` 端点开关 |

### 8.2 互斥校验

启动期配置校验逻辑：

| 校验 | 行为 |
| --- | --- |
| `APP_ENV=production` 且 `POLICY_AUTH_DISABLED=1` | 抛 `ConfigurationError`，拒绝启动 |
| `APP_ENV` 未设置且 `POLICY_AUTH_DISABLED=1` | 同上，按 PRODUCTION 处理后拒绝启动 |
| `APP_ENV=test` 且 `POLICY_AUTH_DISABLED=1` | 允许，仅用于自动化测试 |
| `APP_ENV=dev` 且 `POLICY_AUTH_DISABLED=1` | 允许，仅用于本地开发 |

### 8.3 运行期开关

| 端点 | 控制项 | 关闭时响应 |
| --- | --- | --- |
| `/metrics` | `system_settings.metrics_enabled = 0` | 404 |
| `/health/deep` | 探针结果 unhealthy | 503 |
| `/health` | 始终开放 | 200（仅 liveness） |

`/health/deep` 在数据库连接异常、关键依赖不可用等场景下返 503，便于反向代理与健康检查探针剔除故障实例。

---

## 9. 数据完整性

### 9.1 SQLite 配置

| 配置项 | 取值 | 说明 |
| --- | --- | --- |
| `journal_mode` | `WAL` | Write-Ahead Logging，支持并发读 |
| `synchronous` | `NORMAL` | WAL 模式下的安全/性能平衡点 |
| `foreign_keys` | `ON` | 启用外键约束 |
| `busy_timeout` | `30000` ms | 锁等待 30 秒 |

### 9.2 备份恢复校验

| 操作 | 校验机制 |
| --- | --- |
| 在线备份 | 备份文件 SHA256 计算 + 与源库 SHA256 比对 |
| 恢复 | 恢复后 `PRAGMA integrity_check` + `PRAGMA foreign_key_check` |
| 不可逆操作 | 执行前 `dry_run` 校验，输出影响范围预览 |
| 跨版本迁移 | 迁移前后行数比对 + 关键表抽检 |

### 9.3 事务与并发

- 写操作均包裹在事务内，失败回滚
- WAL 模式下读不阻塞写、写不阻塞读
- `busy_timeout=30000ms` 避免短时锁争用直接报错

---

## 10. 生产边界保持证据

M0-M6 全程严格保持生产边界，证据如下：

| 维度 | 证据 |
| --- | --- |
| 生产数据库 | SHA256 全程未变：`13E6A8A412F69C13D149674A31076854C966F5B151E670FC9E22D0F53AB77AF3` |
| 工作区数据库 | SHA256：`5F35DE29396143B2F1EA5BE328505D37180A2AC2686898357D4BC309E76398F8` |
| 生产服务 | 未重启、未停机 |
| Nginx 配置 | 未修改 |
| PM2 进程 | 未修改、未重启 |
| TLS 证书 | 未修改、未续签 |
| 生产环境变量 | 未修改 |

所有改造、测试、演练均在工作区数据库与本地服务实例上完成，生产环境零影响。

---

## 11. 遗留风险与缓解

| 风险项 | 描述 | 缓解措施 |
| --- | --- | --- |
| POLICY_COOKIE_SECURE 默认 False | 默认配置下 Cookie 未带 Secure 标记，HTTP 传输可能被劫持 | 生产部署时必须显式设置 `POLICY_COOKIE_SECURE=True`，已写入部署 Runbook |
| 无频次限制 | 登录、API 调用未做 rate-limit，可能遭受暴力破解或 DoS | 建议未来在反向代理层（Nginx limit_req）或应用层加 rate-limit 中间件 |
| 备份保留期 | 默认 30 天，超期自动清理 | 备份保留期可通过配置项调整，建议根据合规要求评估 |
| 文件上传校验 | 已做 MIME 与扩展名校验，但未做深度内容扫描 | 建议未来引入 ClamAV 或类似反病毒扫描 |
| 会话固定 | 登录后会话 ID 已轮换，但未做 IP 绑定 | 建议未来在敏感操作时增加 IP 一致性校验 |

---

## 12. 结论

M0-M7 安全改造已按计划完成，覆盖身份认证、RBAC 授权、CSRF 防护、密码安全、审计日志、API 文档保护、Fail-Closed 配置、数据完整性等核心领域。M0-M6 全部自验 PASS，M7 全系统回归 347 passed / 0 failed。生产边界严格保持，生产数据库 SHA256 全程未变，未重启生产服务，未修改 Nginx/PM2/证书。

遗留风险已识别并给出缓解建议，其中 `POLICY_COOKIE_SECURE` 必须在生产部署前显式设置为 True。本报告为主开发 AI 自验产出，待独立总验收。
