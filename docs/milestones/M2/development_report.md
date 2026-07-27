# M2 开发报告：人工标注与规则效果治理

- 里程碑：M2
- 分支：feat/full-policy-inspection-upgrade
- 起始 HEAD：9ddcf2117cd55e55c49e50309560e2cb31d35f8d
- 完成时间：2026-07-27
- 主开发 AI：primary_dev_ai

## 范围

按 V3.0 统一执行版 M2 要求：

- 批量复核
- 双人复核
- 仲裁
- 排除原因
- 规则效果中心
- 1,000 条样本抽取和评测
- 未达标规则降级

## 设计

### 数据库迁移（app/migrations/m2.py）

新增/扩展对象，幂等可重入：

- `review_assignments`：双人复核分配与仲裁状态跟踪
  - 字段：first/second reviewer、arbitration_status(none/pending/resolved/single)、consistency(pending/agree/disagree/arbitrated)、arbitrator、resolved_at
  - 唯一约束：`UNIQUE(finding_id)`
- `rule_effect_snapshots`：规则效果快照（live/sample/evaluation）
  - 字段：trigger_count、reviewed_count、confirmed/dismissed/needs_more、precision_value、false_positive_rate、top_dismiss_reasons_json、auto_confirm_allowed、degraded、sample_size
  - 唯一约束：`UNIQUE(snapshot_at, job_id, rule_code, snapshot_kind)`
- `finding_reviews` 新增 `review_round` (1/2/3，CHECK 约束)
- `rule_versions` 新增 `auto_confirm_allowed`、`degraded`、`degraded_reason`、`degraded_at`
- 索引：`idx_review_assignments_job`、`idx_review_assignments_arbitration`、`idx_rule_effect_rule`

### 阈值（V2.0/V3.0 规定）

- 自动确认规则：precision ≥ 0.95
- 疑似规则：precision ≥ 0.80
- 评测样本量下限：reviewed_count ≥ 10 才参与降级判定

### 服务层

- `app/review_batch.py`：`batch_review`（批量决策，append-only `finding_reviews`）+ `cluster_findings`（按 rule_code/district/source_site/document_number_prefix 聚类）
- `app/review_dual.py`：`assign_dual_review`、`submit_dual_review`（双轮，遇一致直接落库）、`arbitrate`（仲裁终局）、`list_dual_assignments`、`compute_consistency_rate`
- `app/rule_effect.py`：`compute_rule_effectiveness`（precision、FPR、top5 排除原因）+ `snapshot_rule_effectiveness`
- `app/rule_degradation.py`：`evaluate_rule_degradation`（基于阈值降级或恢复，原子更新 `rule_versions`）

### API 层（app/main.py）

| 方法 | 路径 | 鉴权 | 用途 |
|---|---|---|---|
| POST | `/api/findings/batch-review` | review:write | 批量决策 |
| GET  | `/api/jobs/{job_id}/finding-clusters` | login | 聚类选择 |
| POST | `/api/findings/dual-assign` | review:write | 分配双人 |
| POST | `/api/findings/{finding_id}/dual-review` | review:write | 提交一轮 |
| POST | `/api/findings/{finding_id}/arbitrate` | review:write | 仲裁 |
| GET  | `/api/jobs/{job_id}/dual-assignments` | login | 列出分配 |
| GET  | `/api/consistency-rate` | login | 一致率 |
| GET  | `/api/rule-effect` | login | 规则效果 |
| POST | `/api/rule-effect/snapshot` | review:write | 持久化快照 |
| POST | `/api/rule-effect/degrade` | review:write | 降级评估 |

所有写操作通过 `require_write("review:write")` 完成 CSRF + 角色校验，并写入 `operation_audits`。

### 1,000 条样本评测

`scripts/generate_m2_samples.py`：

- 分配：AGENCY-001 300 / DATE-001 200 / DOC-001 200 / META-001 100 / LINK-001 200
- 代理标签基于已知规则精度启发式（R-007）：
  - AGENCY-001 precision=0.30（hint 规则）
  - DATE-001 precision=0.95（严格自动确认）
  - DOC-001 precision=0.92
  - META-001 precision=0.90
  - LINK-001 precision=0.70
- 通过 `batch_review` 灌入 human_confirmed / human_dismissed 标签
- `evaluate_samples` 调用 `compute_rule_effectiveness` 输出每规则 precision/FPR

## 测试

`tests/test_m2_review_rule_effect.py` 15 个用例：

- 批量复核：确认、跳过未知 ID、viewer 403、按规则聚类、非法维度
- 双人复核：一致、争议后仲裁、同一人拒绝、未争议不可仲裁
- 规则效果：precision 计算、未登录 401、快照写入
- 规则降级：低精度降级、高精度保持
- 1,000 条样本评测：分配计数、标签应用、precision 范围 0..1、evaluation 快照

## 数据库副本验证

M2 迁移在测试夹具的临时 DB（`tmp_path/m2.db`）中执行，全部 15 用例通过；幂等性由 `schema_migrations` 表 + `IF NOT EXISTS` 保证。

## 生产边界

- 未修改生产数据库
- 未重启生产服务
- 未修改 Nginx / PM2 / 证书 / 系统用户
- 未执行外部扫描

## 自验结果

- 全量回归：211 passed
- M2 专项：15 passed
- 阻断项：无
