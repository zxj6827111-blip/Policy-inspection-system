from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.db import Database, utc_now
from app.domain import Finding, PolicyListItem, PolicyRecord


PAGE_LINK_CHECK_VERSION = 1


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
        link_check_version: int = PAGE_LINK_CHECK_VERSION,
    ) -> None:
        """按位置写入；若同 job/target/url 已存在则更新该行，避免分页漂移覆盖错位。"""
        listed = item.published_date.isoformat() if item.published_date else None
        authored = authored_date.isoformat() if authored_date else None
        published = published_date.isoformat() if published_date else None
        missing_json = json.dumps(missing_fields or [], ensure_ascii=False)
        now = utc_now()
        with self.db.connect() as conn:
            # 有稳定身份时按 URL 更新，避免分页漂移覆盖；旧路径仍按页码位置唯一
            existing = None
            if getattr(item, "stable_id", None):
                existing = conn.execute(
                    """SELECT id FROM scan_item_results
                    WHERE job_id=? AND target_key=? AND url=?
                    ORDER BY checked_at DESC LIMIT 1""",
                    (job_id, item.source_key, item.url),
                ).fetchone()
            if existing:
                position_conflict = conn.execute(
                    """SELECT id FROM scan_item_results
                    WHERE job_id=? AND target_key=? AND page_number=? AND item_index=? AND id!=?""",
                    (job_id, item.source_key, item.page_number, item.item_index, int(existing["id"])),
                ).fetchone()
                item_index = item.item_index
                if position_conflict:
                    # Keep both audit rows when a dynamic list shifts a processed item.
                    item_index = -int(existing["id"])
                conn.execute(
                    """UPDATE scan_item_results SET page_number=?,item_index=?,title=?,listed_date=?,
                    detail_status=?,header_detected=?,source_id=?,topic_category=?,disclosure_attribute=?,
                    authored_date=?,page_document_number=?,published_date=?,issuing_agency=?,
                    missing_fields_json=?,document_id=?,reused_document_id=?,baseline_job_id=?,
                    link_check_version=?,reason=?,checked_at=?,source_label=?,channel_id=?
                    WHERE id=?""",
                    (
                        item.page_number, item_index, item.title, listed, detail_status,
                        int(header_detected), source_id, topic_category, disclosure_attribute,
                        authored, page_document_number, published, issuing_agency, missing_json,
                        document_id, reused_document_id, baseline_job_id, link_check_version, reason, now,
                        item.source_site, item.source_channel_id, int(existing["id"]),
                    ),
                )
                return
            conn.execute(
                """INSERT INTO scan_item_results(job_id,target_key,source_label,channel_id,page_number,item_index,title,url,
                listed_date,detail_status,header_detected,source_id,topic_category,disclosure_attribute,authored_date,
                page_document_number,published_date,issuing_agency,missing_fields_json,document_id,reused_document_id,
                baseline_job_id,link_check_version,reason,checked_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id,target_key,page_number,item_index) DO UPDATE SET title=excluded.title,url=excluded.url,
                listed_date=excluded.listed_date,detail_status=excluded.detail_status,header_detected=excluded.header_detected,
                source_id=excluded.source_id,topic_category=excluded.topic_category,
                disclosure_attribute=excluded.disclosure_attribute,authored_date=excluded.authored_date,
                page_document_number=excluded.page_document_number,published_date=excluded.published_date,
                issuing_agency=excluded.issuing_agency,missing_fields_json=excluded.missing_fields_json,
                document_id=excluded.document_id,reused_document_id=excluded.reused_document_id,
                baseline_job_id=excluded.baseline_job_id,link_check_version=excluded.link_check_version,
                reason=excluded.reason,checked_at=excluded.checked_at""",
                (
                    job_id, item.source_key, item.source_site, item.source_channel_id, item.page_number, item.item_index,
                    item.title, item.url, listed, detail_status,
                    int(header_detected), source_id, topic_category, disclosure_attribute,
                    authored, page_document_number, published, issuing_agency,
                    missing_json, document_id, reused_document_id,
                    baseline_job_id, link_check_version, reason, now,
                ),
            )

    def baseline_item(self, baseline_job_id: int, item: PolicyListItem) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute(
                """SELECT s.*,
                EXISTS(
                    SELECT 1 FROM link_checks l
                    WHERE l.job_id=s.job_id
                      AND l.document_id=COALESCE(s.document_id,s.reused_document_id)
                      AND (l.visible=0 OR l.review_status='legacy_hidden'
                           OR TRIM(COALESCE(l.source_area,''))=''
                           OR TRIM(COALESCE(l.source_page_url,''))='')
                ) AS has_legacy_hidden_links
                FROM scan_item_results s WHERE s.job_id=? AND s.target_key=? AND s.url=?
                ORDER BY s.checked_at DESC LIMIT 1""",
                (baseline_job_id, item.source_key, item.url),
            ).fetchone()
        return dict(row) if row else None

    def current_job_item(self, job_id: int, url: str) -> dict | None:
        """恢复任务时复用已落库的详情结果，避免同一 URL 再次访问目标站。"""
        with self.db.connect() as conn:
            row = conn.execute(
                """SELECT * FROM scan_item_results
                WHERE job_id=? AND url=? AND detail_status!='exception'
                ORDER BY CASE WHEN detail_status IN
                    ('checked_complete','checked_incomplete','no_header_pass') THEN 0 ELSE 1 END,
                    checked_at DESC LIMIT 1""",
                (job_id, url),
            ).fetchone()
        return dict(row) if row else None

    def scan_item_count(self, job_id: int) -> int:
        with self.db.connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM scan_item_results WHERE job_id=?", (job_id,)
            ).fetchone()[0])

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
                checked_at,redirect_chain_json,source_area,link_text,source_page_url,interaction_type,
                visible,review_status,evidence FROM link_checks
                WHERE job_id=? AND document_id=?
                  AND visible=1 AND review_status!='legacy_hidden'
                  AND TRIM(COALESCE(source_area,''))!=''
                  AND TRIM(COALESCE(source_page_url,''))!=''""",
                (baseline_job_id, document_id),
            ).fetchall()
            conn.executemany(
                """INSERT INTO link_checks(job_id,document_id,link_kind,original_url,final_url,status_code,result,
                error_type,page_title,checked_at,redirect_chain_json,source_area,link_text,source_page_url,
                interaction_type,visible,review_status,evidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id,document_id,original_url,source_area,source_page_url,link_text) DO UPDATE SET
                link_kind=excluded.link_kind,final_url=excluded.final_url,status_code=excluded.status_code,
                result=excluded.result,error_type=excluded.error_type,page_title=excluded.page_title,
                checked_at=excluded.checked_at,redirect_chain_json=excluded.redirect_chain_json,
                interaction_type=excluded.interaction_type,visible=excluded.visible,
                review_status=excluded.review_status,evidence=excluded.evidence""",
                [
                    (
                        job_id, document_id, row["link_kind"], row["original_url"], row["final_url"],
                        row["status_code"], row["result"], row["error_type"], row["page_title"],
                        row["checked_at"], row["redirect_chain_json"], row["source_area"], row["link_text"],
                        row["source_page_url"], row["interaction_type"], row["visible"], row["review_status"],
                        row["evidence"],
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
                error_type,page_title,checked_at,redirect_chain_json,source_area,link_text,source_page_url,
                interaction_type,visible,review_status,evidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(job_id,document_id,original_url,source_area,source_page_url,link_text) DO UPDATE SET link_kind=excluded.link_kind,
                final_url=excluded.final_url,status_code=excluded.status_code,result=excluded.result,
                error_type=excluded.error_type,page_title=excluded.page_title,checked_at=excluded.checked_at,
                redirect_chain_json=excluded.redirect_chain_json,interaction_type=excluded.interaction_type,
                visible=excluded.visible,review_status=excluded.review_status,evidence=excluded.evidence""",
                (job_id, document_id, result["kind"], result["url"], result.get("final_url", ""),
                 result.get("status_code"), result["result"], result.get("error_type", ""),
                 result.get("page_title", ""), utc_now(),
                 json.dumps(result.get("redirect_chain", []), ensure_ascii=False),
                 result.get("source_area", ""), result.get("link_text", ""), result.get("source_page_url", ""),
                 result.get("interaction_type", ""), int(bool(result.get("visible", True))),
                 result.get("review_status", "confirmed"), result.get("evidence", "")),
            )


    # ---- generation / inventory helpers (snapshot continuous scan) ----

    def create_generation(
        self, job_id: int, *, target_key: str = "", generation_kind: str = "full",
        query_contract: str = "", phase: str = "discovering",
    ) -> int:
        now = utc_now()
        with self.db.connect() as conn:
            cur = conn.execute(
                """INSERT INTO scan_generations(
                    job_id,target_key,phase,generation_kind,query_contract,started_at
                ) VALUES(?,?,?,?,?,?)""",
                (job_id, target_key, phase, generation_kind, query_contract, now),
            )
            return int(cur.lastrowid)

    def get_generation(self, generation_id: int) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM scan_generations WHERE id=?", (generation_id,)).fetchone()
        return dict(row) if row else None

    def latest_generation_for_job(self, job_id: int) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM scan_generations WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_generation(self, generation_id: int, **values) -> None:
        if not values:
            return
        allowed = {
            "phase", "observed_total", "discovered_count", "completed_count", "reused_count",
            "new_count", "retry_count", "review_count", "catchup_round", "list_cursor_page",
            "list_cursor_item", "finished_at", "stats_json", "target_key",
        }
        values = {k: v for k, v in values.items() if k in allowed}
        if not values:
            return
        sql = ",".join(f"{k}=?" for k in values)
        with self.db.connect() as conn:
            conn.execute(f"UPDATE scan_generations SET {sql} WHERE id=?", [*values.values(), generation_id])

    def upsert_generation_item(
        self, generation_id: int, job_id: int, item: PolicyListItem, *, is_new: bool | None = None,
    ) -> tuple[int, bool]:
        """幂等写入 generation_items。返回 (id, created_new_pending)。"""
        now = utc_now()
        stable = item.stable_id or f"{item.source_key}|url:{item.url}"
        listed = item.published_date.isoformat() if item.published_date else None
        with self.db.connect() as conn:
            existing = conn.execute(
                "SELECT id,status,content_fingerprint FROM generation_items WHERE generation_id=? AND stable_id=?",
                (generation_id, stable),
            ).fetchone()
            if existing:
                status = existing["status"]
                fp_changed = (existing["content_fingerprint"] or "") != (item.content_fingerprint or "")
                new_status = status
                if fp_changed and status in {"completed", "reused"}:
                    new_status = "pending"
                conn.execute(
                    """UPDATE generation_items SET title=?,url=?,listed_date=?,doc_flag=?,content_fingerprint=?,
                    page_number=?,item_index=?,api_record_id=?,last_seen_at=?,status=?,
                    last_error=CASE WHEN ?='pending' AND status IN ('completed','reused') THEN '' ELSE last_error END
                    WHERE id=?""",
                    (
                        item.title, item.url, listed, item.doc_flag or "", item.content_fingerprint or "",
                        item.page_number, item.item_index, item.api_record_id or "", now, new_status,
                        new_status, int(existing["id"]),
                    ),
                )
                created = False
                reopened = new_status == "pending" and status != "pending"
                return int(existing["id"]), reopened
            cur = conn.execute(
                """INSERT INTO generation_items(
                    generation_id,job_id,target_key,stable_id,api_record_id,title,url,listed_date,doc_flag,
                    content_fingerprint,page_number,item_index,status,first_seen_at,last_seen_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    generation_id, job_id, item.source_key, stable, item.api_record_id or "",
                    item.title, item.url, listed, item.doc_flag or "", item.content_fingerprint or "",
                    item.page_number, item.item_index, "pending", now, now,
                ),
            )
            return int(cur.lastrowid), True

    def claim_generation_item(self, job_id: int, target_key: str | None = None) -> dict | None:
        with self.db.connect() as conn:
            if target_key:
                row = conn.execute(
                    """SELECT * FROM generation_items
                    WHERE job_id=? AND target_key=? AND status IN ('pending','retry')
                    ORDER BY page_number,item_index,id LIMIT 1""",
                    (job_id, target_key),
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT * FROM generation_items
                    WHERE job_id=? AND status IN ('pending','retry')
                    ORDER BY page_number,item_index,id LIMIT 1""",
                    (job_id,),
                ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE generation_items SET status='checking',attempt_count=attempt_count+1 WHERE id=? AND status IN ('pending','retry')",
                (int(row["id"]),),
            )
            claimed = conn.execute("SELECT * FROM generation_items WHERE id=?", (int(row["id"]),)).fetchone()
        return dict(claimed) if claimed else None

    def complete_generation_item(
        self, item_id: int, *, status: str, detail_status: str = "", document_id: int | None = None,
        error: str = "",
    ) -> None:
        now = utc_now()
        completed_at = now if status in {"completed", "reused", "review"} else None
        with self.db.connect() as conn:
            conn.execute(
                """UPDATE generation_items SET status=?,detail_status=?,document_id=COALESCE(?,document_id),
                last_error=?,completed_at=COALESCE(?,completed_at) WHERE id=?""",
                (status, detail_status, document_id, error, completed_at, item_id),
            )

    def generation_item_counts(self, generation_id: int) -> dict[str, int]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS c FROM generation_items WHERE generation_id=? GROUP BY status",
                (generation_id,),
            ).fetchall()
        counts = {row["status"]: int(row["c"]) for row in rows}
        total = sum(counts.values())
        return {
            "total": total,
            "pending": counts.get("pending", 0),
            "checking": counts.get("checking", 0),
            "completed": counts.get("completed", 0),
            "reused": counts.get("reused", 0),
            "retry": counts.get("retry", 0),
            "review": counts.get("review", 0),
            **counts,
        }

    def generation_target_item_count(self, generation_id: int, target_key: str) -> int:
        with self.db.connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM generation_items WHERE generation_id=? AND target_key=?",
                (generation_id, target_key),
            ).fetchone()[0])
    def generation_items_for_job(self, job_id: int, *, status: str | None = None) -> list[dict]:
        with self.db.connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM generation_items WHERE job_id=? AND status=? ORDER BY id",
                    (job_id, status),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM generation_items WHERE job_id=? ORDER BY id", (job_id,),
                ).fetchall()
        return [dict(r) for r in rows]

    def mark_checking_as_retry(self, job_id: int) -> int:
        """进程中断时把 checking 改回 retry，保证可恢复。"""
        with self.db.connect() as conn:
            cur = conn.execute(
                "UPDATE generation_items SET status='retry' WHERE job_id=? AND status='checking'",
                (job_id,),
            )
            return int(cur.rowcount or 0)

    def upsert_source_inventory(self, item: PolicyListItem, generation_id: int) -> None:
        now = utc_now()
        stable = item.stable_id or f"{item.source_key}|url:{item.url}"
        listed = item.published_date.isoformat() if item.published_date else None
        with self.db.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM source_inventory WHERE target_key=? AND stable_id=?",
                (item.source_key, stable),
            ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE source_inventory SET title=?,url=?,listed_date=?,doc_flag=?,content_fingerprint=?,
                    last_seen_at=?,last_generation_id=?,is_present=1,api_record_id=? WHERE id=?""",
                    (
                        item.title, item.url, listed, item.doc_flag or "", item.content_fingerprint or "",
                        now, generation_id, item.api_record_id or "", int(existing["id"]),
                    ),
                )
            else:
                conn.execute(
                    """INSERT INTO source_inventory(
                        target_key,stable_id,api_record_id,title,url,listed_date,doc_flag,content_fingerprint,
                        first_seen_at,last_seen_at,last_generation_id,is_present
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)""",
                    (
                        item.source_key, stable, item.api_record_id or "", item.title, item.url, listed,
                        item.doc_flag or "", item.content_fingerprint or "", now, now, generation_id,
                    ),
                )

    def mark_inventory_absent(self, target_key: str, present_stable_ids: set[str]) -> int:
        """完整对账：不在当前列表中的条目标记为不再出现，不删除历史。"""
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT id,stable_id FROM source_inventory WHERE target_key=? AND is_present=1",
                (target_key,),
            ).fetchall()
            marked = 0
            for row in rows:
                if row["stable_id"] not in present_stable_ids:
                    conn.execute("UPDATE source_inventory SET is_present=0 WHERE id=?", (int(row["id"]),))
                    marked += 1
            return marked

    def mark_inventory_absent_for_generation(self, target_key: str, generation_id: int) -> int:
        """Use persisted generation membership so interrupted reconciliations resume safely."""
        with self.db.connect() as conn:
            cur = conn.execute(
                """UPDATE source_inventory SET is_present=0
                WHERE target_key=? AND is_present=1 AND COALESCE(last_generation_id, -1) != ?""",
                (target_key, generation_id),
            )
            return int(cur.rowcount or 0)
    def inventory_entry(self, target_key: str, stable_id: str) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM source_inventory WHERE target_key=? AND stable_id=?",
                (target_key, stable_id),
            ).fetchone()
        return dict(row) if row else None

    def update_inventory_detail(
        self, target_key: str, stable_id: str, *, detail_status: str, document_id: int | None,
    ) -> None:
        with self.db.connect() as conn:
            conn.execute(
                """UPDATE source_inventory SET last_detail_status=?,last_document_id=COALESCE(?,last_document_id)
                WHERE target_key=? AND stable_id=?""",
                (detail_status, document_id, target_key, stable_id),
            )

    def get_or_create_schedule(
        self, site_key: str, source_signature: list[str], *,
        incremental_hours: float = 6.0, reconcile_days: float = 7.0,
    ) -> dict:
        now = utc_now()
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM continuous_schedules WHERE site_key=?", (site_key,),
            ).fetchone()
            if row:
                return dict(row)
            from datetime import datetime, timedelta, timezone
            base = datetime.now(timezone.utc)
            next_inc = (base + timedelta(hours=float(incremental_hours))).isoformat()
            next_full = (base + timedelta(days=float(reconcile_days))).isoformat()
            conn.execute(
                """INSERT INTO continuous_schedules(
                    site_key,source_signature,enabled,incremental_interval_hours,full_reconcile_interval_days,
                    next_incremental_at,next_full_reconcile_at,updated_at
                ) VALUES(?,?,1,?,?,?,?,?)""",
                (
                    site_key, json.dumps(source_signature, ensure_ascii=False),
                    incremental_hours, reconcile_days, next_inc, next_full, now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM continuous_schedules WHERE site_key=?", (site_key,),
            ).fetchone()
        return dict(row)

    def update_schedule(self, site_key: str, **values) -> None:
        allowed = {
            "enabled", "last_incremental_at", "last_full_reconcile_at", "next_incremental_at",
            "next_full_reconcile_at", "last_job_id", "last_status", "updated_at",
            "incremental_interval_hours", "full_reconcile_interval_days", "source_signature",
        }
        values = {k: v for k, v in values.items() if k in allowed}
        if not values:
            return
        values.setdefault("updated_at", utc_now())
        sql = ",".join(f"{k}=?" for k in values)
        with self.db.connect() as conn:
            conn.execute(f"UPDATE continuous_schedules SET {sql} WHERE site_key=?", [*values.values(), site_key])

    def list_schedules(self) -> list[dict]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM continuous_schedules ORDER BY site_key").fetchall()
        return [dict(r) for r in rows]

    def schedule_for_last_job(self, job_id: int) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM continuous_schedules WHERE last_job_id=?", (job_id,),
            ).fetchone()
        return dict(row) if row else None
    def due_schedules(self, now_iso: str | None = None) -> list[dict]:
        now = now_iso or utc_now()
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM continuous_schedules WHERE enabled=1
                AND (
                    (next_incremental_at IS NOT NULL AND next_incremental_at<=?)
                    OR (next_full_reconcile_at IS NOT NULL AND next_full_reconcile_at<=?)
                )""",
                (now, now),
            ).fetchall()
        return [dict(r) for r in rows]
