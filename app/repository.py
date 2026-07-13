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
                    published_date=?,authored_date=?,body_text=?,content_hash=?,last_seen_job_id=?,last_seen_at=?,
                    last_detail_checked_at=? WHERE id=?""",
                    (record.district, record.source_id, record.source_site, record.title, record.issuing_agency, record.page_document_number,
                     raw["published_date"], raw["authored_date"], record.body_text, content_hash, job_id, now, now, document_id),
                )
            else:
                cur = conn.execute(
                    """INSERT INTO policy_documents(district,source_id,source_site,url,title,issuing_agency,page_document_number,
                    published_date,authored_date,body_text,content_hash,last_detail_checked_at,first_seen_job_id,
                    last_seen_job_id,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (record.district, record.source_id, record.source_site, record.url, record.title, record.issuing_agency,
                     record.page_document_number, raw["published_date"], raw["authored_date"], record.body_text,
                     content_hash, now, job_id, job_id, now, now),
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
