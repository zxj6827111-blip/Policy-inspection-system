# M7 发布候选包与 main 合并方案

> 版本：V3.0
> 生成日期：2026-07-27
> 主开发 AI 自验产出，待独立总验收

本文档定义发布候选包（Release Candidate, RC）的组成、main 合并方案与 tag 候选。**主开发 AI 不直接执行合并、打 tag 或切生产**，仅准备方案，待用户授权后由独立总验收通过再执行。

---

## 1. 发布候选包组成

### 1.1 代码

- 分支：`feat/full-policy-inspection-upgrade`
- HEAD：`02cb6e22341e2940abb0e657d27b9ba2c7b2cae9`（M7 完成后）
- 工作区干净（除本次新增的最终交付文档）
- 测试基线：347 passed / 0 failed
- 1500 评测：Precision/Recall/F1 = 1.0000
- M6-8 演练：10/10 PASS

### 1.2 数据库迁移

- 全部幂等迁移（phase1 → m2 → m3 → m4 → m5 → m6）
- 通过 `Database.initialize()` 自动按序执行
- schema_migrations 表追踪已应用迁移
- 生产数据库 SHA256 未变（13E6A8A412F69C13D149674A31076854C966F5B151E670FC9E22D0F53AB77AF3）

### 1.3 最终交付文档（10 + 1 + 1）

- `docs/final_candidate/01_final_architecture.md`
- `docs/final_candidate/02_final_feature_matrix.md`
- `docs/final_candidate/03_final_rule_catalog.md`
- `docs/final_candidate/04_final_security_report.md`
- `docs/final_candidate/05_final_data_quality_report.md`
- `docs/final_candidate/06_final_test_report.md`
- `docs/final_candidate/07_final_migration_report.md`
- `docs/final_candidate/08_production_cutover_runbook.md`
- `docs/final_candidate/09_rollback_runbook.md`
- `docs/final_candidate/10_independent_acceptance_guide.md`
- `docs/final_candidate/observation_checklist.md`
- `docs/final_candidate/release_candidate_package.md`（本文档）

### 1.4 运维脚本

- `scripts/smoke_test.py` — 生产冒烟（15 步）
- `scripts/rehearsal_backup_restore.py` — 备份/恢复演练（10 步）
- `scripts/final_1500_evaluation.py` — 1500 条评测样本生成与评测

### 1.5 控制文件

- `docs/program_control/status.json`
- `docs/program_control/change_log.md`
- `docs/program_control/risk_register.md`
- `docs/program_control/final_task_matrix.md`
- `docs/program_control/full_upgrade_plan_v3.md`

### 1.6 里程碑报告

- `docs/milestones/M0/` ~ `docs/milestones/M7/`（每个 milestone 含 development_report.md + self_acceptance_report.md）

### 1.7 自验摘要

- `tmp/program_control/M0_self_acceptance.json` ~ `M7_self_acceptance.json`
- `tmp/program_control/final_development_summary.json`
- `tmp/program_control/M7_junit.xml`
- `tmp/program_control/M7_test_output.txt`
- `tmp/program_control/M7_1500_evaluation.json` / `.md`

---

## 2. main 合并方案

### 2.1 前置条件（必须全部满足）

1. 独立总验收 AI 完成 `10_independent_acceptance_guide.md` 中的全部 11 项门禁，结论为 PASS
2. 用户明确授权合并到 main
3. 维护窗口已确认
4. 生产数据库已备份且 SHA256 校验通过
5. 回滚演练通过（`scripts/rehearsal_backup_restore.py` 10/10 PASS）

### 2.2 合并策略：squash merge

```bash
# 1. 切到 main（或生产基线分支）
git checkout codex/fix-snapshot-result-collision
git pull origin codex/fix-snapshot-result-collision

# 2. 创建合并提交（squash 全部 M0-M7 commit）
git merge --squash feat/full-policy-inspection-upgrade

# 3. 编辑 commit message（使用下方模板）
git commit

# 4. 推送（仅在维护窗口内）
git push origin codex/fix-snapshot-result-collision
```

### 2.3 squash commit message 模板

```
feat: complete M0-M7 full policy inspection upgrade (V3.0)

- M0: git formalization, branch creation, phase1 baseline
- M1: auth/RBAC/CSRF/audit, API doc protection, fail-closed
- M2: batch review, dual-person review, arbitration, rule effect center
- M3: PDF text/OCR/three-way compare, attachment checks
- M4: archive anomaly, duplicate detection, dual-channel reconciliation
- M5: reading linkage, media checks, policy interpretation, rectification
- M6: 21-item nav UI, performance indexes, OCR queue, observability, backup/restore
- M7: full regression (347 passed), 1500-sample evaluation (P/R/F1=1.0), release candidate

Tests: 347 passed / 0 failed
Evaluation: 1500 samples, 74 rules, P=R=F1=1.0000
Rehearsal: 10/10 PASS
Production boundary: database SHA256 unchanged, no service restart

Branch: feat/full-policy-inspection-upgrade
Base: codex/fix-snapshot-result-collision (be74df7)
```

### 2.4 合并后验证

1. `git log -1` 确认 commit message
2. `git status` 确认工作区干净
3. 在生产副本上运行 `python -m pytest tests/ -q`，预期 347 passed
4. 运行 `python scripts/smoke_test.py`，预期 15/15 PASS

### 2.5 合并风险与缓解

| 风险 | 概率 | 缓解 |
|---|---|---|
| 合并冲突 | 低（基于生产基线分支创建） | 手动解决冲突后重新测试 |
| 迁移失败 | 低（幂等） | 回滚到 pre_cutover 备份 |
| 测试失败 | 低（已全量回归） | 修复后重新合并 |
| 性能回退 | 中（新增索引） | 观察 24 小时 metrics |

---

## 3. tag 候选

### 3.1 tag 命名

```
v3.0.0-rc1
```

### 3.2 tag 创建命令（仅在合并到 main 后执行）

```bash
git tag -a v3.0.0-rc1 -m "Release Candidate 1: M0-M7 full policy inspection upgrade V3.0"
git push origin v3.0.0-rc1
```

### 3.3 tag 候选清单

| tag | 含义 | 状态 |
|---|---|---|
| v3.0.0-rc1 | 第一个发布候选（M0-M7 完成，待独立总验收） | 候选 |
| v3.0.0-rc2 | 第二个发布候选（独立总验收修复后） | 待定 |
| v3.0.0 | 正式发布（独立总验收 PASS + 用户授权生产切换） | 待定 |

### 3.4 tag 内容验证

打 tag 前必须确认：
- [ ] HEAD 是合并后的 main 分支
- [ ] 工作区干净
- [ ] 全量回归 347 passed
- [ ] 1500 评测 PASS
- [ ] 演练 10/10 PASS
- [ ] 冒烟 15/15 PASS
- [ ] 生产数据库 SHA256 已记录

---

## 4. 发布候选包归档

### 4.1 归档目录结构

```
release_candidate_v3.0.0-rc1/
├── code/                          # 代码快照（git archive）
├── docs/
│   ├── final_candidate/           # 11 个文档
│   ├── milestones/                # M0-M7 报告
│   └── program_control/           # 控制文件
├── scripts/                       # 运维脚本
├── tmp/
│   └── program_control/           # 自验摘要、测试输出、评测报告
└── RELEASE_NOTES.md               # 发布说明
```

### 4.2 归档命令

```bash
# 创建归档目录
mkdir -p release_candidate_v3.0.0-rc1

# 代码快照
git archive --format=tar feat/full-policy-inspection-upgrade | \
    tar -x -C release_candidate_v3.0.0-rc1/code

# 复制文档与脚本
cp -r docs release_candidate_v3.0.0-rc1/
cp -r scripts release_candidate_v3.0.0-rc1/
cp -r tmp release_candidate_v3.0.0-rc1/

# 计算 SHA256
find release_candidate_v3.0.0-rc1 -type f -exec sha256sum {} \; > \
    release_candidate_v3.0.0-rc1/SHA256SUMS

# 打包
tar -czf release_candidate_v3.0.0-rc1.tar.gz release_candidate_v3.0.0-rc1/
sha256sum release_candidate_v3.0.0-rc1.tar.gz > release_candidate_v3.0.0-rc1.tar.gz.sha256
```

### 4.3 RELEASE_NOTES.md 模板

```markdown
# Release Candidate v3.0.0-rc1

## 概述
上海区级政策文件安全巡检系统 M0-M7 完整改造发布候选。

## 测试结果
- 全量回归：347 passed / 0 failed
- 1500 条评测：74 规则，P/R/F1 = 1.0000
- M6-8 演练：10/10 PASS

## 生产边界
- 生产数据库 SHA256 未变
- 生产服务未重启
- 未修改 Nginx/PM2/证书

## 待办
- 独立总验收
- 用户授权生产切换
- 维护窗口确认

## 回滚
参见 docs/final_candidate/09_rollback_runbook.md
```

---

## 5. 执行边界

主开发 AI **不执行**以下操作：
- `git merge` 到 main
- `git tag` 创建 tag
- `git push` 推送 main 或 tag
- 生产数据库迁移
- 生产服务重启
- Nginx/PM2 配置修改

以上操作仅在以下条件全部满足后由用户或独立总验收 AI 执行：
1. 独立总验收 PASS
2. 用户明确授权
3. 备份完成
4. 回滚演练通过
5. 维护窗口已确认

---

## 6. 结论

本发布候选包已准备就绪，待独立总验收通过后由用户授权执行 main 合并、打 tag 与生产切换。
