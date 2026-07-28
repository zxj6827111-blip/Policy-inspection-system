# 生产切换授权书（Production Cutover Authorization）

| 字段 | 值 |
|------|-----|
| 项目 | Policy-inspection-system |
| 授权日期 | 2026-07-28 |
| 授权方 | 独立总体验收 AI（第 3 轮 PASS） |
| 验收 HEAD | 375051376db09ad04bc94bc927464fd8d8216cbc |
| 整改代码 | c1c6102c1e5cf7ddc6cf280e5def70b1419e2c9c |

```text
FINAL_INDEPENDENT_ACCEPTANCE_PASS = true
READY_FOR_PRODUCTION_CUTOVER = true
PRODUCTION_CUTOVER_EXECUTED = false
```

## 授权范围

独立总验收 **PASS** 后，**允许**在用户**显式确认维护窗口**的前提下，由运维/开发按下列 runbook 执行生产切换准备与切换：

1. `docs/final_candidate/08_production_cutover_runbook.md`
2. `docs/final_candidate/09_rollback_runbook.md`
3. `docs/final_candidate/observation_checklist.md`

## 强制前置（切换前必须再核）

- [ ] 用户书面/明确授权本维护窗口
- [ ] 生产数据库完整备份 + SHA256 记录
- [ ] 生产 DB 基线 SHA256 仍为 `13E6A8A412F69C13D149674A31076854C966F5B151E670FC9E22D0F53AB77AF3` 或已批准的新基线
- [ ] 回滚演练（副本）通过
- [ ] 非 root、HTTPS、6001/应用端口收口按 runbook §10
- [ ] 管理员初始化（bootstrap_admin）完成
- [ ] 多角色实机冒烟（含 CSRF 写路径）

## 明确未授权

- 未授权在无用户确认时自动切换
- 未授权跳过备份/回滚演练
- 未授权 force-push / 无审查 merge
- 本文件**不等于** `FINAL_SEAL_PASS` 或 `OBSERVATION_PASS`

## 切换后必做

1. 按 observation_checklist 观察至少一个完整工作日  
2. 通过后生成 `docs/final_release/final_release_report.md` 与 `final_seal_report.md`  
3. 仅当观察通过才可声明整套改造封板完成  

## 签章（独立验收）

- 结论：独立总验收 PASS  
- 可进入生产切换流程：是（须用户授权窗口）  
- 已执行生产切换：否  
