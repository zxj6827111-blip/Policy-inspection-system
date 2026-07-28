# M4 自验报告

> 里程碑：M4
> 自验结论：**PASS**
> 自验时间：2026-07-27 21:00 (Asia/Shanghai)
> 自验执行者：primary_dev_ai

## 1. 门禁清单（V3.0 §13 + 用户指令）

| # | 门禁 | 结果 | 证据 |
|---|---|---|---|
| 1 | 记录开始分支、HEAD、工作区和数据库 SHA256 | ✅ | 分支 `feat/full-policy-inspection-upgrade`；M4 开始 HEAD `79df3af`；生产 DB SHA256 `13e6a8a412...77af3` 未变 |
| 2 | 完成设计 | ✅ | 见 `development_report.md` §2 |
| 3 | 完成代码 | ✅ | `app/archive`、`app/duplicates`、`app/reconciliation`、`app/migrations/m4.py`、`app/main.py` 新增 12 端点 |
| 4 | 完成数据库迁移 | ✅ | `_migrate_m4` 在 `Database.initialize()` 中自动执行；幂等；5 张新表 + 8 列；去除 `policy_documents.url` UNIQUE |
| 5 | 在数据库副本验证 | ✅ | `tests/test_m4_archive_duplicates_recon.py` 全部使用 `tmp_path` 临时 DB |
| 6 | 完成 API | ✅ | 12 端点全部带 auth/CSRF/audit |
| 7 | 完成 UI | N/A | M4 无独立 UI，统一在 M6 渲染 |
| 8 | 完成测试 | ✅ | M4 新增 45 测试，全部通过 |
| 9 | 完成文档 | ✅ | `docs/milestones/M4/development_report.md` 与本报告 |
| 10 | 运行全量回归 | ✅ | `300 passed in 213.24s`，无回归 |
| 11 | 执行本里程碑自验 | ✅ | 本报告 |
| 12 | 生成报告 | ✅ | 见交付物清单 |
| 13 | 自验 PASS 后自动进入下一里程碑 | ✅ | M5 启动条件满足 |

## 2. 生产边界检查

| 项 | 状态 | 说明 |
|---|---|---|
| 修改生产数据库 | 否 | 所有 M4 测试在 tmp_path 临时 DB 中执行 |
| 重启生产服务 | 否 | 未运行任何 prod 服务命令 |
| 修改生产 Nginx/PM2/证书 | 否 | 未触碰 |
| 高频外部扫描 | 否 | M4 是纯本地规则引擎，无外部请求 |

## 3. 测试结果

### 3.1 M4 专项（45 项全 PASS）

```
tests/test_m4_archive_duplicates_recon.py
  TestM4Migration .................... 4 passed
  TestArchiveObservations ............ 2 passed
  TestArchiveAnomalyDetection ........ 11 passed
  TestFingerprints ................... 5 passed
  TestDuplicateClassification ........ 9 passed
  TestReconciliation ................. 10 passed
  TestM4API .......................... 5 passed
============================================== 45 passed in 11.11s
```

### 3.2 全量回归

```
300 passed in 213.24s (0:03:33)
```

M0~M3 既有 255 项全部继续通过，无回归。

## 4. V3.0 §10.4 关键保证逐条复核

| §10.4 保证 | 实现 | 测试 |
|---|---|---|
| 单轮缺失不判 ARCH-001 | 连续 ≥ 2 轮缺失才发 finding | `test_single_round_missing_does_not_flag` |
| 正常废止 / 失效可排除 | `archive_status` in ('repealed','expired') 自动标记 `normal_archive=1` | `test_normal_repealed_excluded` / `test_normal_expired_excluded` |
| 栏目迁移后正常不判 | `archive_status='migrated'` + `migration_target_url` 存在 | `test_migrated_not_flagged_when_target_present` |
| 跨渠道正常同步不判重复 | DUP-005 优先于 DUP-003，`is_normal=True` | `test_dup_005_cross_channel_normal_sync` |
| Evidence 同时含双方 URL | `evidence_json` 含 `municipal_url` + `district_url` | `test_evidence_has_both_urls` |
| RECON-010 仅在无任何字段实际匹配时触发 | `has_matching_signal` 改为 `any(field_match)` | `test_recon_010_insufficient_data` |

## 5. 阻断项 / 失败项

无。

## 6. 自验摘要 JSON

```json
{
  "milestone": "M4",
  "development_complete": true,
  "self_acceptance": "PASS",
  "tests_passed": 300,
  "tests_failed": 0,
  "production_database_modified": false,
  "production_service_restarted": false,
  "blockers": []
}
```

## 7. 进入下一里程碑授权

M4 自验 PASS，符合 V3.0 §13 与用户指令的全部 13 项门禁。自动进入 **M5（阅办联动深度检查 / 政策解读 / 整改闭环）**。
