# 09 回滚 Runbook（候选方案）

> 版本号：V3.0
> 生成日期：2026-07-27
> 状态：主开发 AI 自验产出，待独立总验收
> 适用分支：`feat/full-policy-inspection-upgrade`（升级分支，HEAD：`02cb6e22341e2940abb0e657d27b9ba2c7b2cae9`）
> 回滚目标分支：`codex/fix-snapshot-result-collision`（生产基线，HEAD：`be74df72598ae9ae0feb3a8fd9987c84c5bd8b67`）
> 配套文档：`08_production_cutover_runbook.md`

---

## 1. 执行摘要

当生产切换（参见 `08_production_cutover_runbook.md`）失败或切换后出现严重问题时，按本 Runbook 回滚到切换前状态。

回滚包含两个层面：

- **数据库回滚**：用切换前备份（`label='pre_cutover'`）覆盖当前数据库
- **代码回滚**：从升级分支切回生产基线分支

回滚由 `app/backup/__init__.py` 提供的 `restore_database` / `validate_backup` / `list_backups` 等函数支持。回滚前必须完成 `dry_run` 校验。

本 Runbook 是候选方案。实际回滚须在维护窗口内由具备生产权限的运维人员执行。

---

## 2. 触发条件

满足以下任一条件即触发回滚流程：

| 编号 | 触发条件 | 检测方式 |
|------|----------|----------|
| T1 | 冒烟脚本失败（< 15/15 PASS） | `scripts/smoke_test.py` 输出 |
| T2 | `/health/deep` 返回 `unhealthy` | `curl http://localhost:8000/health/deep` |
| T3 | 数据库 `integrity_check` 失败 | `PRAGMA integrity_check` 输出非 `ok` |
| T4 | 关键业务功能不可用（登录、巡检、规则配置、审计日志） | 人工或脚本验证 |
| T5 | 用户明确要求回滚 | 用户授权记录 |
| T6 | 切换后 1 小时观察期内出现 5xx 错误率 > 1% | `logs/access.log` 统计 |
| T7 | 切换后 24 小时观察期内出现无法修复的严重缺陷 | 日志与监控 |

> 触发 T1-T4 时，**立即回滚**，无需等待用户二次确认；T5 按用户指示执行；T6-T7 须由回滚负责人决策。

---

## 3. 回滚前置条件

执行回滚前，必须确认以下条件：

1. 切换前备份存在且 SHA256 校验通过：`data/backups/policy_inspector_pre_cutover_XXX.db`。
2. 回滚负责人在线，可 30 分钟内响应。
3. 仍在维护窗口内（建议切换后 1 小时内回滚，超过 1 小时须谨慎评估数据损失风险）。
4. 切换前生产数据库 SHA256 已记录（`pre_cutover_sha256`），用于回滚后比对。
5. 故障日志与故障数据库副本的存储位置已规划好。
6. **`appuser` 服务用户仍存在**，且部署目录属主未变（回滚不重建用户）。
7. **Nginx / systemd / ufw / logrotate 配置未被破坏**——回滚仅触碰代码与数据库，基础设施安全配置原样保留（见 §6.5）。
8. **`POLICY_AUTH_DISABLED` 未被设置**——回滚后启动仍须 `auth_enabled=True`。

### 3.1 前置自检命令

```bash
# 列出所有备份，确认 pre_cutover 备份存在
python -c "from app.backup import list_backups; print(list_backups('data/backups'))"

# 校验备份完整性
python -c "from app.backup import validate_backup; print(validate_backup('data/backups/policy_inspector_pre_cutover_XXX.db'))"

# 计算备份 SHA256
python -c "import hashlib; print(hashlib.sha256(open('data/backups/policy_inspector_pre_cutover_XXX.db','rb').read()).hexdigest().upper())"
```

---

## 4. 回滚步骤

按以下顺序执行，**每一步必须 PASS 才能继续下一步**。

### 步骤 1：立即停止写入流量

```bash
# 方式 A：PM2 停止应用
pm2 stop app

# 方式 B：nginx 切换到维护页
nginx -s reload  # 已预先配置 maintenance 路由
```

确认写入流量已停止：

```bash
pm2 status
tail -f logs/access.log  # 不应再出现 POST/PUT/DELETE 请求
```

### 步骤 2：记录当前数据库 SHA256（用于事后分析）

```bash
python -c "import hashlib; print(hashlib.sha256(open('data/policy_inspector.db','rb').read()).hexdigest().upper())"
```

将输出值记录到回滚日志，字段名 `failed_state_sha256`。该值用于事后分析故障数据库状态。

### 步骤 3：定位切换前备份

```bash
python -c "from app.backup import list_backups; print(list_backups('data/backups'))"
```

从输出中找到 `label='pre_cutover'` 的备份文件，记下完整路径，例如：

```
data/backups/policy_inspector_pre_cutover_20260727_020000.db
```

> 若找不到 `pre_cutover` 备份，**立即停止回滚**，向上级报告，由 DBA 决定下一步处置（如使用更早的定期备份）。

### 步骤 4：校验备份

```bash
python -c "from app.backup import validate_backup; print(validate_backup('data/backups/policy_inspector_pre_cutover_XXX.db'))"
```

通过条件：返回 `True` 且 `integrity_check` 输出 `ok`。

同时计算备份 SHA256，与切换日志中记录的备份 SHA256 比对，必须一致。

### 步骤 5：dry_run 恢复

```bash
python -c "from app.backup import restore_database; print(restore_database('data/backups/policy_inspector_pre_cutover_XXX.db', 'data/policy_inspector.db', dry_run=True))"
```

通过条件：`dry_run` 返回成功，无目标文件冲突、磁盘空间不足等警告。

> 若 dry_run 失败，**不得执行实际恢复**，排查问题后再试。

### 步骤 6：实际恢复

```bash
python -c "from app.backup import restore_database; print(restore_database('data/backups/policy_inspector_pre_cutover_XXX.db', 'data/policy_inspector.db', dry_run=False))"
```

通过条件：返回成功，目标文件 `data/policy_inspector.db` 已被覆盖。

> 实际恢复是破坏性操作，执行前确认步骤 1-5 全部 PASS。

### 步骤 7：校验恢复后数据库 SHA256

```bash
python -c "import hashlib; print(hashlib.sha256(open('data/policy_inspector.db','rb').read()).hexdigest().upper())"
```

通过条件：输出值与切换日志中 `pre_cutover_sha256` **完全一致**。

预期值（如生产数据库自上次记录未变）：`13E6A8A412F69C13D149674A31076854C966F5B151E670FC9E22D0F53AB77AF3`。

> 若 SHA256 不一致，**立即停止回滚**，由 DBA 介入排查。

### 步骤 8：回滚代码

```bash
# 进入生产代码目录
cd /opt/policy-inspector  # 视实际部署路径

# 切回生产基线分支
git fetch origin
git checkout codex/fix-snapshot-result-collision
git pull origin codex/fix-snapshot-result-collision

# 确认 HEAD
git rev-parse HEAD
# 预期：be74df72598ae9ae0feb3a8fd9987c84c5bd8b67
```

### 步骤 9：重装旧依赖

```bash
source .venv/bin/activate  # Linux
# 或 .venv\Scripts\activate  # Windows

pip install -r requirements.txt
```

> 升级版本的 `requirements.txt` 可能引入新依赖或升级版本号，回滚时必须使用基线分支的版本，避免依赖不一致。

### 步骤 10：启动服务

> ⚠️ 回滚后**必须保持与切换前等价的安全配置**：以 `appuser` 启动、仅绑定 `127.0.0.1`、HTTPS 仍由 Nginx 终止。安全配置保留验证见 §6.5。回滚不涉及 Nginx/systemd/ufw/logrotate 配置文件的回滚——这些基础设施配置在回滚后必须原样保留。

```bash
# 必须以 appuser 启动（与 08 runbook §10.1/§10.7 一致）
sudo -u appuser -i
cd /opt/policy-inspector

# 方式 A：systemd
sudo systemctl start policy-inspector

# 方式 B：PM2（必须先 sudo -u appuser）
APP_ENV=production POLICY_COOKIE_SECURE=1 pm2 start "uvicorn app.main:app --host 127.0.0.1 --port 8000" --name policy-inspector --env production
```

确认服务已启动：

```bash
systemctl status policy-inspector   # 或 pm2 status
ps -eo user,cmd | grep uvicorn      # USER 列必须为 appuser
curl -s http://127.0.0.1:8000/health
```

### 步骤 11：运行冒烟脚本

```bash
python scripts/smoke_test.py --base-url http://localhost:8000 --admin-user admin --admin-pass 'PASSWORD'
```

通过条件：**15/15 PASS**。

> 若冒烟脚本仍失败，说明回滚未成功或生产基线本身存在问题。立即上报，由 DBA 与开发负责人联合处置。

### 步骤 12：恢复写入流量

```bash
# 方式 A：PM2 已在步骤 10 启动，无需额外操作
# 方式 B：nginx 恢复
nginx -s reload  # 移除 maintenance 路由
```

确认写入流量已恢复：

```bash
tail -f logs/access.log  # 应能看到 POST/PUT/DELETE 请求
```

---

## 5. 回滚后验证

回滚完成后，逐项验证：

| 验证项 | 期望结果 | 检查命令 |
|--------|----------|----------|
| 数据库 SHA256 | 与 `pre_cutover_sha256` 一致 | `python -c "import hashlib; print(hashlib.sha256(open('data/policy_inspector.db','rb').read()).hexdigest().upper())"` |
| `/health` | `status=ok` | `curl -s http://127.0.0.1:8000/health` |
| `/health/deep` | `healthy` | `curl -s http://127.0.0.1:8000/health/deep` |
| 关键业务功能恢复 | 登录、巡检、规则配置、审计日志正常 | 人工验证 |
| 审计日志记录回滚操作 | 含 rollback 相关日志 | 查询 `audit_logs` 表 |
| 代码 HEAD | `be74df72598ae9ae0feb3a8fd9987c84c5bd8b67` | `git rev-parse HEAD` |
| 服务运行用户 | `appuser`（非 root） | `ps -eo user,cmd \| grep uvicorn \| grep -v grep` |
| 应用端口绑定 | 仅 `127.0.0.1:8000`，无 `0.0.0.0:6001/8000` | `ss -ltnp \| grep -E ':8000\|:6001'` |
| HTTPS 仍生效 | HTTP 301 → HTTPS，HSTS 存在 | `curl -sI http://localhost/ \| head -1` |
| `.env` 权限 | 600 appuser | `stat -c '%a %U' .env` |
| DB 文件权限 | 600 appuser | `stat -c '%a %U' data/policy_inspector.db` |
| 防火墙规则 | 6001/8000 仍 deny | `ufw status \| grep -E '6001\|8000'` |
| `POLICY_AUTH_DISABLED` | 未设置 | `env \| grep POLICY_AUTH_DISABLED`（应无输出） |
| 进程监管 | enabled，以 appuser 启动 | `systemctl is-enabled policy-inspector` 或 `pm2 jlist` |
| logrotate 配置 | 仍存在 | `ls /etc/logrotate.d/policy-inspector` |

### 5.1 审计日志查询示例

```bash
python -c "
from app.db import Database
db = Database()
rows = db.execute(\"SELECT created_at, user_id, action, ip FROM audit_logs WHERE action LIKE '%rollback%' OR action LIKE '%restore%' ORDER BY created_at DESC LIMIT 20\").fetchall()
for r in rows: print(r)
"
```

---

## 6. 回滚后行动

回滚成功后，执行以下行动以便事后分析与复盘：

### 6.1 保留故障数据库副本

```bash
# 将故障数据库重命名并归档（按回滚日期）
mv data/policy_inspector_failed_YYYYMMDD.db.tmp archives/

# 若步骤 2 已记录 failed_state_sha256，可对比确认副本完整性
python -c "import hashlib; print(hashlib.sha256(open('archives/policy_inspector_failed_YYYYMMDD.db','rb').read()).hexdigest().upper())"
```

> 故障数据库副本保留至少 90 天，供事后分析与改进测试用例。

### 6.2 保留故障日志

```bash
# 归档故障时段日志
mkdir -p archives/logs_YYYYMMDD
cp logs/*.log archives/logs_YYYYMMDD/
```

### 6.3 提交事故报告

24 小时内提交事故报告，包含：

- 触发条件（T1-T7 中的哪一项）
- 故障现象与日志摘录
- 回滚操作时间线（每步开始/结束时间）
- 故障数据库 SHA256 与备份 SHA256 对比
- 根因分析（初步）
- 改进措施（测试用例补充、监控告警优化、Runbook 修订等）

事故报告归档至 `docs/final_candidate/incident_report_YYYYMMDD.md`（如用户要求）。

### 6.4 通知用户

在 IM 群、邮件、系统公告三个渠道发布回滚完成通知，包含：

- 回滚完成时间
- 当前生产版本（基线分支 HEAD）
- 用户可正常使用的时间
- 后续切换计划（如修复后会重新切换）

### 6.5 安全配置保留验证（R-P1-3 配套）

> 回滚仅回退**代码与数据库**，**不回退基础设施安全配置**。Nginx（HTTPS/HSTS/80→443）、systemd/PM2 单元、ufw/iptables 规则、logrotate、`.env`/DB 文件权限、`appuser` 用户与属主——这些在回滚后必须原样保留，与 `08_production_cutover_runbook.md` 第 10 章等价。
>
> 若回滚过程中误删/误改了上述任一配置，必须立即按 08 runbook §10 对应小节恢复，不得在「安全裸奔」状态下恢复写入流量。

#### 6.5.1 基础设施配置保留清单

| 配置项 | 回滚是否触碰 | 保留要求 | 恢复参考 |
|--------|--------------|----------|----------|
| `appuser` 用户 | 否 | 用户仍存在 | 08 §10.1 `useradd` |
| 部署目录属主 | 否 | `appuser:appuser` | 08 §10.1 `chown -R` |
| Nginx 配置（80→443、HSTS、`proxy_pass 127.0.0.1`） | 否 | 文件未变，`nginx -t` 通过 | 08 §10.2 |
| TLS 证书与私钥 | 否 | 路径/权限不变 | 08 §10.2 |
| systemd unit / PM2 进程列表 | 否（仅 restart，不 recreate） | `User=appuser`、`ExecStart` 含 `--host 127.0.0.1` | 08 §10.7 |
| ufw / iptables / 云安全组 | 否 | 22/80/443 放行，6001/8000/8765 deny | 08 §10.4 |
| `.env` 文件 | 否（仅可能被 git checkout 覆盖模板） | 权限 600、属主 appuser、不含 `POLICY_AUTH_DISABLED=1` | 08 §10.5 |
| `data/policy_inspector.db` | 是（restore_database 覆盖） | restore 后立即 `chown appuser:appuser && chmod 600` | 08 §10.5 |
| logrotate 配置 | 否 | `/etc/logrotate.d/policy-inspector` 仍存在 | 08 §10.6 |
| 备份目录权限 | 否 | `appuser:appuser`，文件 600 | 08 §10.5 |

#### 6.5.2 回滚后安全复核脚本

```bash
#!/bin/bash
# 回滚后安全复核（与 08 runbook §10.8 等价）
set -u
fail=0

echo "== [1] 服务运行用户 =="
u=$(ps -eo user,cmd | grep uvicorn | grep -v grep | awk '{print $1}' | head -1)
[ "$u" = "appuser" ] && echo "OK appuser" || { echo "FAIL user=$u"; fail=1; }

echo "== [2] 应用端口绑定 =="
b=$(ss -ltnp | grep -E ':8000|:6001' | grep -v '127.0.0.1')
[ -z "$b" ] && echo "OK 仅 127.0.0.1" || { echo "FAIL 公网绑定: $b"; fail=1; }

echo "== [3] HTTP->HTTPS =="
c=$(curl -sI http://localhost/ | head -1)
echo "$c" | grep -q 301 && echo "OK 301" || { echo "FAIL $c"; fail=1; }

echo "== [4] HSTS =="
curl -sI https://localhost/ | grep -qi strict-transport-security && echo "OK HSTS" || { echo "FAIL no HSTS"; fail=1; }

echo "== [5] .env 权限 =="
p=$(stat -c '%a %U' /opt/policy-inspector/.env)
[ "$p" = "600 appuser" ] && echo "OK" || { echo "FAIL $p"; fail=1; }

echo "== [6] DB 权限 =="
p=$(stat -c '%a %U' /opt/policy-inspector/data/policy_inspector.db)
[ "$p" = "600 appuser" ] && echo "OK" || { echo "FAIL $p"; fail=1; }

echo "== [7] 防火墙 6001/8000 =="
ufw status 2>/dev/null | grep -E '6001|8000' | grep -qi deny && echo "OK deny" || echo "WARN 需人工确认（或 iptables/云安全组）"

echo "== [8] POLICY_AUTH_DISABLED =="
e=$(env | grep POLICY_AUTH_DISABLED)
[ -z "$e" ] && echo "OK 未设置" || { echo "FAIL $e"; fail=1; }

echo "== [9] 进程监管 =="
systemctl is-enabled policy-inspector 2>/dev/null | grep -q enabled && echo "OK systemd enabled" || \
  (pm2 jlist 2>/dev/null | grep -q policy-inspector && echo "OK pm2" || { echo "FAIL 未托管"; fail=1; })

echo "== [10] logrotate =="
[ -f /etc/logrotate.d/policy-inspector ] && echo "OK" || { echo "FAIL logrotate 缺失"; fail=1; }

echo ""
[ $fail -eq 0 ] && echo "✅ 安全复核全部通过" || { echo "❌ 存在失败项，不得恢复写入流量"; exit 1; }
```

#### 6.5.3 restore_database 后的权限修复

`restore_database`（步骤 6）以 `appuser` 身份执行时，新写入的 DB 文件会继承 `appuser:appuser` 与默认 umask。但若以 root 执行过恢复，或 umask 较宽，必须在步骤 7（SHA256 校验）之后、步骤 10（启动服务）之前显式修复：

```bash
sudo chown appuser:appuser /opt/policy-inspector/data/policy_inspector.db
sudo chmod 600 /opt/policy-inspector/data/policy_inspector.db
# 备份目录同理
sudo find /opt/policy-inspector/data/backups -type f -exec chmod 600 {} \;
sudo chown -R appuser:appuser /opt/policy-inspector/data/backups
```

#### 6.5.4 部分回滚（仅代码回滚）的安全注意

若采用第 7 章「部分回滚」（仅回滚代码、不回滚数据库），除上述清单外，还需额外确认：

- 旧基线代码分支的 `requirements.txt` 不得移除 `appuser` 运行所需的依赖
- 旧基线代码若使用不同的环境变量名（如 `POLICY_COOKIE_SECURE` 在历史版本曾缺省），必须在 `.env` 中显式补齐
- 旧基线代码若仍包含 `--host 0.0.0.0` 的启动脚本，**必须**改为 `127.0.0.1` 后再启动，不得沿用历史不安全默认

---

## 7. 部分回滚选项（仅代码回滚，不回滚数据库）

### 7.1 适用场景

满足以下全部条件时，可考虑仅回滚代码、不回滚数据库：

- 数据库迁移无破坏性变更（如仅新增表、新增字段，未删除/修改既有数据）
- 故障仅由代码 bug 引起，与数据库 schema 无关
- 用户明确同意保留迁移后的数据库

### 7.2 步骤

仅执行第 4 章的步骤 8-11，跳过数据库恢复（步骤 1-7、12 中涉及数据库的部分）：

1. 停止写入流量（步骤 1，仍需执行）
2. 回滚代码（步骤 8）
3. 重装旧依赖（步骤 9）
4. 启动服务（步骤 10）
5. 运行冒烟脚本（步骤 11）
6. 恢复写入流量

### 7.3 风险

| 风险 | 说明 | 缓解措施 |
|------|------|----------|
| 迁移已部分应用 | 旧代码可能无法识别新表/字段 | 启动前手动检查 `schema_migrations` 表 |
| 数据库 schema 与代码不匹配 | 旧代码读取新字段可能报错 | 启动后密切监控 `error.log` |
| 审计日志字段不一致 | 新字段在旧代码中无对应处理 | 验证审计日志写入功能正常 |
| 后续切换复杂化 | 下次切换需重新评估已应用的迁移 | 在 `schema_migrations` 表中明确记录状态 |

### 7.4 决策原则

> 部分回滚是**高风险选项**，仅在数据库回滚不可行（如已产生不可逆的业务数据）时使用。
> 默认采用完整回滚（第 4 章全部步骤）。

---

## 8. 应急联系

| 角色 | 职责 | 响应时间 | 联系方式 |
|------|------|----------|----------|
| 回滚负责人 | 决策并执行回滚 | 30 分钟内 | （填写实际联系方式） |
| DBA | 数据库异常处置 | 30 分钟内 | （填写实际联系方式） |
| 开发负责人 | 代码异常处置 | 30 分钟内 | （填写实际联系方式） |
| 运维负责人 | 基础设施异常处置 | 30 分钟内 | （填写实际联系方式） |
| 用户接口人 | 用户沟通与授权确认 | 1 小时内 | （填写实际联系方式） |

> 联系方式须在切换前填写完整，并经各角色确认。

---

## 9. 关键命令速查

```bash
# 列出备份
python -c "from app.backup import list_backups; print(list_backups('data/backups'))"

# 校验备份
python -c "from app.backup import validate_backup; print(validate_backup('data/backups/policy_inspector_pre_cutover_XXX.db'))"

# dry_run 恢复
python -c "from app.backup import restore_database; print(restore_database('data/backups/policy_inspector_pre_cutover_XXX.db', 'data/policy_inspector.db', dry_run=True))"

# 实际恢复
python -c "from app.backup import restore_database; print(restore_database('data/backups/policy_inspector_pre_cutover_XXX.db', 'data/policy_inspector.db', dry_run=False))"

# 回滚到迁移前（如备份模块提供）
python -c "from app.db import Database; from app.backup import rollback_to_pre_migration; rollback_to_pre_migration(Database(), 'data/backups')"

# 切换代码分支
git checkout codex/fix-snapshot-result-collision
git rev-parse HEAD  # 预期 be74df72598ae9ae0feb3a8fd9987c84c5bd8b67

# 启动服务
APP_ENV=production POLICY_COOKIE_SECURE=1 uvicorn app.main:app --host 127.0.0.1 --port 8000

# 冒烟
python scripts/smoke_test.py --base-url http://localhost:8000 --admin-user admin --admin-pass 'PASSWORD'

# 数据库 SHA256
python -c "import hashlib; print(hashlib.sha256(open('data/policy_inspector.db','rb').read()).hexdigest().upper())"

# 数据库完整性
python -c "from app.db import Database; print(Database().execute('PRAGMA integrity_check').fetchone()[0])"

# 清理旧备份（按需）
python -c "from app.backup import prune_old_backups; print(prune_old_backups('data/backups', keep_days=30))"
```

---

## 10. 回滚时间线参考

| 阶段 | 预计耗时 | 累计耗时 |
|------|----------|----------|
| 触发回滚决策 | 5 分钟 | 5 分钟 |
| 步骤 1：停止写入流量 | 2 分钟 | 7 分钟 |
| 步骤 2：记录当前 SHA256 | 1 分钟 | 8 分钟 |
| 步骤 3：定位备份 | 1 分钟 | 9 分钟 |
| 步骤 4：校验备份 | 2 分钟 | 11 分钟 |
| 步骤 5：dry_run 恢复 | 2 分钟 | 13 分钟 |
| 步骤 6：实际恢复 | 2 分钟 | 15 分钟 |
| 步骤 7：校验恢复后 SHA256 | 1 分钟 | 16 分钟 |
| 步骤 8：回滚代码 | 3 分钟 | 19 分钟 |
| 步骤 9：重装旧依赖 | 3 分钟 | 22 分钟 |
| 步骤 10：启动服务 | 2 分钟 | 24 分钟 |
| 步骤 11：冒烟脚本 | 5 分钟 | 29 分钟 |
| §6.5 安全复核（恢复写入流量前必做） | 2 分钟 | 31 分钟 |
| 步骤 12：恢复写入流量 | 2 分钟 | 33 分钟 |

预计回滚总耗时约 33 分钟，建议维护窗口预留 1 小时余量。**§6.5 安全复核未通过前不得恢复写入流量。**

---

## 11. 风险与缓解措施

| 风险 | 等级 | 缓解措施 |
|------|------|----------|
| 备份文件损坏导致无法恢复 | 高 | `validate_backup` + SHA256 双重校验，dry_run 先行 |
| 恢复过程中断电 | 高 | `restore_database` 支持重试，备份文件不受影响 |
| 切换后已产生业务数据丢失 | 高 | 切换后 1 小时内回滚，业务数据损失最小化 |
| 代码回滚后依赖不一致 | 中 | 严格使用基线分支的 `requirements.txt` |
| 部分回滚导致 schema 不一致 | 中 | 默认采用完整回滚，部分回滚需 DBA 评估 |
| 回滚窗口超时 | 中 | 维护窗口预留 1 小时余量 |
| 审计日志在回滚中丢失 | 低 | 备份包含审计日志，恢复后自动找回 |
| 回滚后安全配置丢失（root 启动/0.0.0.0/无 HTTPS） | 高 | §6.5 安全复核脚本强制校验；restore 后修复 DB 权限；不回滚 Nginx/ufw/logrotate |
| restore_database 后 DB 权限变宽 | 中 | §6.5.3 显式 `chown appuser && chmod 600` |
| 基线代码含 `--host 0.0.0.0` 历史默认 | 中 | §6.5.4 强制改为 `127.0.0.1` 后再启动 |

---

## 12. 结论

本 Runbook 是 M7 阶段产出的**候选方案**，已完成：

- 触发条件（7 项）
- 回滚前置条件（5 项）
- 回滚步骤（12 步，每步含通过条件）
- 回滚后验证（含安全配置保留项）
- 部分回滚选项与风险说明
- 应急联系、风险缓解、时间线参考
- **安全配置保留验证（§6.5，配套 R-P1-3）**：基础设施配置保留清单、回滚后安全复核脚本、restore_database 后权限修复、部分回滚安全注意

**实际回滚须在维护窗口内执行**。回滚是破坏性操作，执行前必须完成 dry_run 校验，并由回滚负责人决策。回滚后必须通过 §6.5 安全复核，方可恢复写入流量。

本 Runbook 通过主开发 AI 自验产出，待独立总验收。
