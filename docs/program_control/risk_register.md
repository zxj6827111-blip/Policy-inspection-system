# 风险登记册（Risk Register）

> 记录 M0～M7 主开发过程中识别的风险、影响、缓解措施和当前状态。
> 严守生产边界：任何触及生产数据库、生产服务、生产 Nginx/PM2/证书的操作必须先升级到用户决策。

---

## R-001 生产数据库被误修改

- 影响：生产数据损坏，不可逆
- 可能性：中
- 缓解：所有迁移先在生产副本（`data/policy_inspector.db` 的拷贝）上执行；主开发 AI 在 M0～M6 不得对生产库做任何写入；`status.json` 中 `production_database_modified` 必须保持 `false`
- 当前状态：OPEN（持续监控）
- 触发条件：任何脚本直接对 `data/policy_inspector.db` 执行 INSERT/UPDATE/DELETE/ALTER
- 处置：立即停止，回滚副本，记录事件

## R-002 生产服务被重启

- 影响：生产中断、用户感知
- 可能性：低
- 缓解：M0～M6 不执行 `pm2 restart`、`systemctl restart`、`nginx -s reload`；`status.json` 中 `production_service_restarted` 必须保持 `false`
- 当前状态：OPEN

## R-003 高频外部扫描触发封禁

- 影响：被目标站点封禁 IP
- 可能性：中
- 缓解：M0～M6 不执行真实外部扫描；所有测试用本地 fixture / mock；M2/M3/M5 的样本抽取使用历史快照与已下载 PDF
- 当前状态：OPEN

## R-004 工作区 dirty 导致提交不可复现

- 影响：Git 正式化失败
- 可能性：中
- 缓解：M0 强制工作区干净；每个里程碑开始/结束记录 HEAD 与 SHA256
- 当前状态：MITIGATED（M0 完成后即关闭）

## R-005 敏感文件误入库

- 影响：密钥、数据库、日志泄露
- 可能性：低
- 缓解：`.gitignore` 覆盖 `*.db`、`*.db-*`、`.env`、`tmp/`、规划文档；提交前用 `git ls-files` 检查
- 当前状态：MITIGATED

## R-006 OCR/PDF 依赖在 Windows 开发机不可用

- 影响：M3 开发受阻
- 可能性：中
- 缓解：使用纯 Python PDF 库（pdfminer.six / pypdf）+ 可选 OCR（rapidocr-onnxruntime / paddleocr）；测试用 fixture；不在 M3 启动时强制安装重型依赖
- 当前状态：MITIGATED（M3 已完成，Tesseract v5.5.0 已安装；测试用临时 PDF fixture 覆盖）

## R-007 1,000 / 1,500 条样本缺乏真实标注

- 影响：M2/M7 规则效果中心无法生成真实 precision
- 可能性：高
- 缓解：使用历史 findings + 模拟标注（基于规则版本与人工状态推断）作为代理样本；在样本集中明确标注「代理标注」与「真实人工标注」比例；M7 最终验收时若仍无真实标注，需升级到用户决策
- 当前状态：MITIGATED（M2 已使用脚本生成 1,000 条代理样本；M7 最终评测时需用户提供真实标注子集以校准）

## R-011 SQLite UNIQUE 约束在 M4 无法直接去除

- 影响：M4 DUP-001 要求允许同 URL 多次入库，但 `policy_documents.url` 原始 schema 含 `UNIQUE` 约束，SQLite auto-index 不能用 `DROP INDEX` 删除
- 可能性：已发生
- 缓解：采用 SQLite 官方推荐的表重建方案：检测 UNIQUE → 捕获手动索引 → 重建新表（去 UNIQUE）→ 拷贝数据 → drop 旧表 → rename → 重建索引。幂等。
- 当前状态：MITIGATED（M4 已完成；表重建方案在 `app/migrations/m4.py` `_drop_url_unique_constraint` 中实现并通过 45 项测试）

## R-008 浏览器多角色验证在无浏览器环境无法执行

- 影响：M1/M6 真实浏览器验证受阻
- 可能性：中
- 缓解：使用 Playwright（已在 requirements.txt）headless 模式；若 Playwright 浏览器未安装，退化为 API 层权限矩阵测试 + UI 路由可达性静态检查
- 当前状态：OPEN

## R-009 主开发 AI 上下文超限

- 影响：长任务中断
- 可能性：中
- 缓解：每里程碑产物落盘到 `docs/milestones/Mx/` 与 `tmp/program_control/`；状态持久化到 `status.json`；自验摘要持久化到 JSON
- 当前状态：MITIGATED

## R-010 main 合并与生产切换不可逆

- 影响：生产事故
- 可能性：低
- 缓解：M7 默认只做候选包与切换准备；main 合并方案与 runbook 写入 `docs/final_candidate/`；实际合并与切换必须用户授权
- 当前状态：OPEN（M7 进入时再评估）

