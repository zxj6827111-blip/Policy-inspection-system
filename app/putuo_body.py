from __future__ import annotations

import re


_POLLUTION_MARKERS = (
    "跳转到网站导航区",
    "跳转到主要内容区域",
    "无障碍操作说明",
)
_FOOTER_MARKERS = (
    "\n分享：",
    "\n关闭窗口",
    "\n相关推荐",
    "\n上海市政府及各区网站",
)
_LAST_METADATA_RE = re.compile(
    r"(?m)^\s*公开主体\s*[:：]\s*(?:\n\s*)?[^\n]*\n+"
)


def is_polluted_putuo_body(text: str | None) -> bool:
    source = text or ""
    return any(marker in source[:500] for marker in _POLLUTION_MARKERS)


def clean_putuo_body_text(text: str | None, title: str = "") -> str:
    """Remove the known shpt.gov.cn page shell while leaving clean text untouched."""
    source = text or ""
    if not is_polluted_putuo_body(source):
        return source

    start = 0
    normalized_title = (title or "").strip()
    if normalized_title:
        title_pos = source.find(normalized_title)
        if title_pos >= 0:
            start = title_pos + len(normalized_title)

    candidate = source[start:]
    metadata_match = _LAST_METADATA_RE.search(candidate)
    if metadata_match:
        candidate = candidate[metadata_match.end():]

    end_positions = [
        candidate.find(marker)
        for marker in _FOOTER_MARKERS
        if candidate.find(marker) >= 0
    ]
    if end_positions:
        candidate = candidate[:min(end_positions)]

    cleaned = candidate.strip()
    return cleaned or source
