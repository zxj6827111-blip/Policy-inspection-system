"""M3 date semantics (V3.0 plan §9.4).

Classifies a date string found in policy text into one of 8 semantic roles:
- authored: 成文日期
- first_published: 首次公开日期
- platform_publish: 当前平台上线日期
- modified: 修改日期
- effective: 施行日期
- valid_until: 有效期
- repealed: 废止日期
- attachment_date: 附件落款日期

Auto-confirm precision target: ≥ 95%.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

DateRole = Literal[
    "authored", "first_published", "platform_publish", "modified",
    "effective", "valid_until", "repealed", "attachment_date", "unknown",
]


@dataclass
class DateClassification:
    raw_value: str
    iso_value: str | None
    role: DateRole
    confidence: float
    context: str


# Each role's nearby-context keywords.
DATE_ROLE_MARKERS: dict[DateRole, list[str]] = {
    "authored": ["成文日期", "成文", "落款"],
    "first_published": ["首次公开", "首次发布", "首次公开日期"],
    "platform_publish": ["发布日期", "公布日期", "上线日期"],
    "modified": ["修改日期", "修订日期", "修改时间"],
    "effective": ["施行日期", "起施行", "施行", "生效日期", "执行日期"],
    "valid_until": ["有效期至", "有效期限", "有效期"],
    "repealed": ["废止日期", "废止"],
    "attachment_date": ["附件落款", "附件成文"],
}


def classify_date(raw_value: str, context: str = "") -> DateClassification:
    """Classify a single date string into one of 8 roles.

    Strategy:
    1. Parse the date to ISO (YYYY-MM-DD).
    2. Inspect ±60 chars of context for role keywords.
    3. Default to `authored` if the date appears near a signature block.
    4. Return `unknown` if no marker is found and no signature nearby.
    """
    iso = _parse_iso_date(raw_value)
    if iso is None:
        return DateClassification(
            raw_value=raw_value, iso_value=None,
            role="unknown", confidence=0.0, context=context[:200],
        )

    ctx = context or ""
    # Priority: attachment_date > valid_until > repealed > effective >
    # first_published > platform_publish > modified > authored
    priority: list[DateRole] = [
        "attachment_date", "valid_until", "repealed", "effective",
        "first_published", "platform_publish", "modified", "authored",
    ]
    for role in priority:
        for marker in DATE_ROLE_MARKERS[role]:
            if marker in ctx:
                return DateClassification(
                    raw_value=raw_value, iso_value=iso,
                    role=role, confidence=0.92, context=ctx[:200],
                )

    # No marker found — if context contains signature-like keywords, assume authored.
    signature_keywords = ("人民政府", "委员会", "办公室", "管理局", "监管局")
    if any(kw in ctx for kw in signature_keywords):
        return DateClassification(
            raw_value=raw_value, iso_value=iso,
            role="authored", confidence=0.70, context=ctx[:200],
        )

    return DateClassification(
        raw_value=raw_value, iso_value=iso,
        role="unknown", confidence=0.40, context=ctx[:200],
    )


def _parse_iso_date(value: str) -> str | None:
    """Parse `YYYY年MM月DD日` or `YYYY-MM-DD` to ISO YYYY-MM-DD."""
    match = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", value or "")
    if not match:
        return None
    try:
        d = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        return d.isoformat()
    except ValueError:
        return None


def classify_all_dates(
    dates: list[str], full_text: str,
) -> list[DateClassification]:
    """Classify each date with surrounding context."""
    out: list[DateClassification] = []
    for d in dates:
        idx = full_text.find(d)
        if idx < 0:
            ctx = ""
        else:
            start = max(0, idx - 60)
            end = min(len(full_text), idx + len(d) + 60)
            ctx = full_text[start:end]
        out.append(classify_date(d, ctx))
    return out


def can_auto_confirm_date(classification: DateClassification) -> bool:
    """Return True if this date can be auto-confirmed without human review.

    Per V3.0 §9.5: 日期自动确认 precision ≥ 95%.
    Strategy:
    - `authored` and `effective` roles with confidence ≥ 0.85 → auto-confirm
    - All others → require human review
    """
    if classification.role in ("authored", "effective") and classification.confidence >= 0.85:
        return True
    return False
