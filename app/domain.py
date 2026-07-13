from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COOLING = "cooling"
    COMPLETED = "completed"
    PARTIAL = "partial"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass
class RelatedLink:
    kind: str
    url: str


@dataclass
class PolicyListItem:
    district: str
    page_number: int
    item_index: int
    title: str
    url: str
    published_date: date | None = None
    related_links: list[RelatedLink] = field(default_factory=list)
    source_site: str = ""
    source_key: str = ""
    source_channel_id: str = ""


@dataclass
class PolicyRecord:
    district: str
    title: str
    url: str
    source_id: str = ""
    issuing_agency: str = ""
    page_document_number: str = ""
    published_date: date | None = None
    authored_date: date | None = None
    body_text: str = ""
    body_document_numbers: list[str] = field(default_factory=list)
    related_links: list[RelatedLink] = field(default_factory=list)
    source_site: str = ""
    topic_category: str = ""
    disclosure_attribute: str = ""
    header_detected: bool = False
    missing_metadata_fields: list[str] = field(default_factory=list)


@dataclass
class DetailInspection:
    record: PolicyRecord | None
    header_detected: bool
    missing_fields: list[str] = field(default_factory=list)
    invalid_fields: list[str] = field(default_factory=list)


@dataclass
class Finding:
    rule_code: str
    category: str
    severity: str
    status: str
    detail: str
    page_value: str = ""
    body_value: str = ""
    evidence: str = ""


class SafetyPause(RuntimeError):
    """访问策略要求立即暂停任务。"""


class CooldownPause(SafetyPause):
    """访问风险触发的临时冷却，冷却结束后可以自动恢复。"""


class ItemReviewRequired(SafetyPause):
    """单条政策详情异常；记录后可继续扫描其它条目。"""

    def __init__(self, message: str, category: str = "detail_load"):
        super().__init__(message)
        self.category = category
