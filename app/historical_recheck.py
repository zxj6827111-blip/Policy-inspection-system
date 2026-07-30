from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from chinese_calendar import is_workday

from app.domain import Finding, PolicyRecord
from app.knowledge.organizations import load_knowledge
from app.finding_counts import compute_counts_from_rows
from app.findings_model import legacy_review_status
from app.rules import WorkdayCalendar, evaluate_record


COMPARISON_FIELDS = [
    "job_id",
    "document_id",
    "district",
    "title",
    "url",
    "rule_code",
    "old_finding_id",
    "old_machine_status",
    "new_machine_status",
    "old_allow_customer_output",
    "new_allow_customer_output",
    "old_page_value",
    "new_page_value",
    "old_body_value",
    "new_body_value",
    "old_detail",
    "new_detail",
]


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _json_list(value: Any) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _database_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _document_rows(conn: sqlite3.Connection, job_ids: list[int]) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in job_ids)
    sql = f"""
        WITH membership AS (
            SELECT DISTINCT job_id, document_id
            FROM scan_job_documents
            WHERE job_id IN ({placeholders})
        ), ranked_results AS (
            SELECT s.*,
                   ROW_NUMBER() OVER (PARTITION BY s.job_id, s.document_id ORDER BY s.id DESC) AS rn
            FROM scan_item_results s
            WHERE s.job_id IN ({placeholders}) AND s.document_id IS NOT NULL
        ), latest_results AS (
            SELECT * FROM ranked_results WHERE rn=1
        )
        SELECT m.job_id, p.*,
               COALESCE(s.header_detected, 0) AS scan_header_detected,
               COALESCE(s.missing_fields_json, '[]') AS scan_missing_fields_json
        FROM membership m
        JOIN policy_documents p ON p.id=m.document_id
        LEFT JOIN latest_results s ON s.job_id=m.job_id AND s.document_id=m.document_id
        ORDER BY m.job_id, p.id
    """
    return conn.execute(sql, [*job_ids, *job_ids]).fetchall()


def _old_finding_rows(conn: sqlite3.Connection, job_ids: list[int]) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in job_ids)
    rows = conn.execute(
        f"""SELECT f.*, d.district, d.title, d.url
        FROM rule_findings f
        JOIN policy_documents d ON d.id=f.document_id
        WHERE f.job_id IN ({placeholders}) AND COALESCE(f.is_current,1)=1
        ORDER BY f.job_id,f.document_id,f.rule_code,f.id""",
        job_ids,
    ).fetchall()
    return [dict(row) for row in rows]


def _policy_record(row: sqlite3.Row) -> PolicyRecord:
    return PolicyRecord(
        district=row["district"],
        title=row["title"],
        url=row["url"],
        source_id=row["source_id"] or "",
        issuing_agency=row["issuing_agency"] or "",
        page_document_number=row["page_document_number"] or "",
        published_date=_parse_date(row["published_date"]),
        authored_date=_parse_date(row["authored_date"]),
        body_text=row["body_text"] or "",
        source_site=row["source_site"] or "",
        topic_category=row["topic_category"] or "",
        disclosure_attribute=row["disclosure_attribute"] or "",
        header_detected=bool(row["scan_header_detected"]),
        missing_metadata_fields=_json_list(row["scan_missing_fields_json"]),
    )


def _new_finding_row(row: sqlite3.Row, finding: Finding) -> dict[str, Any]:
    return {
        "job_id": int(row["job_id"]),
        "document_id": int(row["id"]),
        "district": row["district"],
        "title": row["title"],
        "url": row["url"],
        "rule_code": finding.rule_code,
        "category": finding.category,
        "severity": finding.severity,
        "machine_status": finding.machine_status or finding.status,
        "human_status": finding.human_status or "unreviewed",
        "allow_customer_output": 1 if finding.allow_customer_output else 0,
        "detail": finding.detail,
        "page_value": finding.page_value,
        "body_value": finding.body_value,
        "evidence": finding.evidence,
        "rule_version": finding.rule_version,
        "confidence": finding.confidence,
        "evidence_fields": finding.evidence_fields or [],
    }


def _key(row: dict[str, Any]) -> tuple[int, int, str]:
    return int(row["job_id"]), int(row["document_id"]), str(row["rule_code"])


def _comparison_row(
    old: dict[str, Any] | None,
    new: dict[str, Any] | None,
) -> dict[str, Any]:
    source = new or old or {}
    return {
        "job_id": source.get("job_id", ""),
        "document_id": source.get("document_id", ""),
        "district": source.get("district", ""),
        "title": source.get("title", ""),
        "url": source.get("url", ""),
        "rule_code": source.get("rule_code", ""),
        "old_finding_id": (old or {}).get("id", ""),
        "old_machine_status": (old or {}).get("machine_status", ""),
        "new_machine_status": (new or {}).get("machine_status", ""),
        "old_allow_customer_output": (old or {}).get("allow_customer_output", ""),
        "new_allow_customer_output": (new or {}).get("allow_customer_output", ""),
        "old_page_value": (old or {}).get("page_value", ""),
        "new_page_value": (new or {}).get("page_value", ""),
        "old_body_value": (old or {}).get("body_value", ""),
        "new_body_value": (new or {}).get("body_value", ""),
        "old_detail": (old or {}).get("detail", ""),
        "new_detail": (new or {}).get("detail", ""),
    }


def _rule_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return dict(sorted(Counter(str(row["rule_code"]) for row in rows).items()))


def recheck_jobs(database: Path, job_ids: list[int]) -> dict[str, Any]:
    """Re-evaluate stored M1 inputs without changing the database."""
    database = database.resolve()
    normalized_ids = sorted({int(job_id) for job_id in job_ids})
    if not normalized_ids:
        raise ValueError("At least one job id is required")
    if not database.is_file():
        raise FileNotFoundError(database)

    conn = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in normalized_ids)
        existing = {
            int(row["id"])
            for row in conn.execute(
                f"SELECT id FROM scan_jobs WHERE id IN ({placeholders})", normalized_ids
            ).fetchall()
        }
        missing = sorted(set(normalized_ids) - existing)
        if missing:
            raise ValueError(f"Unknown job ids: {missing}")

        documents = _document_rows(conn, normalized_ids)
        old_rows = _old_finding_rows(conn, normalized_ids)
        calendar = WorkdayCalendar(provider=is_workday)
        knowledge_cache: dict[str, list[Any]] = {}
        agency_cache: dict[str, list[dict[str, Any]]] = {}
        new_rows: list[dict[str, Any]] = []

        for row in documents:
            district = str(row["district"])
            if district not in knowledge_cache:
                knowledge_cache[district] = load_knowledge(conn, district)
                agency_cache[district] = [
                    dict(item)
                    for item in conn.execute(
                        "SELECT * FROM agency_rules WHERE district=?", (district,)
                    ).fetchall()
                ]
            findings = evaluate_record(
                _policy_record(row),
                agency_cache[district],
                calendar,
                knowledge=knowledge_cache[district],
            )
            new_rows.extend(_new_finding_row(row, finding) for finding in findings)

        old_by_key = {_key(row): row for row in old_rows}
        new_by_key = {_key(row): row for row in new_rows}
        old_keys = set(old_by_key)
        new_keys = set(new_by_key)
        old_customer_keys = {
            key for key, row in old_by_key.items()
            if int(row.get("allow_customer_output") or 0)
        }
        new_customer_keys = {
            key for key, row in new_by_key.items()
            if int(row.get("allow_customer_output") or 0)
        }

        kept = [
            _comparison_row(old_by_key[key], new_by_key[key])
            for key in sorted(old_keys & new_keys)
        ]
        added = [
            _comparison_row(None, new_by_key[key])
            for key in sorted(new_keys - old_keys)
        ]
        removed = [
            _comparison_row(old_by_key[key], None)
            for key in sorted(old_keys - new_keys)
        ]
        changed = [
            row
            for row in kept
            if (
                row["old_machine_status"],
                row["old_allow_customer_output"],
                row["old_detail"],
            ) != (
                row["new_machine_status"],
                row["new_allow_customer_output"],
                row["new_detail"],
            )
        ]

        jobs: dict[str, Any] = {}
        for job_id in normalized_ids:
            old_job = [row for row in old_rows if int(row["job_id"]) == job_id]
            new_job = [row for row in new_rows if int(row["job_id"]) == job_id]
            jobs[str(job_id)] = {
                "documents_evaluated": sum(int(row["job_id"]) == job_id for row in documents),
                "old_total": len(old_job),
                "new_total": len(new_job),
                "old_customer_candidates": sum(int(row.get("allow_customer_output") or 0) for row in old_job),
                "new_customer_candidates": sum(int(row.get("allow_customer_output") or 0) for row in new_job),
                "kept_customer_candidates": sum(
                    key[0] == job_id for key in old_customer_keys & new_customer_keys
                ),
                "added_customer_candidates": sum(
                    key[0] == job_id for key in new_customer_keys - old_customer_keys
                ),
                "removed_customer_candidates": sum(
                    key[0] == job_id for key in old_customer_keys - new_customer_keys
                ),
                "kept": sum(int(row["job_id"]) == job_id for row in kept),
                "added": sum(int(row["job_id"]) == job_id for row in added),
                "removed": sum(int(row["job_id"]) == job_id for row in removed),
                "changed": sum(int(row["job_id"]) == job_id for row in changed),
                "old_by_rule": _rule_counts(old_job),
                "new_by_rule": _rule_counts(new_job),
            }

        summary = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "database": str(database),
            "database_sha256": _database_sha256(database),
            "read_only": True,
            "job_ids": normalized_ids,
            "totals": {
                "documents_evaluated": len(documents),
                "old_total": len(old_rows),
                "new_total": len(new_rows),
                "old_customer_candidates": sum(int(row.get("allow_customer_output") or 0) for row in old_rows),
                "new_customer_candidates": sum(int(row.get("allow_customer_output") or 0) for row in new_rows),
                "kept_customer_candidates": len(old_customer_keys & new_customer_keys),
                "added_customer_candidates": len(new_customer_keys - old_customer_keys),
                "removed_customer_candidates": len(old_customer_keys - new_customer_keys),
                "kept": len(kept),
                "added": len(added),
                "removed": len(removed),
                "changed": len(changed),
            },
            "jobs": jobs,
        }
        return {
            "summary": summary,
            "new_rows": new_rows,
            "kept": kept,
            "added": added,
            "removed": removed,
            "changed": changed,
        }
    finally:
        conn.close()


def apply_recheck_result(
    database: Path,
    job_ids: list[int],
    backup_path: Path,
) -> dict[str, Any]:
    """Atomically publish a historical recheck while preserving finding lineage."""
    database = database.resolve()
    backup_path = backup_path.resolve()
    if backup_path.exists():
        raise FileExistsError(backup_path)
    backup_path.parent.mkdir(parents=True, exist_ok=True)

    result = recheck_jobs(database, job_ids)
    normalized_ids = result["summary"]["job_ids"]

    # sqlite backup stays consistent even while the local web process has the DB open.
    with sqlite3.connect(database) as source, sqlite3.connect(backup_path) as backup:
        source.backup(backup)
        integrity = backup.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"Backup integrity check failed: {integrity}")

    placeholders = ",".join("?" for _ in normalized_ids)
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        current_rows = [
            dict(row)
            for row in conn.execute(
                f"""SELECT * FROM rule_findings
                WHERE job_id IN ({placeholders}) AND COALESCE(is_current,1)=1""",
                normalized_ids,
            ).fetchall()
        ]
        if len(current_rows) != result["summary"]["totals"]["old_total"]:
            raise RuntimeError("Current findings changed after recheck; refusing to apply stale result")

        current_by_key = {_key(row): row for row in current_rows}
        inherited_human: dict[tuple[int, int, str], dict[str, Any]] = {}
        for row in conn.execute(
            f"""SELECT * FROM rule_findings
            WHERE job_id IN ({placeholders})
              AND human_status NOT IN ('', 'unreviewed')
            ORDER BY id DESC""",
            normalized_ids,
        ).fetchall():
            item = dict(row)
            inherited_human.setdefault(_key(item), item)
        legacy_decisions: dict[tuple[int, int, str], str] = {}
        for row in conn.execute(
            f"""SELECT f.job_id,f.document_id,f.rule_code,r.decision
            FROM rule_findings f
            JOIN review_decisions r ON r.finding_id=f.id
            WHERE f.job_id IN ({placeholders})
            ORDER BY r.id DESC""",
            normalized_ids,
        ).fetchall():
            item = dict(row)
            legacy_decisions.setdefault(_key(item), str(item["decision"]))

        new_ids: dict[tuple[int, int, str], int] = {}
        for item in result["new_rows"]:
            key = _key(item)
            previous = inherited_human.get(key) or current_by_key.get(key)
            human = item.get("human_status") or "unreviewed"
            if previous:
                previous_human = previous.get("human_status") or "unreviewed"
                if previous_human != "unreviewed":
                    human = previous_human
            decision = legacy_decisions.get(key)
            if human == "unreviewed" and decision in {"confirmed", "human_confirmed"}:
                human = "human_confirmed"
            elif human == "unreviewed" and decision in {"dismissed", "human_dismissed"}:
                human = "human_dismissed"
            review_status = legacy_review_status(item["machine_status"], human)
            stable_key = f"{item['document_id']}:{item['rule_code']}"
            evidence_fields = item.get("evidence_fields") or []
            cursor = conn.execute(
                """INSERT INTO rule_findings(
                    job_id,document_id,rule_code,category,severity,review_status,detail,
                    page_value,body_value,evidence,created_at,machine_status,human_status,
                    rule_version,confidence,allow_customer_output,evidence_json,is_current,
                    superseded_by,lineage_key,stable_key,superseded_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,NULL,?,?,NULL)""",
                (
                    item["job_id"], item["document_id"], item["rule_code"],
                    item["category"], item["severity"], review_status, item["detail"],
                    item["page_value"], item["body_value"], item["evidence"], now,
                    item["machine_status"], human, item["rule_version"], item["confidence"],
                    int(item["allow_customer_output"]),
                    json.dumps(evidence_fields, ensure_ascii=False), stable_key, stable_key,
                ),
            )
            finding_id = int(cursor.lastrowid)
            new_ids[key] = finding_id

            fields = list(evidence_fields)
            if not fields:
                for field_name, raw, source_type in (
                    ("page_value", item["page_value"], "page"),
                    ("body_value", item["body_value"], "body"),
                    ("evidence", item["evidence"], "mixed"),
                    ("detail", item["detail"], "system"),
                ):
                    if raw:
                        fields.append({
                            "field_name": field_name,
                            "raw_value": raw,
                            "source_type": source_type,
                            "excerpt": str(raw)[:500],
                        })
            for field in fields:
                raw_value = str(field.get("raw_value", ""))
                conn.execute(
                    """INSERT INTO finding_evidence(
                        finding_id,evidence_type,source_type,source_url,field_name,
                        raw_value,normalized_value,excerpt,confidence,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        finding_id, field.get("evidence_type", "field"),
                        field.get("source_type", "page"), field.get("source_url", item["url"]),
                        field.get("field_name", ""), raw_value,
                        field.get("normalized_value", raw_value),
                        field.get("excerpt", raw_value[:500]),
                        float(field.get("confidence", item["confidence"]) or item["confidence"]), now,
                    ),
                )

        for key, old in current_by_key.items():
            replacement_id = new_ids.get(key)
            conn.execute(
                """UPDATE rule_findings
                SET is_current=0,superseded_by=?,superseded_at=? WHERE id=?""",
                (replacement_id, now, old["id"]),
            )
            if replacement_id is None:
                continue
            decision = conn.execute(
                "SELECT * FROM review_decisions WHERE finding_id=?", (old["id"],)
            ).fetchone()
            if decision:
                conn.execute(
                    """INSERT INTO review_decisions(finding_id,decision,note,reviewed_at)
                    VALUES(?,?,?,?)""",
                    (replacement_id, decision["decision"], decision["note"], decision["reviewed_at"]),
                )

        applied_counts: dict[str, dict[str, int]] = {}
        for job_id in normalized_ids:
            rows = conn.execute(
                """SELECT * FROM rule_findings
                WHERE job_id=? AND COALESCE(is_current,1)=1""",
                (job_id,),
            ).fetchall()
            counts = compute_counts_from_rows([dict(row) for row in rows])
            applied_counts[str(job_id)] = counts
            conn.execute(
                """UPDATE scan_jobs SET finding_count=?,finding_total_count=?,
                formal_finding_count=?,suspected_count=?,hint_count=?,
                insufficient_data_count=?,collection_error_count=?,auto_excluded_count=?,
                human_dismissed_count=?,human_confirmed_count=? WHERE id=?""",
                (
                    counts["finding_count"], counts["finding_total_count"],
                    counts["formal_finding_count"], counts["suspected_count"],
                    counts["hint_count"], counts["insufficient_data_count"],
                    counts["collection_error_count"], counts["auto_excluded_count"],
                    counts["human_dismissed_count"], counts["human_confirmed_count"], job_id,
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        "backup": str(backup_path),
        "backup_integrity": "ok",
        "job_ids": normalized_ids,
        "comparison": result["summary"]["totals"],
        "applied_counts": applied_counts,
        "applied_at": now,
    }


def write_recheck_report(result: dict[str, Any], output_dir: Path) -> Path:
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "summary.json").write_text(
        json.dumps(result["summary"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    for name in ("kept", "added", "removed", "changed"):
        with (output_dir / f"{name}.csv").open("w", encoding="utf-8-sig", newline="") as target:
            writer = csv.DictWriter(target, fieldnames=COMPARISON_FIELDS)
            writer.writeheader()
            writer.writerows(result[name])
    return output_dir
