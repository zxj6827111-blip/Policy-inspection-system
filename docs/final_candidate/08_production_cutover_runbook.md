# 08 生产切换 Runbook（候选方案）

> 版本号：V3.0
> 生成日期：2026-07-27
> 状态：主开发 AI 自验产出，待独立总验收
> 适用分支：`feat/full-policy-inspection-upgrade`（HEAD：`02cb6e22341e2940abb0e657d27b9ba2c7b2cae9`）
> 生产基线分支：`codex/fix-snapshot-result-collision`（HEAD：`be74df72598ae9ae0feb3a8fd9987c84c5bd8b67`）

---

## 1. 执行摘要

本 Runbook 描述上海区级政策文件安全巡检系统从当前生产基线（`codex/fix-snapshot-result-collision`）切换到全量升级版本（`feat/full-policy-inspection-upgrade`）的完整流程。

M7 阶段的产出是"可发布候选版本"与"完整生产切换准备"，**不直接切生产**，除非用户已明确授权生产切换。

在执行本 Runbook 前，须确认以下里程碑均已自验通过：

| 里程碑 | 状态 | 关键证据 |
|--------|------|----------|
| M0-M6 | PASS | 各阶段 `self_acceptance_report.md` |
| M7 全系统回归 | 347 passed / 0 failed | `pytest tests/ -q` |
| M7 最终 1,500 条评测 | 74 条规则，P=R=F1=1.0000 | `scripts/final_1500_evaluation.py` |
| M6-8 生产副本全流程演练 | 10/10 PASS | `scripts/rehearsal_backup_restore.py` |
| 工作区数据库 SHA256 | 5F35DE29396143B2F1EA5BE328505D37180A2AC2686898357D4BC309E76398F8 | `data/policy_inspector.db` |
| 生产数据库 SHA256 | 13E6A8A412F69C13D149674A31076854C966F5B151E670FC9E22D0F53AB77AF3 | 未变更 |

本 Runbook 是候选方案。实际切换须由用户授权后，在维护窗口内由具备生产权限的运维人员执行。

---

## 2. 前置条件

以下条件**必须全部满足**才能执行切换，任一未满足则不得进入切换流程：

1. 用户已明确授权生产切换（书面或对话授权记录）。
2. 备份完成：`backup_database` 执行成功，输出文件 SHA256 已记录并通过 `integrity_check`。
3. 回滚演练通过：`scripts/rehearsal_backup_restore.py` 输出 10/10 PASS。
4. 维护窗口已确认，建议周末 02:00-06:00，避开月底/月初业务高峰。
5. 全部 M0-M7 自验通过，回归测试 347 passed / 0 failed。
6. 最终 1,500 条评测 PASS：74 条规则，Precision=Recall=F1=1.0000。
7. 生产环境具备 Python 运行时、PM2、nginx（或等价进程与反向代理）。
8. 备份模块 `app/backup/__init__.py` 提供 `backup_database` / `restore_database` / `validate_backup` / `list_backups` / `prune_old_backups` / `rollback_to_pre_migration` 六个函数，已通过自验。
9. **非 root 服务用户已创建**（如 `appuser` 或 `policyapp`），且部署目录属主已切换（见 §10.1）。
10. **TLS 证书已就绪**：`/etc/ssl/certs/policy-inspect.crt` 与 `/etc/ssl/private/policy-inspect.key` 可读，且距过期不少于 30 天。
11. **Nginx 反向代理配置已就位**：80→443 重定向、HSTS、`proxy_pass http://127.0.0.1:8000`（或 6001，见 §10.3）。
12. **防火墙/安全组规则已生效**：仅放行 22/80/443，6001 与 8000 严禁公网放行（见 §10.4）。
13. **`POLICY_AUTH_DISABLED` 未设置或为 0**：生产环境禁止绕过认证。

### 2.1 前置自检命令

| 检查项 | 命令 |
|--------|------|
| 当前分支 | `git rev-parse --abbrev-ref HEAD` |
| 当前 HEAD | `git rev-parse HEAD` |
| 回归测试 | `python -m pytest tests/ -q --tb=short` |
| 1500 评测 | `python scripts/final_1500_evaluation.py` |
| 备份演练 | `python scripts/rehearsal_backup_restore.py --source-db data/policy_inspector.db --tmp-dir tmp/rehearsal` |
| 备份目录可写 | `python -c "import os; os.makedirs('data/backups', exist_ok=True); print('OK')"` |
| 服务用户存在 | `id appuser`（或 `id policyapp`） |
| 部署目录属主 | `stat -c '%U:%G' /opt/policy-inspector` |
| 应用未绑定公网 | `ss -ltnp \| grep -E ':8001\|:6001\|:8000'`（应只见 127.0.0.1） |
| Nginx 配置语法 | `nginx -t` |
| TLS 证书有效期 | `openssl x509 -in /etc/ssl/certs/policy-inspect.crt -noout -enddate` |
| 防火墙规则 | `ufw status numbered`（或 `iptables -L -n`） |
| `.env` 权限 | `stat -c '%a %U' .env`（应 600 且属主为服务用户） |
| DB 文件权限 | `stat -c '%a %U' data/policy_inspector.db`（应 600 且属主为服务用户） |
| `POLICY_AUTH_DISABLED` 未设 | `env \| grep POLICY_AUTH_DISABLED`（应无输出） |

---

## 3. 切换前检查清单

执行切换前，逐项确认以下清单。任一项未勾选则**暂停切换**并上报：

- [ ] 用户授权记录已归档（含授权时间、授权人、授权范围）
- [ ] 维护窗口公告已发布（至少提前 24 小时）
- [ ] 生产数据库 SHA256（变更前）已记录
- [ ] 生产服务运行状态正常：`pm2 list`、`nginx -t`
- [ ] 备份目录可写：`data/backups/`
- [ ] 磁盘剩余空间足够（至少 3 倍生产数据库大小）
- [ ] 回滚负责人在线，可 30 分钟内响应
- [ ] DBA 在线（如涉及数据库异常处置）
- [ ] 开发负责人在线（如涉及代码异常处置）
- [ ] 切换前备份脚本演练 10/10 PASS（最近 7 天内）
- [ ] 冒烟脚本 `scripts/smoke_test.py` 在生产基线版本可 15/15 PASS（作为对照基线）
- [ ] 应急通信渠道可用（IM 群、电话）
- [ ] 切换全程录屏或操作日志已开启
- [ ] 服务以非 root 用户（`appuser`/`policyapp`）运行，部署目录属主已切换（§10.1）
- [ ] Nginx 80→443 重定向、HSTS、TLS 证书有效（§10.2）
- [ ] 应用端口仅绑定 `127.0.0.1`，公网仅 80/443，`0.0.0.0:6001`/`0.0.0.0:8000` 已禁止（§10.3）
- [ ] 防火墙/安全组仅放行 22/80/443（§10.4）
- [ ] `.env` 与 `data/policy_inspector.db` 权限 600、属主为服务用户（§10.5）
- [ ] logrotate 已配置（§10.6）
- [ ] 进程监管（systemd/PM2）以非 root 用户启动（§10.7）
- [ ] `POLICY_AUTH_DISABLED` 未设置或为 0

---

## 4. 切换步骤

按以下顺序执行，**每一步必须 PASS 才能继续下一步**。任何一步失败，立即转入 `09_rollback_runbook.md`。

### 步骤 1：通知用户进入维护窗口

在 IM 群、邮件、系统公告三个渠道发布维护窗口开始通知，包含：

- 维护开始时间、预计结束时间
- 影响范围（系统不可用、数据只读等）
- 回滚预案要点
- 应急联系方式

等待至少 5 分钟确认用户已收到通知后再执行下一步。

### 步骤 2：停止写入流量

任选一种方式停止写入流量，**只读流量可暂时保留**：

```bash
# 方式 A：PM2 停止应用（完全停服）
pm2 stop app

# 方式 B：nginx 切换到维护页（保留只读静态页）
nginx -s reload  # 已预先配置 maintenance 页面路由
```

确认写入流量已停止：

```bash
pm2 status       # 应用状态应为 stopped
tail -f logs/access.log  # 不应再出现 POST/PUT/DELETE 请求
```

### 步骤 3：记录生产数据库 SHA256（变更前）

```bash
python -c "import hashlib; print(hashlib.sha256(open('data/policy_inspector.db','rb').read()).hexdigest().upper())"
```

将输出值记录到切换日志，字段名 `pre_cutover_sha256`。

预期值（如生产数据库自上次记录未变）：`13E6A8A412F69C13D149674A31076854C966F5B151E670FC9E22D0F53AB77AF3`。

> 若实际值与预期值不一致，**暂停切换**，调查数据库是否已被修改。

### 步骤 4：执行 backup_database 并校验

```bash
python -c "from app.db import Database; from app.backup import backup_database; backup_database(Database(), 'data/backups', label='pre_cutover')"
```

校验备份：

```bash
# 校验 SHA256
python -c "import hashlib; print(hashlib.sha256(open('data/backups/policy_inspector_pre_cutover_XXX.db','rb').read()).hexdigest().upper())"

# 校验完整性（必须输出 'ok'）
python -c "from app.backup import validate_backup; print(validate_backup('data/backups/policy_inspector_pre_cutover_XXX.db'))"
```

通过条件：

- SHA256 已记录
- `validate_backup` 返回 `True` 或 `integrity_check` 输出 `ok`

### 步骤 5：拉取最新代码到生产

```bash
# 进入生产代码目录
cd /opt/policy-inspector  # 视实际部署路径

# 拉取并切换到升级分支
git fetch origin
git checkout feat/full-policy-inspection-upgrade
git pull origin feat/full-policy-inspection-upgrade

# 确认 HEAD
git rev-parse HEAD
# 预期：02cb6e22341e2940abb0e657d27b9ba2c7b2cae9
```

### 步骤 6：安装新依赖

```bash
# 建议在虚拟环境中执行
source .venv/bin/activate  # Linux
# 或 .venv\Scripts\activate  # Windows

pip install -r requirements.txt
```

如有依赖冲突，记录日志并转入回滚流程。

### 步骤 7：设置生产环境变量

```bash
export APP_ENV=production
export POLICY_COOKIE_SECURE=1

# 启动服务时务必带上（应用只绑定 127.0.0.1，公网由 Nginx 80/443 代理）：
# APP_ENV=production POLICY_COOKIE_SECURE=1 uvicorn app.main:app --host 127.0.0.1 --port 8000
```

确认 `.env` 文件（如有）也配置了相同项，避免重启后失效。

> ⚠️ **禁止** `--host 0.0.0.0` 直接公网暴露应用端口。完整端口收口、non-root 用户、HTTPS、防火墙等要求见 **第 10 章 安全与运维加固**。

### 步骤 8：执行 Database.initialize()（应用所有迁移）

```bash
python -c "from app.db import Database; db = Database(); db.initialize(); print('migrations applied')"
```

迁移执行顺序（`Database.initialize()` 自动按序执行，幂等）：

```
phase1 → m2 → m3 → m4 → m5 → m6
```

幂等性保证：已应用的迁移不会重复执行。

### 步骤 9：校验迁移结果

```bash
# 1. 检查 schema_migrations 表
python -c "
from app.db import Database
db = Database()
rows = db.execute('SELECT name, applied_at FROM schema_migrations ORDER BY applied_at').fetchall()
for r in rows: print(r)
"

# 2. 关键表计数（与切换前对比）
python -c "
from app.db import Database
db = Database()
for t in ['users','policies','rules','inspections','audit_logs']:
    c = db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
    print(f'{t}: {c}')
"

# 3. integrity_check
python -c "
from app.db import Database
db = Database()
print(db.execute('PRAGMA integrity_check').fetchone()[0])
"
```

通过条件：

- `schema_migrations` 表包含 phase1、m2、m3、m4、m5、m6 全部记录
- 关键表计数与切换前一致（除迁移引入的预期变化外）
- `integrity_check` 输出 `ok`

### 步骤 10：记录生产数据库 SHA256（变更后）

```bash
python -c "import hashlib; print(hashlib.sha256(open('data/policy_inspector.db','rb').read()).hexdigest().upper())"
```

将输出值记录到切换日志，字段名 `post_cutover_sha256`。

> 该值应与 `pre_cutover_sha256` **不同**（迁移会修改 schema）。

### 步骤 11：启动服务

> ⚠️ 必须以 `appuser` 启动（见 §10.1、§10.7），应用仅绑定 `127.0.0.1`（见 §10.3）。禁止 root 启动、禁止 `--host 0.0.0.0`。

```bash
# 切换到服务用户（如尚未切换）
sudo -u appuser -i
cd /opt/policy-inspector

# 方式 A：systemd（推荐，见 §10.7 配置）
sudo systemctl start policy-inspector

# 方式 B：PM2（必须先 sudo -u appuser）
APP_ENV=production POLICY_COOKIE_SECURE=1 pm2 start "uvicorn app.main:app --host 127.0.0.1 --port 8000" --name policy-inspector --env production

# 方式 C：临时前台启动（仅排错用，不得用于长期运行）
APP_ENV=production POLICY_COOKIE_SECURE=1 uvicorn app.main:app --host 127.0.0.1 --port 8000
```

确认服务已启动：

```bash
systemctl status policy-inspector   # 或 pm2 status
ps -eo user,cmd | grep uvicorn      # USER 列必须为 appuser
curl -s http://127.0.0.1:8000/health
```

### 步骤 12：运行冒烟脚本

```bash
python scripts/smoke_test.py --base-url http://localhost:8000 --admin-user admin --admin-pass 'PASSWORD'
```

通过条件：**15/15 PASS**。

任一步骤失败，立即转入回滚流程。

### 步骤 13：恢复写入流量

```bash
# 方式 A：PM2 已在步骤 11 启动，无需额外操作
# 方式 B：nginx 恢复
nginx -s reload  # 移除 maintenance 路由
```

确认写入流量已恢复：

```bash
tail -f logs/access.log  # 应能看到 POST/PUT/DELETE 请求
```

### 步骤 14：观察 1 小时

观察以下指标至少 60 分钟：

- `/health` 与 `/health/deep` 每 5 分钟检查一次
- `/metrics` 每 15 分钟检查一次
- `logs/error.log` 持续监控，不应出现 ERROR 级别日志
- `logs/access.log` 监控 5xx 错误率，应 < 0.1%
- PM2 内存与 CPU 使用率正常

观察期内任一异常，立即转入回滚流程。

---

## 5. 切换后验证

切换完成后，逐项验证：

| 验证项 | 期望结果 | 检查命令 |
|--------|----------|----------|
| `/health` | `status=ok` | `curl -s http://localhost:8000/health` |
| `/health/deep` | `healthy` | `curl -s http://localhost:8000/health/deep` |
| `/docs` | 404 | `curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/docs` |
| `/metrics` | 200 + metrics 文本 | `curl -s http://localhost:8000/metrics` |
| 登录功能 | 200 + token | 浏览器或 curl POST `/api/auth/login` |
| 21 项导航可见（admin 角色） | 21 个菜单项 | 浏览器登录后查看 |
| 审计日志记录切换操作 | 含 cutover 相关日志 | `SELECT * FROM audit_logs WHERE action LIKE '%cutover%' ORDER BY created_at DESC LIMIT 10` |

### 5.1 审计日志示例查询

```bash
python -c "
from app.db import Database
db = Database()
rows = db.execute(\"SELECT created_at, user_id, action, ip FROM audit_logs WHERE created_at >= datetime('now', '-2 hours') ORDER BY created_at DESC LIMIT 20\").fetchall()
for r in rows: print(r)
"
```

---

## 6. 观察期（切换后 24 小时）

切换完成后进入 24 小时观察期，按以下节奏监控：

| 监控项 | 频率 | 阈值 | 异常处置 |
|--------|------|------|----------|
| `/health/deep` | 每 15 分钟 | 必须 `healthy` | 转入回滚 |
| `/metrics` | 每 1 小时 | 错误率 < 0.1% | 报警并排查 |
| `logs/error.log` | 持续 | 无 ERROR | 报警并排查 |
| 数据库锁（busy_timeout 日志） | 持续 | 无 `database is locked` | 报警并排查 |
| 备份目录大小 | 每 4 小时 | < 配额 80% | 执行 `prune_old_backups` |
| PM2 内存 | 每 1 小时 | < 80% 上限 | 重启或排查 |
| 磁盘空间 | 每 4 小时 | < 80% | 清理临时文件 |

### 6.1 观察期日志巡检脚本示例

```bash
# 巡检 ERROR 日志
grep -E "ERROR|CRITICAL" logs/*.log | tail -n 50

# 巡检数据库锁
grep -E "database is locked|busy_timeout" logs/*.log | tail -n 20

# 巡检 5xx 响应
awk '$9 ~ /^5/ {print}' logs/access.log | tail -n 50
```

### 6.2 观察期结束确认

观察期 24 小时满后，由切换负责人确认以下事项并在切换日志中签字：

- 全部监控项无异常
- 用户无重大投诉
- 备份目录已按策略归档
- 切换日志归档至 `docs/final_candidate/cutover_log_YYYYMMDD.md`（如用户要求）

---

## 7. 应急联系

| 角色 | 职责 | 响应时间 | 联系方式 |
|------|------|----------|----------|
| 回滚负责人 | 决策并执行回滚 | 30 分钟内 | （填写实际联系方式） |
| DBA | 数据库异常处置 | 30 分钟内 | （填写实际联系方式） |
| 开发负责人 | 代码异常处置 | 30 分钟内 | （填写实际联系方式） |
| 运维负责人 | 基础设施异常处置 | 30 分钟内 | （填写实际联系方式） |
| 用户接口人 | 用户沟通与授权确认 | 1 小时内 | （填写实际联系方式） |

> 联系方式须在切换前填写完整，并经各角色确认。

---

## 8. 风险与缓解措施

| 风险 | 等级 | 缓解措施 |
|------|------|----------|
| 迁移过程中断电 | 高 | 备份完整 + `Database.initialize()` 幂等可重入 |
| 备份文件损坏 | 高 | `validate_backup` + SHA256 双重校验 |
| 冒烟脚本漏报 | 中 | 切换后 24 小时观察期 + `/health/deep` 持续监控 |
| 回滚窗口超时 | 中 | 维护窗口建议 4 小时，回滚操作预计 30 分钟 |
| 依赖冲突 | 中 | 切换前在副本环境完整安装一次依赖 |
| 审计日志丢失 | 低 | 迁移前备份包含审计日志，可恢复 |
| 应用以 root 运行 | 高 | §10.1/§10.7 强制 appuser 启动，`ps` 复核 USER 列 |
| 应用端口公网暴露（0.0.0.0:6001/8000） | 高 | §10.3 强制 127.0.0.1，§10.4 ufw/iptables/安全组 deny，`ss` 复核 |
| HTTPS 配置失效或证书过期 | 高 | §10.2 Nginx 80→443 + HSTS，certbot cron 自动续期，切换前 `openssl x509` 校验有效期 |
| `.env`/DB 权限过宽（644/属主 root） | 中 | §10.5 chmod 600 + chown appuser，§3 检查清单与 §10.8 复核 |
| `POLICY_AUTH_DISABLED=1` 残留 | 高 | §2 前置条件 13 禁止，启动日志须含 `auth_enabled=True` |

---

## 9. 关键命令速查

```bash
# 启动开发服务器（仅供参考，生产环境见步骤 7/11）
uvicorn app.main:app --host 127.0.0.1 --port 8000

# 生产环境启动
APP_ENV=production POLICY_COOKIE_SECURE=1 uvicorn app.main:app --host 127.0.0.1 --port 8000

# 备份数据库
python -c "from app.db import Database; from app.backup import backup_database; backup_database(Database(), 'data/backups', label='pre_cutover')"

# 恢复数据库（dry_run）
python -c "from app.backup import restore_database; print(restore_database('data/backups/policy_inspector_pre_cutover_XXX.db', 'data/policy_inspector.db', dry_run=True))"

# 校验备份
python -c "from app.backup import validate_backup; print(validate_backup('data/backups/policy_inspector_pre_cutover_XXX.db'))"

# 列出备份
python -c "from app.backup import list_backups; print(list_backups('data/backups'))"

# 演练
python scripts/rehearsal_backup_restore.py --source-db data/policy_inspector.db --tmp-dir tmp/rehearsal

# 冒烟
python scripts/smoke_test.py --base-url http://localhost:8000 --admin-user admin --admin-pass 'PASSWORD'

# 1500 评测
python scripts/final_1500_evaluation.py

# 全量测试
python -m pytest tests/ -q --tb=short

# 数据库 SHA256
python -c "import hashlib; print(hashlib.sha256(open('data/policy_inspector.db','rb').read()).hexdigest().upper())"

# 数据库完整性
python -c "from app.db import Database; print(Database().execute('PRAGMA integrity_check').fetchone()[0])"
```

---

## 10. 安全与运维加固（R-P1-3 补齐）

> 本章节对应独立总验 `remediation_list.md` R-P1-3 与 `final_acceptance_report.md` F6 中「final cutover runbook 对 non-root/HTTPS/6001 弱于 phase1 closeout」的整改要求，等价段落见 `docs/phase1_closeout/production_cutover_runbook.md` §9-§11 与 `docs/phase1/02_security.md`。
>
> 本章节的所有配置均为**生产强制项**，与第 2 章前置条件、第 3 章检查清单一一对应。切换前必须全部就位，切换后必须全部保留；回滚后必须按 `09_rollback_runbook.md` §6 复核。

### 10.1 Non-root 运行用户

应用服务、Nginx worker、OCR worker、备份脚本均**禁止以 root 运行**。统一使用专用服务账户 `appuser`（与 phase1 closeout 的 `policyapp` 等价，二选一，全文保持一致即可）。

```bash
# 1. 创建专用服务用户（无登录 shell，无 home）
sudo useradd -r -s /usr/sbin/nologin -d /opt/policy-inspector appuser

# 2. 切换部署目录属主（代码、数据、日志、备份一并切换）
sudo chown -R appuser:appuser /opt/policy-inspector
sudo chown -R appuser:appuser /opt/policy-inspector/data
sudo chown -R appuser:appuser /opt/policy-inspector/logs
sudo chown -R appuser:appuser /opt/policy-inspector/data/backups

# 3. 切换到服务用户执行后续启动/迁移/冒烟
sudo -u appuser -i
cd /opt/policy-inspector
# 之后所有 python / pm2 / uvicorn 命令均在该 sudo -u appuser 上下文执行
```

**切换到 appuser 后再执行第 4 章步骤 5-12**。Nginx 主进程虽以 root 启动（绑定 80/443 需要），但 worker 进程必须在 `nginx.conf` 中配置 `user appuser;`（或 `www-data` 等价低权账户）。

验证：

```bash
ps -eo user,pid,cmd | grep -E 'uvicorn|gunicorn|python.*app'   # USER 列必须为 appuser，不得出现 root
ps -eo user,pid,cmd | grep nginx                                # master 可为 root，worker 必须非 root
```

### 10.2 HTTPS 强制

所有外部端点**必须 HTTPS**。HTTP 仅用于 301 重定向到 HTTPS。禁止以明文 HTTP 暴露登录、巡检、审计、metrics 任何接口。

Nginx 配置（与 `docs/phase1/02_security.md` 等价，端口对齐为本 runbook 的 8000）：

```nginx
# /etc/nginx/sites-available/policy-inspect.conf

# 1. HTTP -> HTTPS 强制重定向
server {
    listen 80;
    listen [::]:80;
    server_name policy-inspect.example;     # 改为实际域名
    return 301 https://$host$request_uri;
}

# 2. HTTPS 主入口
server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name policy-inspect.example;

    ssl_certificate     /etc/ssl/certs/policy-inspect.crt;
    ssl_certificate_key /etc/ssl/private/policy-inspect.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    ssl_session_cache   shared:SSL:10m;
    ssl_session_timeout 1d;

    # HSTS：强制浏览器后续一律走 HTTPS
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Content-Type-Options    "nosniff" always;
    add_header X-Frame-Options           "SAMEORIGIN" always;
    add_header Referrer-Policy           "strict-origin-when-cross-origin" always;

    # 反向代理到本机应用（仅 127.0.0.1）
    location / {
        proxy_pass http://127.0.0.1:8000;       # 或 6001，见 §10.3
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 60s;
    }
}
```

**证书路径与续期**：

```bash
# 证书路径（属主 root:root，权限 644/600）
sudo chmod 644 /etc/ssl/certs/policy-inspect.crt
sudo chmod 600 /etc/ssl/private/policy-inspect.key
sudo chown root:root /etc/ssl/certs/policy-inspect.crt /etc/ssl/private/policy-inspect.key

# 续期：Let's Encrypt 示例（如使用商业证书，按 CA 流程替换）
sudo certbot renew --dry-run                                 # 切换前预演
sudo certbot renew --deploy-hook "systemctl reload nginx"    # 实际续期
# cron 定时（每天 02:30 检查，距过期 <30 天才真正续）
echo "30 2 * * * /usr/bin/certbot renew --quiet --deploy-hook 'systemctl reload nginx'" | sudo tee /etc/cron.d/certbot-renew
```

验证：

```bash
# HTTP 必须 301
curl -sI http://policy-inspect.example/ | head -n 1            # 期望 HTTP/1.1 301 Moved Permanently
# HTTPS 头必须含 HSTS
curl -sI https://policy-inspect.example/ | grep -i strict-transport-security
# 证书有效期
echo | openssl s_client -connect policy-inspect.example:443 -servername policy-inspect.example 2>/dev/null | openssl x509 -noout -dates
```

### 10.3 端口收口

公网仅暴露 Nginx 的 80/443；应用端口（本 runbook 为 8000；phase1 closeout 使用 8765/6001）**仅绑定 127.0.0.1**，或改用 unix socket。**严禁 `0.0.0.0:6001`、`0.0.0.0:8000`、`0.0.0.0:8765` 等公网绑定**。

```bash
# 正确：仅本机回环
APP_ENV=production POLICY_COOKIE_SECURE=1 \
  uvicorn app.main:app --host 127.0.0.1 --port 8000
# 或使用 unix socket（更彻底，无 TCP 端口暴露）
APP_ENV=production POLICY_COOKIE_SECURE=1 \
  uvicorn app.main:app --uds /run/policy-inspector/app.sock

# 错误（严禁）：
# uvicorn app.main:app --host 0.0.0.0 --port 6001   # ← 公网暴露，R-P1-3 红线
# uvicorn app.main:app --host 0.0.0.0 --port 8000   # ← 公网暴露
```

**6001 端口收口**（与 phase1 closeout §10 等价）：

```bash
# 若历史部署曾在 6001 提供服务（legacy 或 metrics 旁路），切换后必须显式关闭
sudo ufw deny 6001/tcp
sudo ufw deny 8000/tcp    # 应用端口同样禁止公网

# 确认 6001/8000 不再监听公网
sudo ss -ltnp | grep -E ':6001|:8000'
# 期望输出仅出现 127.0.0.1:8000 或 /run/policy-inspector/app.sock，不得出现 0.0.0.0:6001 或 *:6001
```

验证矩阵：

| 监听地址 | 端口 | 是否允许 | 说明 |
|----------|------|----------|------|
| `0.0.0.0:80` / `:::80` | 80 | ✅ | Nginx HTTP→HTTPS 重定向 |
| `0.0.0.0:443` / `:::443` | 443 | ✅ | Nginx HTTPS 主入口 |
| `127.0.0.1:8000` | 8000 | ✅ | 应用，仅本机 |
| `127.0.0.1:6001` | 6001 | ✅ | 应用（如使用 6001），仅本机 |
| `/run/policy-inspector/app.sock` | unix | ✅ | 应用，仅本机 |
| `0.0.0.0:6001` / `*:6001` | 6001 | ❌ 禁止 | 公网暴露应用 |
| `0.0.0.0:8000` / `*:8000` | 8000 | ❌ 禁止 | 公网暴露应用 |
| `0.0.0.0:8765` | 8765 | ❌ 禁止 | phase1 legacy 端口公网暴露 |

### 10.4 防火墙 / 安全组

主机防火墙与云安全组**双重收口**，仅放行业务必要端口。

**ufw 示例**（Debian/Ubuntu）：

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp          # SSH（建议进一步限定来源 IP）
sudo ufw allow 80/tcp          # Nginx HTTP（仅用于 301）
sudo ufw allow 443/tcp         # Nginx HTTPS
# 6001 / 8000 / 8765 一律不放行
sudo ufw deny 6001/tcp
sudo ufw deny 8000/tcp
sudo ufw deny 8765/tcp
sudo ufw enable
sudo ufw status numbered
```

**iptables 示例**（RHEL/CentOS 等无 ufw 的环境）：

```bash
sudo iptables -A INPUT -p tcp --dport 22  -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 80  -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 443 -j ACCEPT
sudo iptables -A INPUT -p tcp --dport 6001 -j DROP
sudo iptables -A INPUT -p tcp --dport 8000 -j DROP
sudo iptables -A INPUT -p tcp --dport 8765 -j DROP
sudo iptables -A INPUT -j DROP   # 默认拒绝
sudo iptables-save | sudo tee /etc/sysconfig/iptables
```

**云安全组**（阿里云/腾讯云/AWS 等）：入站规则仅放行 22/80/443，来源 IP 按最小授权原则限定（如仅办公网出口或堡垒机）。**不得**在安全组放行 6001/8000/8765。

验证：

```bash
# 从公网主机探测，6001/8000 必须不可达
nc -zv <prod-public-ip> 6001   # 期望 refused / timeout
nc -zv <prod-public-ip> 8000   # 期望 refused / timeout
nc -zv <prod-public-ip> 443    # 期望 open
```

### 10.5 凭据与密钥

所有凭据文件**权限 600、属主 appuser**。禁止 644、禁止属主 root（除非只读证书）、禁止进入 git。

```bash
# .env 文件
sudo chown appuser:appuser /opt/policy-inspector/.env
sudo chmod 600 /opt/policy-inspector/.env

# 数据库文件
sudo chown appuser:appuser /opt/policy-inspector/data/policy_inspector.db
sudo chmod 600 /opt/policy-inspector/data/policy_inspector.db

# 备份目录下所有备份文件
sudo chown -R appuser:appuser /opt/policy-inspector/data/backups
sudo find /opt/policy-inspector/data/backups -type f -exec chmod 600 {} \;

# 日志文件
sudo chown -R appuser:appuser /opt/policy-inspector/logs
sudo find /opt/policy-inspector/logs -type f -exec chmod 640 {} \;

# TLS 私钥（Nginx 读取，root 持有）
sudo chown root:root /etc/ssl/private/policy-inspect.key
sudo chmod 600 /etc/ssl/private/policy-inspect.key
```

**.env 内容最小集**（实际值用强随机串，禁止使用示例值）：

```ini
APP_ENV=production
POLICY_COOKIE_SECURE=1
# POLICY_AUTH_DISABLED 不得出现，或必须为 0
POLICY_ADMIN_USERNAME=admin
POLICY_ADMIN_PASSWORD=<强随机串，与 bootstrap_admin.py 一致>
SECRET_KEY=<强随机串>
DATABASE_URL=sqlite:///data/policy_inspector.db
```

验证：

```bash
stat -c '%a %U:%G %n' /opt/policy-inspector/.env                          # 600 appuser:appuser
stat -c '%a %U:%G %n' /opt/policy-inspector/data/policy_inspector.db      # 600 appuser:appuser
stat -c '%a %U:%G %n' /etc/ssl/private/policy-inspect.key                 # 600 root:root
# 确认 .env 未进 git
git -C /opt/policy-inspector check-ignore .env                            # 应输出 .env
```

### 10.6 日志轮转

使用 logrotate 防止日志撑爆磁盘。应用日志、Nginx 日志均纳入轮转。

**/etc/logrotate.d/policy-inspector**：

```
/opt/policy-inspector/logs/*.log {
    daily
    rotate 30
    missingok
    notifempty
    compress
    delaycompress
    dateext
    dateformat -%Y%m%d
    create 640 appuser appuser
    sharedscripts
    postrotate
        # 通知应用重开日志句柄（PM2 或 systemd 自动处理；uvicorn 需 reload）
        /usr/bin/pm2 reload policy-inspector 2>/dev/null || true
        /bin/systemctl reload policy-inspector 2>/dev/null || true
    endscript
}

/var/log/nginx/policy-inspect-*.log {
    daily
    rotate 30
    missingok
    notifempty
    compress
    delaycompress
    dateext
    dateformat -%Y%m%d
    create 640 www-data adm
    sharedscripts
    postrotate
        [ -f /var/run/nginx.pid ] && kill -USR1 $(cat /var/run/nginx.pid)
    endscript
}
```

验证：

```bash
sudo logrotate -d /etc/logrotate.d/policy-inspector    # dry-run，确认配置无误
sudo logrotate -f /etc/logrotate.d/policy-inspector    # 强制执行一次，观察归档文件生成
ls -lht /opt/policy-inspector/logs/ | head             # 应见 *.log 与 *.log-YYYYMMDD.gz 并存
```

### 10.7 进程监管（非 root 启动）

应用必须由进程监管器托管，**以 appuser 启动**，崩溃自动拉起、开机自启。

**方案 A：systemd（推荐，Linux 原生）**

`/etc/systemd/system/policy-inspector.service`：

```ini
[Unit]
Description=Policy Inspection System (uvicorn)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=appuser
Group=appuser
WorkingDirectory=/opt/policy-inspector
EnvironmentFile=/opt/policy-inspector/.env
ExecStart=/opt/policy-inspector/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5s
StandardOutput=append:/opt/policy-inspector/logs/app.log
StandardError=append:/opt/policy-inspector/logs/error.log

# 安全加固
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/policy-inspector/data /opt/policy-inspector/logs
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
LockPersonality=true
RestrictRealtime=true
RestrictSUIDSGID=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable policy-inspector
sudo systemctl start policy-inspector
sudo systemctl status policy-inspector
# 确认日志含 [startup] app_env=production auth_enabled=True
```

**方案 B：PM2（与现有 runbook 一致，必须以 appuser 启动）**

```bash
# 关键：先 sudo -u appuser，再 pm2 start，否则 pm2 以 root 拉起子进程
sudo -u appuser -i
cd /opt/policy-inspector
pm2 start "uvicorn app.main:app --host 127.0.0.1 --port 8000" \
  --name policy-inspector \
  --env production \
  --log logs/app.log --error logs/error.log
pm2 save                                       # 保存进程列表
pm2 startup systemd -u appuser --hp /opt/policy-inspector   # 生成开机自启脚本（以 appuser 身份）
```

**禁止**：以 root 直接 `pm2 start` 然后用 `--uid` 降权——子进程仍可能继承 root 环境变量与能力。必须先切到 appuser。

验证：

```bash
# systemd
systemctl show policy-inspector -p User,Group,ExecStart    # User=appuser
ps -eo user,pid,cmd | grep uvicorn                          # USER=appuser
# PM2
pm2 describe policy-inspector | grep -E 'user|script path'  # user=appuser
pm2 jlist | python -c "import sys,json;d=json.load(sys.stdin);print([(p['name'],p['pm2_env'].get('uid','?')) for p in d])"
```

### 10.8 切换后安全复核清单

第 4 章步骤 14（观察 1 小时）期间，按下表逐项复核安全配置是否在切换中保持：

| 复核项 | 期望 | 命令 |
|--------|------|------|
| 服务用户 | appuser | `ps -eo user,cmd \| grep uvicorn \| grep -v grep` |
| 应用端口 | 仅 127.0.0.1 | `ss -ltnp \| grep -E ':8000\|:6001'` |
| HTTP→HTTPS | 301 | `curl -sI http://localhost/ \| head -1` |
| HSTS | 存在 | `curl -sI https://localhost/ \| grep -i strict-transport` |
| `.env` 权限 | 600 appuser | `stat -c '%a %U' .env` |
| DB 权限 | 600 appuser | `stat -c '%a %U' data/policy_inspector.db` |
| 防火墙 | 6001/8000 DROP | `ufw status \| grep -E '6001\|8000'` |
| `POLICY_AUTH_DISABLED` | 未设 | `systemctl show policy-inspector -p Environment \| grep AUTH_DISABLED`（应无） |
| 进程监管 | enabled | `systemctl is-enabled policy-inspector` 或 `pm2 jlist` |

任一项不符合期望 → 立即转入 `09_rollback_runbook.md` 或就地修复并记录。

---

## 11. 结论

本 Runbook 是 M7 阶段产出的**候选方案**，已完成：

- 切换前检查清单（含安全前置项）
- 切换步骤（14 步，每步含通过条件）
- 切换后验证（7 项）
- 24 小时观察期监控矩阵
- 应急联系与风险缓解
- **安全与运维加固（第 10 章，补齐 R-P1-3）**：non-root 用户、HTTPS 强制、端口收口（含 6001）、防火墙/安全组、凭据密钥权限、logrotate、systemd/PM2 非 root 启动、切换后安全复核清单

**实际切换须由用户授权后执行**。在用户授权前，本 Runbook 仅作为准备材料存在，不得用于任何生产环境操作。

本 Runbook 通过主开发 AI 自验产出，待独立总验收。
