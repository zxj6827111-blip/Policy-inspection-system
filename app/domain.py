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


class ScanPhase(StrEnum):
    """任务扫描阶段，供 UI 与恢复逻辑区分。"""

    IDLE = "idle"
    DISCOVERING = "discovering"
    PROCESSING = "processing"
    CATCHING_UP = "catching_up"
    INCREMENTAL_DISCOVERY = "incremental_discovery"
    FULL_RECONCILE = "full_reconcile"
    COOLING = "cooling"
    REVIEW = "review"


class GenerationItemStatus(StrEnum):
    PENDING = "pending"
    CHECKING = "checking"
    COMPLETED = "completed"
    RETRY = "retry"
    REVIEW = "review"
    REUSED = "reused"


@dataclass
class RelatedLink:
    kind: str
    url: str
    source_area: str = ""
    link_text: str = ""
    source_page_url: str = ""
    interaction_type: str = "click"
    visible: bool = True
    element_index: int = -1
    check_result: dict | None = None


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
    stable_id: str = ""
    content_fingerprint: str = ""
    api_record_id: str = ""
    doc_flag: str = ""


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
