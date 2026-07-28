"""M3 document-number semantics (V3.0 plan §9.3).

Classifies each document number found in PDF/body text into one of 8 roles:
- main_file: 主文件文号 — the file being issued
- forwarded: 被转发文件文号 — file being forwarded
- referenced: 引用文号 — file cited as reference
- attachment: 附件文号 — file in an attachment
- joint: 联合发文文号 — joint issuing document
- announcement: 公告编号
- order: 令号
- none_label: 无文号文件

Accuracy target: ≥ 90% on main vs referenced classification.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

DocNumberRole = Literal[
    "main_file", "forwarded", "referenced", "attachment",
    "joint", "announcement", "order", "none_label",
]


@dataclass
class DocNumberClassification:
    raw_value: str
    normalized: str
    role: DocNumberRole
    confidence: float
    context: str


# Context markers for each role (simplified heuristics).
ROLE_MARKERS: dict[DocNumberRole, list[str]] = {
    "forwarded": ["转发", "批转", "印转"],
    "referenced": ["根据", "依据", "参照", "参见", "按照", "依据文件", "另见"],
    "attachment": ["附件", "附则"],
    "joint": ["联合发文", "联合印发的", "会同"],
    "announcement": ["公告"],
    "order": ["令", "第.*号令"],
    "main_file": ["印发", "发布", "公布"],
}


def classify_doc_number(raw_value: str, context: str = "") -> DocNumberClassification:
    """Classify a single document number into one of 8 roles.

    Heuristics (per V3.0 §9.3):
    - If `令` appears alone with a number, it's an `order` (令号).
    - If `公告` is in context, `announcement`.
    - If `转发/批转` is in context (after the number), `forwarded`.
    - If `根据/依据/参见` is in context (before the number), `referenced`.
    - If `附件` is in context, `attachment`.
    - If `联合发文/会同` is in context, `joint`.
    - If `印发/发布/公布` is in context (after the number), `main_file`.
    - Otherwise default to `main_file` (most policy documents are main files).
    """
    # Normalize
    normalized = raw_value.strip().replace("【", "〔").replace("】", "〕")
    normalized = re.sub(r"\s+", "", normalized)

    # Detect order (令号) — pattern like "第N号令"
    if re.search(r"第\s*\d+\s*号\s*令", context + normalized):
        return DocNumberClassification(
            raw_value=raw_value, normalized=normalized,
            role="order", confidence=0.92, context=context[:200],
        )
    # Detect announcement (公告编号) — `公告` nearby + number
    if "公告" in context and re.search(r"第\s*\d+\s*号", context):
        return DocNumberClassification(
            raw_value=raw_value, normalized=normalized,
            role="announcement", confidence=0.85, context=context[:200],
        )

    # Find the position of the doc number in context to determine before/after markers.
    idx = context.find(raw_value) if context else -1
    before_ctx = context[:idx] if idx >= 0 else context
    after_ctx = context[idx + len(raw_value):] if idx >= 0 else ""

    # "Referenced" markers typically appear BEFORE the number (根据 X 文件).
    referenced_before = any(m in before_ctx for m in ROLE_MARKERS["referenced"])
    # "Forwarded" markers typically appear BEFORE the number (转发 X).
    forwarded_before = any(m in before_ctx for m in ROLE_MARKERS["forwarded"])
    # "Main file" markers typically appear AFTER the number (印发 X 号文件).
    main_after = any(m in after_ctx for m in ROLE_MARKERS["main_file"])
    # "Joint" markers can appear before or after.
    joint_marker = any(m in context for m in ROLE_MARKERS["joint"])
    # "Attachment" markers typically appear BEFORE the number (附件 X).
    attachment_before = any(m in before_ctx for m in ROLE_MARKERS["attachment"])

    # Priority: attachment > forwarded > referenced > joint > main_file
    # (more specific roles win over the default main_file).
    if attachment_before:
        return DocNumberClassification(
            raw_value=raw_value, normalized=normalized,
            role="attachment", confidence=0.85, context=context[:200],
        )
    if forwarded_before:
        return DocNumberClassification(
            raw_value=raw_value, normalized=normalized,
            role="forwarded", confidence=0.85, context=context[:200],
        )
    if referenced_before and not main_after:
        return DocNumberClassification(
            raw_value=raw_value, normalized=normalized,
            role="referenced", confidence=0.85, context=context[:200],
        )
    if joint_marker:
        return DocNumberClassification(
            raw_value=raw_value, normalized=normalized,
            role="joint", confidence=0.80, context=context[:200],
        )
    if main_after:
        return DocNumberClassification(
            raw_value=raw_value, normalized=normalized,
            role="main_file", confidence=0.85, context=context[:200],
        )

    # Default: main_file (most policy docs are main issuances)
    return DocNumberClassification(
        raw_value=raw_value, normalized=normalized,
        role="main_file", confidence=0.60, context=context[:200],
    )


def classify_all_doc_numbers(
    numbers: list[str], full_text: str,
) -> list[DocNumberClassification]:
    """Classify each document number with its surrounding context (±80 chars)."""
    out: list[DocNumberClassification] = []
    for n in numbers:
        idx = full_text.find(n)
        if idx < 0:
            ctx = ""
        else:
            start = max(0, idx - 80)
            end = min(len(full_text), idx + len(n) + 80)
            ctx = full_text[start:end]
        out.append(classify_doc_number(n, ctx))
    return out
