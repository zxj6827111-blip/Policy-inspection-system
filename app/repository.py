from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.db import Database, utc_now
from app.findings_model import RECTIFIED_BY_LATER_SCAN
from app.domain import Finding, PolicyListItem, PolicyRecord


PAGE_LINK_CHECK_VERSION = 2


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
            # Lineage-preserving rescan: never delete finding_reviews / review_decisions.
            # Old current findings are marked superseded; human conclusions inherit by stable_key.
            import json as _json
            from app.findings_model import legacy_review_status, map_legacy_status_to_machine
            from app.finding_counts import compute_counts_from_rows

            prior_rows = [
                dict(row)
                for row in conn.execute(
                    """SELECT * FROM rule_findings
                    WHERE job_id=? AND document_id=? AND COALESCE(is_current, 1)=1""",
                    (job_id, document_id),
                ).fetchall()
            ]
            prior_by_rule = {row["rule_code"]: row for row in prior_rows}
            # Also inherit from any historical current-or-not human decisions on same stable key
            inherited_human: dict[str, dict] = {}
            for row in conn.execute(
                """SELECT rule_code, human_status, review_status, rule_version, id
                FROM rule_findings
                WHERE job_id=? AND document_id=? AND human_status IS NOT NULL
                  AND human_status != '' AND human_status != 'unreviewed'
                ORDER BY id DESC""",
                (job_id, document_id),
            ).fetchall():
                code = row["rule_code"]
                if code not in inherited_human:
                    inherited_human[code] = dict(row)

            new_ids_by_rule: dict[str, int] = {}
            for f in findings:
                machine = getattr(f, "machine_status", None) or f.status
                if machine in {"pending", "confirmed", "dismissed"}:
                    machine = map_legacy_status_to_machine(machine, f.rule_code)
                human = getattr(f, "human_status", None) or "unreviewed"
                if f.rule_code in inherited_human:
                    prev = inherited_human[f.rule_code]
                    if prev.get("human_status") and prev["human_status"] != "unreviewed":
                        human = prev["human_status"]
                    elif prev.get("review_status") in {"confirmed", "dismissed"}:
                        human = (
                            "human_confirmed"
                            if prev["review_status"] == "confirmed"
                            else "human_dismissed"
                        )
                elif f.rule_code in prior_by_rule:
                    prev = prior_by_rule[f.rule_code]
                    if prev.get("human_status") and prev["human_status"] != "unreviewed":
                        human = prev["human_status"]
                    elif prev.get("review_status") in {"confirmed", "dismissed"}:
                        human = (
                            "human_confirmed"
                            if prev["review_status"] == "confirmed"
                            else "human_dismissed"
                        )
                allow = 1 if getattr(f, "allow_customer_output", False) else 0
                if f.rule_code.startswith("AGENCY-") or f.rule_code.startswith("ORG-"):
                    allow = 0
                elif f.rule_code in {"META-001", "DOC-001", "DOC-002", "DATE-001", "DATE-002"}:
                    allow = 1
                    if machine in {"pending", "confirmed"} or machine == "confirmed_by_rule":
                        machine = (
                            "confirmed_by_rule"
                            if f.status in {"confirmed", "confirmed_by_rule"}
                            or machine == "confirmed_by_rule"
                            else machine
                        )
                version = getattr(f, "rule_version", None) or "1.0.0"
                confidence = float(getattr(f, "confidence", 0) or 0)
                if human == "unreviewed" and f.status in {"pending", "confirmed", "dismissed"}:
                    review_status = f.status
                else:
                    review_status = legacy_review_status(machine, human)
                stable_key = f"{document_id}:{f.rule_code}"
                lineage_key = stable_key
                cur = conn.execute(
                    """INSERT INTO rule_findings(
                        job_id,document_id,rule_code,category,severity,review_status,detail,
                        page_value,body_value,evidence,created_at,
                        machine_status,human_status,rule_version,confidence,allow_customer_output,evidence_json,
                        is_current,superseded_by,lineage_key,stable_key,superseded_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,NULL)""",
                    (
                        job_id,
                        document_id,
                        f.rule_code,
                        f.category,
                        f.severity,
                        review_status,
                        f.detail,
                        f.page_value,
                        f.body_value,
                        f.evidence,
                        now,
                        machine,
                        human,
                        version,
                        confidence,
                        allow,
                        _json.dumps(getattr(f, "evidence_fields", None) or [], ensure_ascii=False),
                        1,
                        lineage_key,
                        stable_key,
                    ),
                )
                finding_id = int(cur.lastrowid)
                new_ids_by_rule[f.rule_code] = finding_id
                fields = list(getattr(f, "evidence_fields", None) or [])
                if not fields:
                    for field_name, raw, source in (
                        ("page_value", f.page_value, "page"),
                        ("body_value", f.body_value, "body"),
                        ("evidence", f.evidence, "mixed"),
                        ("detail", f.detail, "system"),
                    ):
                        if raw:
                            fields.append(
                                {
                                    "field_name": field_name,
                                    "raw_value": raw,
                                    "source_type": source,
                                    "excerpt": str(raw)[:500],
                                }
                            )
                for item in fields:
                    conn.execute(
                        """INSERT INTO finding_evidence(
                            finding_id, evidence_type, source_type, source_url, field_name,
                            raw_value, normalized_value, excerpt, confidence, created_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (
                            finding_id,
                            item.get("evidence_type", "field"),
                            item.get("source_type", "page"),
                            item.get("source_url", record.url),
                            item.get("field_name", ""),
                            item.get("raw_value", ""),
                            item.get("normalized_value", item.get("raw_value", "")),
                            item.get("excerpt", str(item.get("raw_value", ""))[:500]),
                            float(item.get("confidence", confidence) or confidence),
                            now,
                        ),
                    )

            # Supersede previous current findings for this job/document (do not delete reviews).
            for old in prior_rows:
                new_id = new_ids_by_rule.get(old["rule_code"])
                conn.execute(
                    """UPDATE rule_findings
                    SET is_current=0, superseded_by=?, superseded_at=?
                    WHERE id=?""",
                    (new_id, now, old["id"]),
                )

            # Point review_decisions / keep finding_reviews on old ids (immutable).
            # Optionally mirror latest decision onto new finding for API convenience without deleting history.
            for rule_code, new_id in new_ids_by_rule.items():
                old = prior_by_rule.get(rule_code) or inherited_human.get(rule_code)
                if not old:
                    continue
                old_id = old.get("id")
                if not old_id:
                    continue
                decision = conn.execute(
                    "SELECT * FROM review_decisions WHERE finding_id=?",
                    (old_id,),
                ).fetchone()
                if decision:
                    conn.execute(
                        """INSERT INTO review_decisions(finding_id, decision, note, reviewed_at)
                        VALUES(?,?,?,?)
                        ON CONFLICT(finding_id) DO UPDATE SET
                          decision=excluded.decision,
                          note=excluded.note,
                          reviewed_at=excluded.reviewed_at""",
                        (new_id, decision["decision"], decision["note"], decision["reviewed_at"]),
                    )
                # Do NOT copy finding_reviews rows; they remain attached to original finding_id.
                # Link via lineage: consumers join by stable_key / superseded_by chain.

            # Refresh denormalized job counters for this job (current findings only).
            current_rows = conn.execute(
                "SELECT * FROM rule_findings WHERE job_id=? AND COALESCE(is_current,1)=1",
                (job_id,),
            ).fetchall()
            counts = compute_counts_from_rows([dict(r) for r in current_rows])
            # Only update count columns if they exist (closeout migration).
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(scan_jobs)")}
            if "formal_finding_count" in cols:
                conn.execute(
                    """UPDATE scan_jobs SET
                        finding_count=?,
                        finding_total_count=?,
                        formal_finding_count=?,
                        suspected_count=?,
                        hint_count=?,
                        insufficient_data_count=?,
                        collection_error_count=?,
                        auto_excluded_count=?,
                        human_dismissed_count=?,
                        human_confirmed_count=?
                    WHERE id=?""",
                    (
                        counts["finding_count"],
                        counts["finding_total_count"],
                        counts["formal_finding_count"],
                        counts["suspected_count"],
                        counts["hint_count"],
                        counts["insufficient_data_count"],
                        counts["collection_error_count"],
                        counts["auto_excluded_count"],
                        counts["human_dismissed_count"],
                        counts["human_confirmed_count"],
                        job_id,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE scan_jobs SET finding_count=? WHERE id=?",
                    (counts["finding_count"], job_id),
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
        """记录列表条目详情结果。

        快照条目有稳定身份时按 URL 幂等更新；位置已被其他稳定条目占用时改用负数
        item_index 保留审计行。旧的非快照路径仍按页码位置 upsert。
        """
        listed = item.published_date.isoformat() if item.published_date else None
        authored = authored_date.isoformat() if authored_date else None
        published = published_date.isoformat() if published_date else None
        missing_json = json.dumps(missing_fields or [], ensure_ascii=False)
        now = utc_now()
        with self.db.connect() as conn:
            if getattr(item, "stable_id", None):
                existing = conn.execute(
                    """SELECT id FROM scan_item_results
                    WHERE job_id=? AND target_key=? AND url=?
                    ORDER BY checked_at DESC LIMIT 1""",
                    (job_id, item.source_key, item.url),
                ).fetchone()
                existing_id = int(existing["id"]) if existing else None
                item_index = self._conflict_safe_item_index(
                    conn,
                    job_id=job_id,
                    target_key=item.source_key,
                    page_number=item.page_number,
                    item_index=item.item_index,
                    existing_result_id=existing_id,
                )
                if existing_id is not None:
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
                            item.source_site, item.source_channel_id, existing_id,
                        ),
                    )
                    return
                conn.execute(
                    """INSERT INTO scan_item_results(job_id,target_key,source_label,channel_id,page_number,item_index,title,url,
                    listed_date,detail_status,header_detected,source_id,topic_category,disclosure_attribute,authored_date,
                    page_document_number,published_date,issuing_agency,missing_fields_json,document_id,reused_document_id,
                    baseline_job_id,link_check_version,reason,checked_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        job_id, item.source_key, item.source_site, item.source_channel_id, item.page_number, item_index,
                        item.title, item.url, listed, detail_status,
                        int(header_detected), source_id, topic_category, disclosure_attribute,
                        authored, page_document_number, published, issuing_agency,
                        missing_json, document_id, reused_document_id,
                        baseline_job_id, link_check_version, reason, now,
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

    def _conflict_safe_item_index(
        self,
        conn,
        *,
        job_id: int,
        target_key: str,
        page_number: int,
        item_index: int,
        existing_result_id: int | None = None,
    ) -> int:
        position_conflict = conn.execute(
            """SELECT id FROM scan_item_results
            WHERE job_id=? AND target_key=? AND page_number=? AND item_index=?
              AND (? IS NULL OR id!=?)
            LIMIT 1""",
            (job_id, target_key, page_number, item_index, existing_result_id, existing_result_id),
        ).fetchone()
        if not position_conflict:
            return item_index
        if existing_result_id is not None:
            existing = conn.execute(
                "SELECT item_index FROM scan_item_results WHERE id=?", (existing_result_id,),
            ).fetchone()
            if existing and int(existing["item_index"]) < 0:
                existing_index = int(existing["item_index"])
                negative_conflict = conn.execute(
                    """SELECT id FROM scan_item_results
                    WHERE job_id=? AND target_key=? AND page_number=? AND item_index=? AND id!=?
                    LIMIT 1""",
                    (job_id, target_key, page_number, existing_index, existing_result_id),
                ).fetchone()
                if not negative_conflict:
                    return existing_index
        row = conn.execute(
            """SELECT MIN(item_index) AS min_index FROM scan_item_results
            WHERE job_id=? AND target_key=? AND page_number=? AND item_index<0""",
            (job_id, target_key, page_number),
        ).fetchone()
        min_index = int(row["min_index"]) if row and row["min_index"] is not None else 0
        return min_index - 1 if min_index < 0 else -1

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
                ) AS has_legacy_hidden_links,
                EXISTS(
                    SELECT 1 FROM rule_findings f
                    WHERE f.job_id=s.job_id
                      AND f.document_id=COALESCE(s.document_id,s.reused_document_id)
                      AND COALESCE(f.is_current, 1)=1
                      AND COALESCE(f.human_status, '') <> 'human_dismissed'
                ) AS has_open_findings,
                EXISTS(
                    SELECT 1 FROM link_checks l
                    WHERE l.document_id=COALESCE(s.document_id,s.reused_document_id)
                      AND l.link_kind='附件'
                ) AS has_attachment_checks,
                EXISTS(
                    SELECT 1 FROM policy_documents d
                    WHERE d.id=COALESCE(s.document_id,s.reused_document_id)
                      AND d.body_text LIKE '%附件%'
                ) AS declares_attachment,
                EXISTS(
                    SELECT 1 FROM link_checks l
                    WHERE l.document_id=COALESCE(s.document_id,s.reused_document_id)
                      AND l.result='broken'
                      AND l.status_code IS NULL
                      AND COALESCE(l.error_type,'')='click_no_response'
                ) AS has_unevidenced_broken_links
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
        # 列表外复验行用 page_number=0/item_index<0 作哨兵，不属于列表项，
        # 不能拿来和 generation 的列表条目数做覆盖校验。
        with self.db.connect() as conn:
            return int(conn.execute(
                "SELECT COUNT(*) FROM scan_item_results WHERE job_id=?"
                " AND NOT (page_number=0 AND item_index<0)", (job_id,)
            ).fetchone()[0])

    def copy_baseline_findings(self, baseline_job_id: int, job_id: int, document_id: int) -> list[Finding]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM rule_findings WHERE job_id=? AND document_id=?""",
                (baseline_job_id, document_id),
            ).fetchall()
            now = utc_now()
            findings: list[Finding] = []
            for row in rows:
                keys = set(row.keys())
                machine = row["machine_status"] if "machine_status" in keys and row["machine_status"] else row["review_status"]
                human = row["human_status"] if "human_status" in keys and row["human_status"] else "unreviewed"
                version = row["rule_version"] if "rule_version" in keys and row["rule_version"] else "1.0.0"
                confidence = float(row["confidence"]) if "confidence" in keys and row["confidence"] is not None else 0.0
                allow = int(row["allow_customer_output"]) if "allow_customer_output" in keys else 0
                evidence_json = row["evidence_json"] if "evidence_json" in keys and row["evidence_json"] else "[]"
                conn.execute(
                    """INSERT INTO rule_findings(
                        job_id,document_id,rule_code,category,severity,review_status,detail,
                        page_value,body_value,evidence,created_at,
                        machine_status,human_status,rule_version,confidence,allow_customer_output,evidence_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        job_id, document_id, row["rule_code"], row["category"], row["severity"],
                        row["review_status"], row["detail"], row["page_value"], row["body_value"],
                        row["evidence"], now, machine, human, version, confidence, allow, evidence_json,
                    ),
                )
                findings.append(
                    Finding(
                        row["rule_code"], row["category"], row["severity"], row["review_status"], row["detail"],
                        row["page_value"], row["body_value"], row["evidence"],
                        machine_status=machine, human_status=human, rule_version=version,
                        confidence=confidence, allow_customer_output=bool(allow),
                    )
                )
            return findings

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

    def vanished_open_findings(self, job_id: int, target, limit: int = 400) -> list[dict]:
        """本轮没在列表里出现、但库里仍挂着未关闭问题的文档。

        增量只按当期列表建队列，这类文档永远不会被重开：既确认不了问题是否还在，
        也销不掉号。候选只从「未关闭问题」这一小撮行出发，并用 (job_id,target_key,url)
        索引判断本轮有没有复验过——按 url 全表比对会在两万多条的来源上先把扫描拖慢。
        """
        with self.db.connect() as conn:
            rows = conn.execute(
                f"""SELECT d.id AS document_id, d.url AS url, d.title AS title,
                          (SELECT i.stable_id FROM source_inventory i
                           WHERE i.target_key=? AND i.url=d.url LIMIT 1) AS stable_id,
                          COUNT(DISTINCT f.rule_code) AS open_rules
                   FROM rule_findings f
                   JOIN policy_documents d ON d.id=f.document_id
                   WHERE f.is_current=1 AND f.allow_customer_output=1 AND f.job_id<>?
                     AND d.source_site=? AND {RECTIFIED_BY_LATER_SCAN}
                     AND NOT EXISTS (SELECT 1 FROM scan_item_results n
                                     WHERE n.job_id=? AND n.target_key=? AND n.url=d.url)
                   GROUP BY d.id
                   ORDER BY open_rules DESC, d.url LIMIT ?""",
                (target.key, job_id, target.label, job_id, target.key, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def close_open_findings_as_vanished(self, job_id: int, document_id: int, note: str) -> int:
        """详情页已不再展示该文件时，把这文档的现存问题记为销号并移出问题库。

        判定依据来自本轮真开过的页面，不是「本轮没看到」——所以只在复验到空壳/下线时调用。
        """
        now = utc_now()
        with self.db.connect() as conn:
            rows = [dict(r) for r in conn.execute(
                """SELECT f.*,d.title,d.url,d.district,d.source_site FROM rule_findings f
                   JOIN policy_documents d ON d.id=f.document_id
                   WHERE f.document_id=? AND COALESCE(f.is_current,1)=1""", (document_id,)
            ).fetchall()]
            for old in rows:
                conn.execute(
                    """INSERT INTO finding_transitions(
                        job_id,baseline_job_id,document_id,stable_key,rule_code,category,severity,
                        kind,detail,before_value,after_value,source_site,district,title,document_url,recorded_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(job_id,stable_key,kind) DO NOTHING""",
                    (
                        job_id, old["job_id"], document_id,
                        old["stable_key"] or f"{document_id}:{old['rule_code']}", old["rule_code"],
                        old["category"], old["severity"], "closed",
                        f"{old['detail'] or ''}｜{note}", str(old["page_value"] or ""), "",
                        old["source_site"] or "", old["district"] or "", old["title"] or "", old["url"] or "", now,
                    ),
                )
                conn.execute(
                    "UPDATE rule_findings SET is_current=0, superseded_at=? WHERE id=?", (now, int(old["id"])),
                )
        return len(rows)

    def document(self, document_id: int) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM policy_documents WHERE id=?", (document_id,)).fetchone()
        return dict(row) if row else None

    def reconcile_rectifications(
        self, job_id: int, document_id: int,
    ) -> dict[str, int]:
        """增量复查后与历史对齐：本轮不再命中的记「销号」，人工驳回却又命中的记「冲突」。

        「历史」是这一篇文档在**所有更早任务**里仍 current 的问题，每个规则取最新一条——
        只比指定的那一个基线任务会漏掉更早轮次留下的行（实测普陀 49 条 META-001 来自
        job 4，基线是 job 9，于是永远没人跟它比，job 9 早就核验过字段齐全也没用）。
        只在文档确实被重新打开并成功评估之后调用——否则「本轮没有这行」只代表没看，
        不代表网站改好了。人工驳回过的仍按驳回结论不报，这里只留一条提醒。
        """
        with self.db.connect() as conn:
            doc = conn.execute(
                "SELECT district,source_site,title,url FROM policy_documents WHERE id=?", (document_id,),
            ).fetchone()
            baseline = {
                row["rule_code"]: dict(row)
                for row in conn.execute(
                    """SELECT f.* FROM rule_findings f
                    WHERE f.document_id=? AND COALESCE(f.is_current, 1)=1 AND f.job_id<?
                      AND f.id=(SELECT f2.id FROM rule_findings f2
                                WHERE f2.document_id=f.document_id AND f2.rule_code=f.rule_code
                                  AND COALESCE(f2.is_current, 1)=1 AND f2.job_id<?
                                ORDER BY COALESCE(f2.created_at, '') DESC, f2.id DESC LIMIT 1)""",
                    (document_id, job_id, job_id),
                ).fetchall()
            }
            current = {
                row["rule_code"]: dict(row)
                for row in conn.execute(
                    """SELECT * FROM rule_findings
                    WHERE job_id=? AND document_id=? AND COALESCE(is_current, 1)=1""",
                    (job_id, document_id),
                ).fetchall()
            }
        now = utc_now()
        counts = {"closed": 0, "reconfirmed_after_dismissal": 0}
        with self.db.connect() as conn:
            for rule_code, old in baseline.items():
                stable_key = old["stable_key"] or f"{document_id}:{rule_code}"
                new = current.get(rule_code)
                if new is None:
                    kind = "closed"
                    after_value = ""
                elif old["human_status"] == "human_dismissed" or new["human_status"] == "human_dismissed":
                    kind = "reconfirmed_after_dismissal"
                    after_value = str(new["page_value"] or "")
                else:
                    continue
                conn.execute(
                    """INSERT INTO finding_transitions(
                        job_id,baseline_job_id,document_id,stable_key,rule_code,category,severity,
                        kind,detail,before_value,after_value,source_site,district,title,document_url,recorded_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(job_id,stable_key,kind) DO NOTHING""",
                    (
                        job_id, old["job_id"], document_id, stable_key, rule_code,
                        old["category"], old["severity"], kind, old["detail"],
                        str(old["page_value"] or ""), after_value,
                        dict(doc)["source_site"] if doc else "",
                        dict(doc)["district"] if doc else "",
                        dict(doc)["title"] if doc else "",
                        dict(doc)["url"] if doc else "", now,
                    ),
                )
                counts[kind] += 1
        return counts

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

    def open_generation_item_count(self, job_id: int, target_key: str | None = None) -> int:
        with self.db.connect() as conn:
            if target_key:
                row = conn.execute(
                    """SELECT COUNT(*) FROM generation_items
                    WHERE job_id=? AND target_key=? AND status IN ('pending','checking','retry')""",
                    (job_id, target_key),
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT COUNT(*) FROM generation_items
                    WHERE job_id=? AND status IN ('pending','checking','retry')""",
                    (job_id,),
                ).fetchone()
        return int(row[0])

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
                return self._converge_schedule_intervals(
                    conn, dict(row), incremental_hours, reconcile_days,
                )
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

    def _converge_schedule_intervals(
        self, conn: Any, row: dict, incremental_hours: float, reconcile_days: float,
    ) -> dict:
        """调用方传的周期（来自环境变量）是唯一事实来源，改了就把表里的值和下一轮时间一起收敛。

        调度行创建后只会被调度器原样读出，若这里不收敛，改完 CONTINUOUS_SCAN_* 后
        表里仍按旧节奏判到期：既可能提前跑，也可能一直不跑。
        """
        from datetime import datetime, timedelta, timezone

        same = (
            float(row.get("incremental_interval_hours") or 0) == float(incremental_hours)
            and float(row.get("full_reconcile_interval_days") or 0) == float(reconcile_days)
        )
        if same:
            return row
        now = datetime.now(timezone.utc)
        next_inc = self._next_after(row.get("last_incremental_at"), incremental_hours, now)
        next_full = self._next_after(row.get("last_full_reconcile_at"), reconcile_days * 24, now)
        conn.execute(
            """UPDATE continuous_schedules SET incremental_interval_hours=?, full_reconcile_interval_days=?,
            next_incremental_at=?, next_full_reconcile_at=?, updated_at=? WHERE site_key=?""",
            (incremental_hours, reconcile_days, next_inc, next_full, utc_now(), row["site_key"]),
        )
        return dict(conn.execute(
            "SELECT * FROM continuous_schedules WHERE site_key=?", (row["site_key"],),
        ).fetchone())

    @staticmethod
    def _next_after(last_at: str | None, interval_hours: float, now: Any) -> str:
        from datetime import datetime, timedelta, timezone

        base = now
        if last_at:
            try:
                base = datetime.fromisoformat(str(last_at))
                if base.tzinfo is None:
                    base = base.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                base = now
        # 缩短周期时允许下一轮立刻到期，但不把时间戳留在过去。
        return max(base + timedelta(hours=float(interval_hours)), now).isoformat()

    def get_schedule(self, site_key: str) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM continuous_schedules WHERE site_key=?", (site_key,),
            ).fetchone()
        return dict(row) if row else None

    def update_schedule(self, site_key: str, **values) -> None:
        allowed = {
            "enabled", "last_incremental_at", "last_full_reconcile_at", "next_incremental_at",
            "next_full_reconcile_at", "last_job_id", "last_status", "updated_at",
            "incremental_interval_hours", "full_reconcile_interval_days", "source_signature",
            "consecutive_failures",
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
