# M7 生产观察清单（Observation Checklist）

> 版本：V3.0
> 生成日期：2026-07-27
> 主开发 AI 自验产出，待独立总验收

本清单用于生产切换后的观察期（建议 24-72 小时）。每项均需人工或脚本核对，发现异常立即按 `09_rollback_runbook.md` 触发回滚评估。

## 1. 健康检查（每 15 分钟，切换后 1 小时内）

| 项 | 端点 / 命令 | 期望 | 异常处理 |
|---|---|---|---|
| 基础健康 | `GET /health` | 200, `status=ok` | 立即排查 |
| 深度健康 | `GET /health/deep` | 200, `status=healthy` | unhealthy → 评估回滚 |
| 数据库完整性 | `PRAGMA integrity_check` | `ok` | 立即回滚 |
| WAL 大小 | `data/policy_inspector.db-wal` | < 100 MB | 过大 → 手动 checkpoint |
| 服务进程 | `pm2 list` | app 状态 `online` | 重启服务 |
| 磁盘空间 | `df -h .` | 可用 > 30% | 立即清理 |

## 2. 性能与队列（每 30 分钟）

| 项 | 端点 / 命令 | 期望 | 异常处理 |
|---|---|---|---|
| /metrics | `GET /metrics` | 200, JSON | 不可达 → 排查 |
| OCR 队列深度 | `GET /api/ocr-queue/depth` | pending < 100 | 积压 → 启动 worker |
| 请求 P99 延迟 | 日志 `duration_ms` | P99 < 5000 ms | 飙升 → 检查锁/索引 |
| 5xx 错误率 | 日志 `status_code >= 500` | 0 / 小时 | 飙升 → 评估回滚 |
| 4xx 错误率 | 日志 `status_code >= 400` | < 50 / 小时 | 飙升 → 排查鉴权/CSRF |
| 数据库锁等待 | 日志 `database is locked` | 0 / 小时 | 飙升 → 检查 busy_timeout |

## 3. 业务功能（每 1 小时）

| 项 | 端点 / 命令 | 期望 | 异常处理 |
|---|---|---|---|
| 登录 | `POST /api/auth/login` | 200, `ok=true` | 不可登录 → 紧急回滚 |
| 21 项导航 | `GET /api/nav-items` | 21 项（admin） | 缺失 → 排查角色映射 |
| 全局总览 | `GET /api/overview` | 200, JSON | 失败 → 排查聚合查询 |
| 审计日志 | `GET /api/audits?limit=5` | 200, list | 失败 → 排查鉴权 |
| 系统设置 | `GET /api/system-settings` | 200, list | 失败 → 排查鉴权 |

## 4. 备份与恢复（切换后 24 小时）

| 项 | 命令 | 期望 | 异常处理 |
|---|---|---|---|
| 切换前备份存在 | `python -c "from app.backup import list_backups; print(list_backups('data/backups'))"` | 至少 1 项 `pre_cutover` | 缺失 → 紧急备份 |
| 备份 SHA256 校验 | `*.sha256` sidecar | 一致 | 不一致 → 重新备份 |
| 备份完整性 | `validate_backup(path)` | `valid=true` | 失败 → 重新备份 |
| 备份目录大小 | `du -sh data/backups` | < 1 GB | 过大 → prune |

## 5. 安全（每 2 小时）

| 项 | 端点 / 命令 | 期望 | 异常处理 |
|---|---|---|---|
| /docs 在生产隐藏 | `GET /docs` | 404 | 200 → 紧急排查 |
| /openapi.json 隐藏 | `GET /openapi.json` | 404 | 200 → 紧急排查 |
| /redoc 隐藏 | `GET /redoc` | 404 | 200 → 紧急排查 |
| 未登录访问受保护 | `GET /api/users` | 401 | 200 → 紧急回滚 |
| CSRF 校验生效 | `POST /api/users`（无 token） | 403 | 200 → 紧急回滚 |
| 审计日志新增 | `SELECT COUNT(*) FROM operation_audits` | 切换后 > 0 | 0 → 排查审计 |

## 6. 数据库锁治理（每 30 分钟）

| 项 | 命令 | 期望 | 异常处理 |
|---|---|---|---|
| 锁等待日志 | 日志 `database is locked` | 0 / 30 分钟 | 飙升 → 检查长事务 |
| WAL checkpoint | `PRAGMA wal_checkpoint(PASSIVE)` | `0`（成功） | 失败 → 手动 TRUNCATE |
| freelist_count | `PRAGMA freelist_count` | < 1000 | 过大 → VACUUM |
| page_count | `PRAGMA page_count` | 稳定增长 | 暴增 → 排查写入 |

## 7. 日志监控（持续）

| 项 | 命令 | 期望 | 异常处理 |
|---|---|---|---|
| ERROR 日志 | `grep -i error logs/*.log` | 0 / 小时 | 飙升 → 排查 |
| WARNING 日志 | `grep -i warning logs/*.log` | < 10 / 小时 | 飙升 → 排查 |
| request_id 完整 | 日志含 `request_id` | 全部有 | 缺失 → 排查中间件 |
| X-Request-ID 响应头 | `curl -I` | 存在 | 缺失 → 排查中间件 |

## 8. 业务连续性（每 4 小时）

| 项 | 端点 / 命令 | 期望 | 异常处理 |
|---|---|---|---|
| 扫描任务运行 | `GET /api/jobs` | 切换后任务正常推进 | 停滞 → 排查 |
| 连续调度 | `GET /api/continuous-schedules` | enabled=true | disabled → 排查 |
| 规则效果 | `GET /api/rule-effect` | 200, JSON | 失败 → 排查 |
| 整改任务 | `GET /api/rectification/tasks` | 200, list | 失败 → 排查 |

## 9. 观察期结论模板

观察期结束后（建议 72 小时），填写：

- 观察期起止：____ 至 ____
- 健康检查通过率：____ / ____
- 性能指标异常次数：____
- 业务功能异常次数：____
- 安全事件次数：____
- 数据库锁事件次数：____
- ERROR 日志数：____
- 结论：□ 持续稳定运行 □ 发现异常已修复 □ 发现严重问题已回滚

## 10. 异常上报路径

1. 立即记录到 `docs/program_control/risk_register.md`
2. 严重问题立即通知用户并启动回滚评估
3. 一般问题 24 小时内修复并重新自验
4. 所有异常均需在 `docs/program_control/change_log.md` 记录修复过程
