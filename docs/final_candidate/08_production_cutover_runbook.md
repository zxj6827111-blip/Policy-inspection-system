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

### 2.1 前置自检命令

| 检查项 | 命令 |
|--------|------|
| 当前分支 | `git rev-parse --abbrev-ref HEAD` |
| 当前 HEAD | `git rev-parse HEAD` |
| 回归测试 | `python -m pytest tests/ -q --tb=short` |
| 1500 评测 | `python scripts/final_1500_evaluation.py` |
| 备份演练 | `python scripts/rehearsal_backup_restore.py --source-db data/policy_inspector.db --tmp-dir tmp/rehearsal` |
| 备份目录可写 | `python -c "import os; os.makedirs('data/backups', exist_ok=True); print('OK')"` |

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

# 启动服务时务必带上：
# APP_ENV=production POLICY_COOKIE_SECURE=1 uvicorn app.main:app --host 0.0.0.0 --port 8000
```

确认 `.env` 文件（如有）也配置了相同项，避免重启后失效。

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

```bash
APP_ENV=production POLICY_COOKIE_SECURE=1 pm2 start app
# 或
APP_ENV=production POLICY_COOKIE_SECURE=1 uvicorn app.main:app --host 0.0.0.0 --port 8000
```

确认服务已启动：

```bash
pm2 status
curl -s http://localhost:8000/health
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

---

## 9. 关键命令速查

```bash
# 启动开发服务器（仅供参考，生产环境见步骤 7/11）
uvicorn app.main:app --host 0.0.0.0 --port 8000

# 生产环境启动
APP_ENV=production POLICY_COOKIE_SECURE=1 uvicorn app.main:app --host 0.0.0.0 --port 8000

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

## 10. 结论

本 Runbook 是 M7 阶段产出的**候选方案**，已完成：

- 切换前检查清单
- 切换步骤（14 步，每步含通过条件）
- 切换后验证（7 项）
- 24 小时观察期监控矩阵
- 应急联系与风险缓解

**实际切换须由用户授权后执行**。在用户授权前，本 Runbook 仅作为准备材料存在，不得用于任何生产环境操作。

本 Runbook 通过主开发 AI 自验产出，待独立总验收。
