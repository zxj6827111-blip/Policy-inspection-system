# Phase 1 UI Changes

## Auth bar

- Login / logout controls in top bar
- Session cookie + CSRF header injected by `api()` helper

## Finding classification chips

- 确认问题
- 待复核线索
- 数据不足
- 采集异常
- 自动排除
- 人工排除
- 客户正式

Backed by `/api/jobs/{id}/review-queue` `bucket_counts` and `/finding-summary`.

## Evidence dialog

- Dynamic `evidence_fields` list (label + value)
- For AGENCY/ORG: 页面公开主体、实际发文机关、联合发文机关、文号机关代字、知识库匹配、正文证据、机器状态、置信度、规则版本、人工状态
- Legacy page/body nodes retained but not the sole presentation

## Stats

Customer formal counts should be used for “对外问题” messaging; raw finding_count may still reflect all machine clues until job counter is split in a later phase.
