from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.db import Database, utc_now
from app.domain import Finding, PolicyListItem, PolicyRecord


class Repository:
    def __init__(self, db: Database):
        self.db = db

    def save_record(self, job_id: int, record: PolicyRecord, findings: list[Finding]) -> int:
        raw = asdict(record)
        raw["published_date"] = record.published_date.isoformat() if record.published_date else None
        raw["authored_date"] = record.authored_date.isoformat() if record.authored_date else None
        content_hash = hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        now = utc_now()
        with self.db.connect() as conn:
            existing = conn.execute("SELECT id FROM policy_documents WHERE url=?", (record.url,)).fetchone()
            if existing:
                document_id = int(existing["id"])
                conn.execute(
                    """UPDATE policy_documents SET district=?,source_id=?,source_site=?,title=?,issuing_agency=?,page_document_number=?,
                    published_date=?,authored_date=?,body_text=?,topic_category=?,disclosure_attribute=?,content_hash=?,last_seen_job_id=?,last_seen_at=?,
                    last_detail_checked_at=? WHERE id=?""",
                    (record.district, record.source_id, record.source_site, record.title, record.issuing_agency, record.page_document_number,
                     raw["published_date"], raw["authored_date"], record.body_text, record.topic_category,
                     record.disclosure_attribute, content_hash, job_id, now, now, document_id),
                )
            else:
                cur = conn.execute(
                    """INSERT INTO policy_documents(district,source_id,source_site,url,title,issuing_agency,page_document_number,
                    published_date,authored_date,body_text,topic_category,disclosure_attribute,content_hash,last_detail_checked_at,first_seen_job_id,
                    last_seen_job_id,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (record.district, record.source_id, record.source_site, record.url, record.title, record.issuing_agency,
                     record.page_document_number, raw["published_date"], raw["authored_date"], record.body_text,
                     record.topic_category, record.disclosure_attribute, content_hash, now, job_id, job_id, now, now),
                )
                document_id = int(cur.lastrowid)
            conn.execute(
                "INSERT OR REPLACE INTO document_snapshots(job_id,document_id,captured_at,raw_json) VALUES(?,?,?,?)",
                (job_id, document_id, now, json.dumps(raw, ensure_ascii=False)),
            )
            conn.execute("DELETE FROM rule_findings WHERE job_id=? AND document_id=?", (job_id, document_id))
            conn.executemany(
                """INSERT INTO rule_findings(job_id,document_id,rule_code,category,severity,review_status,detail,
                page_value,body_value,evidence,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                [(job_id, document_id, f.rule_code, f.category, f.severity, f.status, f.detail,
                  f.page_value, f.body_value, f.evidence, now) for f in findings],
            )
            return document_id

    def record_scan_item(
        self, job_id: int, item: PolicyListItem, *, detail_status: str, header_detected: bool = False,
        source_id: str = "", topic_category: str = "", disclosure_attribute: str = "",
        authored_date: date | None = None, page_document_number: str = "", published_date: date | None = None,
        issuing_agency: str = "", missing_fields: list[str] | None = None, document_id: int | None = None,
        reused_document_id: int | None = None, baseline_job_id: int | None = None, reason: str = "",
    ) -> None:
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO scan_item_results(job_id,target_key,source_label,channel_id,page_number,item_index,title,url,
                listed_date,detail_status,header_detected,source_id,topic_category,disclosure_attribute,authored_date,
                page_document_number,published_date,issuing_agency,missing_fields_json,document_id,reused_document_id,
                baseline_job_id,reason,checked_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id,target_key,page_number,item_index) DO UPDATE SET title=excluded.title,url=excluded.url,
                listed_date=excluded.listed_date,detail_status=excluded.detail_status,header_detected=excluded.header_detected,
                source_id=excluded.source_id,topic_category=excluded.topic_category,
                disclosure_attribute=excluded.disclosure_attribute,authored_date=excluded.authored_date,
                page_document_number=excluded.page_document_number,published_date=excluded.published_date,
                issuing_agency=excluded.issuing_agency,missing_fields_json=excluded.missing_fields_json,
                document_id=excluded.document_id,reused_document_id=excluded.reused_document_id,
                baseline_job_id=excluded.baseline_job_id,reason=excluded.reason,checked_at=excluded.checked_at""",
                (
                    job_id, item.source_key, item.source_site, item.source_channel_id, item.page_number, item.item_index,
                    item.title, item.url, item.published_date.isoformat() if item.published_date else None, detail_status,
                    int(header_detected), source_id, topic_category, disclosure_attribute,
                    authored_date.isoformat() if authored_date else None, page_document_number,
                    published_date.isoformat() if published_date else None, issuing_agency,
                    json.dumps(missing_fields or [], ensure_ascii=False), document_id, reused_document_id,
                    baseline_job_id, reason, utc_now(),
                ),
            )

    def baseline_item(self, baseline_job_id: int, item: PolicyListItem) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute(
                """SELECT * FROM scan_item_results WHERE job_id=? AND target_key=? AND url=?
                ORDER BY checked_at DESC LIMIT 1""",
                (baseline_job_id, item.source_key, item.url),
            ).fetchone()
        return dict(row) if row else None

    def copy_baseline_findings(self, baseline_job_id: int, job_id: int, document_id: int) -> list[Finding]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT rule_code,category,severity,review_status,detail,page_value,body_value,evidence
                FROM rule_findings WHERE job_id=? AND document_id=?""",
                (baseline_job_id, document_id),
            ).fetchall()
            now = utc_now()
            conn.executemany(
                """INSERT INTO rule_findings(job_id,document_id,rule_code,category,severity,review_status,detail,
                page_value,body_value,evidence,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        job_id, document_id, row["rule_code"], row["category"], row["severity"],
                        row["review_status"], row["detail"], row["page_value"], row["body_value"],
                        row["evidence"], now,
                    )
                    for row in rows
                ],
            )
        return [
            Finding(
                row["rule_code"], row["category"], row["severity"], row["review_status"], row["detail"],
                row["page_value"], row["body_value"], row["evidence"],
            ) for row in rows
        ]

    def copy_baseline_link_checks(self, baseline_job_id: int, job_id: int, document_id: int) -> None:
        """沿用未变化文件的外链检查结论，保证增量导出仍能呈现既有外链问题。"""
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT link_kind,original_url,final_url,status_code,result,error_type,page_title,
                checked_at,redirect_chain_json FROM link_checks
                WHERE job_id=? AND document_id=?""",
                (baseline_job_id, document_id),
            ).fetchall()
            conn.executemany(
                """INSERT INTO link_checks(job_id,document_id,link_kind,original_url,final_url,status_code,result,
                error_type,page_title,checked_at,redirect_chain_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id,document_id,original_url) DO UPDATE SET
                link_kind=excluded.link_kind,final_url=excluded.final_url,status_code=excluded.status_code,
                result=excluded.result,error_type=excluded.error_type,page_title=excluded.page_title,
                checked_at=excluded.checked_at,redirect_chain_json=excluded.redirect_chain_json""",
                [
                    (
                        job_id, document_id, row["link_kind"], row["original_url"], row["final_url"],
                        row["status_code"], row["result"], row["error_type"], row["page_title"],
                        row["checked_at"], row["redirect_chain_json"],
                    )
                    for row in rows
                ],
            )

    def document(self, document_id: int) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM policy_documents WHERE id=?", (document_id,)).fetchone()
        return dict(row) if row else None

    def incremental_decision(
        self, url: str, title: str, published_date: date | None, refresh_days: int = 30,
        job_id: int | None = None,
    ) -> tuple[bool, str, int | None]:
        """只有稳定元数据一致且近期检查过正文时才跳过详情。"""
        with self.db.connect() as conn:
            row = conn.execute(
                """SELECT d.id,d.title,d.published_date,d.last_detail_checked_at,j.action
                FROM policy_documents d LEFT JOIN scan_job_documents j ON j.document_id=d.id AND j.job_id=?
                WHERE d.url=?""", (job_id or -1, url)
            ).fetchone()
        if not row:
            return False, "新增文件", None
        if row["action"] == "checking_links":
            return False, "上次关联链接检查未完成", int(row["id"])
        if row["title"].strip() != title.strip():
            return False, "列表标题发生变化", int(row["id"])
        listed_date = published_date.isoformat() if published_date else None
        if listed_date and row["published_date"] != listed_date:
            return False, "发布日期发生变化", int(row["id"])
        checked = row["last_detail_checked_at"]
        if not checked:
            return False, "历史记录尚未建立正文复检时间", int(row["id"])
        try:
            checked_at = datetime.fromisoformat(checked)
            if checked_at.tzinfo is None:
                checked_at = checked_at.replace(tzinfo=timezone.utc)
        except ValueError:
            return False, "正文复检时间无效", int(row["id"])
        if datetime.now(timezone.utc) - checked_at > timedelta(days=refresh_days):
            return False, f"距上次正文检查超过{refresh_days}天", int(row["id"])
        return True, "URL、标题和发布日期未变化，且正文近期已检查", int(row["id"])

    def record_job_document(
        self, job_id: int, document_id: int, district: str, page_number: int,
        item_index: int, action: str, reason: str = ""
    ) -> None:
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO scan_job_documents(job_id,document_id,district,page_number,item_index,action,reason,recorded_at)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(job_id,document_id) DO UPDATE SET
                page_number=excluded.page_number,item_index=excluded.item_index,action=excluded.action,
                reason=excluded.reason,recorded_at=excluded.recorded_at""",
                (job_id, document_id, district, page_number, item_index, action, reason, now),
            )
            conn.execute(
                "UPDATE policy_documents SET last_seen_job_id=?,last_seen_at=? WHERE id=?",
                (job_id, now, document_id),
            )

    def record_scan_exception(
        self, job_id: int, item: PolicyListItem, category: str, message: str
    ) -> None:
        """保存单条政策异常，不写入不完整的政策正文。"""
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO scan_exceptions(job_id,district,page_number,item_index,title,url,category,
                first_error,last_error,status,retry_count,first_seen_at,last_checked_at,resolved_at)
                VALUES(?,?,?,?,?,?,?,?,?,'pending',0,?,?,NULL)
                ON CONFLICT(job_id,url) DO UPDATE SET district=excluded.district,page_number=excluded.page_number,
                item_index=excluded.item_index,title=excluded.title,category=excluded.category,
                last_error=excluded.last_error,status='pending',last_checked_at=excluded.last_checked_at,resolved_at=NULL""",
                (job_id, item.district, item.page_number, item.item_index, item.title, item.url, category,
                 message, message, now, now),
            )

    def pending_scan_exceptions(self, job_id: int, district: str) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM scan_exceptions WHERE job_id=? AND district=? AND status='pending'
                ORDER BY page_number,item_index""",
                (job_id, district),
            ).fetchall()
        return [dict(row) for row in rows]

    def resolve_scan_exception(self, exception_id: int) -> None:
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                """UPDATE scan_exceptions SET status='resolved',retry_count=retry_count+1,last_checked_at=?,
                resolved_at=?,last_error='' WHERE id=?""",
                (now, now, exception_id),
            )

    def fail_scan_exception_retest(self, exception_id: int, message: str) -> None:
        now = utc_now()
        with self.db.connect() as conn:
            conn.execute(
                """UPDATE scan_exceptions SET status='review_required',retry_count=retry_count+1,
                last_error=?,last_checked_at=? WHERE id=?""",
                (message, now, exception_id),
            )

    def save_link_check(self, job_id: int, document_id: int, result: dict[str, Any]) -> None:
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO link_checks(job_id,document_id,link_kind,original_url,final_url,status_code,result,
                error_type,page_title,checked_at,redirect_chain_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id,document_id,original_url) DO UPDATE SET link_kind=excluded.link_kind,
                final_url=excluded.final_url,status_code=excluded.status_code,result=excluded.result,
                error_type=excluded.error_type,page_title=excluded.page_title,checked_at=excluded.checked_at,
                redirect_chain_json=excluded.redirect_chain_json""",
                (job_id, document_id, result["kind"], result["url"], result.get("final_url", ""),
                 result.get("status_code"), result["result"], result.get("error_type", ""),
                 result.get("page_title", ""), utc_now(),
                 json.dumps(result.get("redirect_chain", []), ensure_ascii=False)),
            )
