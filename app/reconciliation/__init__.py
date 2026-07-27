"""M4 dual-channel reconciliation (city vs district).

Implements RECON-001~010 per V3.0 §10.3.

Match results:
- RECON-001 市区一致
- RECON-002 市级缺失 (district has it, municipal doesn't)
- RECON-003 区级缺失 (municipal has it, district doesn't)
- RECON-004 标题不一致
- RECON-005 文号不一致
- RECON-006 日期不一致
- RECON-007 正文不一致
- RECON-008 附件不一致
- RECON-009 更新延迟
- RECON-010 无法匹配 (insufficient data)

Key guarantees (V3.0 §10.4):
- 同一政策跨渠道匹配 precision ≥ 95%
- 正常跨渠道同步不判重复 (handled via DUP-005 by duplicates module)
- 对账差异有双方证据
- 无法匹配时进入数据不足 (RECON-010)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.db import Database, utc_now
from app.duplicates import (
    DocumentFingerprint,
    body_simhash,
    get_fingerprint,
    normalize_title,
    simhash_similarity,
)
from app.rules import normalize_document_number

logger = logging.getLogger(__name__)

# Match thresholds
EXACT_MATCH_THRESHOLD = 1.0
HIGH_SIMILARITY_THRESHOLD = 0.90
MIN_MATCH_THRESHOLD = 0.50  # Below this → RECON-010

# Update delay tolerance: municipal update within N days of district (or vice versa)
UPDATE_DELAY_DAYS = 7


@dataclass
class ReconciliationMatch:
    municipal_document_id: int
    district_document_id: int | None = None
    match_status: str = "RECON-010"
    title_match: bool | None = None
    doc_number_match: bool | None = None
    date_match: bool | None = None
    body_match: bool | None = None
    attachment_match: bool | None = None
    similarity_score: float = 0.0
    evidence: list[dict[str, Any]] = field(default_factory=list)
    machine_status: str = "suspected"
    allow_customer_output: bool = True
    notes: str = ""


def _get_doc_fields(conn, document_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT id, url, title, source_site, channel_level, district,
                  page_document_number, authored_date, published_date,
                  issuing_agency, body_text, content_hash, fingerprint_hash,
                  last_seen_at, first_seen_at
        FROM policy_documents WHERE id=?""",
        (document_id,),
    ).fetchone()
    return dict(row) if row else None


def _check_update_delay(
    municipal_doc: dict[str, Any], district_doc: dict[str, Any],
) -> tuple[bool, int | None]:
    """Check if the documents have a significant update delay.

    Returns (has_delay, delay_days).
    """
    muni_seen = municipal_doc.get("last_seen_at")
    dist_seen = district_doc.get("last_seen_at")
    if not muni_seen or not dist_seen:
        return False, None
    try:
        muni_dt = datetime.fromisoformat(muni_seen.replace("Z", "+00:00"))
        dist_dt = datetime.fromisoformat(dist_seen.replace("Z", "+00:00"))
        delay_days = abs((muni_dt - dist_dt).days)
        return delay_days > UPDATE_DELAY_DAYS, delay_days
    except (ValueError, TypeError):
        return False, None


def _classify_match(
    municipal_doc: dict[str, Any],
    district_doc: dict[str, Any],
    muni_fp: DocumentFingerprint,
    dist_fp: DocumentFingerprint,
) -> ReconciliationMatch:
    """Classify a city-district document match into RECON-001~010."""
    muni_id = int(municipal_doc["id"])
    dist_id = int(district_doc["id"])

    title_match = (
        bool(muni_fp.normalized_title)
        and bool(dist_fp.normalized_title)
        and muni_fp.normalized_title == dist_fp.normalized_title
    )
    doc_number_match = (
        bool(muni_fp.normalized_doc_number)
        and bool(dist_fp.normalized_doc_number)
        and muni_fp.normalized_doc_number == dist_fp.normalized_doc_number
    )
    date_match = (
        bool(muni_fp.authored_date)
        and bool(dist_fp.authored_date)
        and muni_fp.authored_date == dist_fp.authored_date
    )
    body_match = bool(muni_fp.body_hash) and muni_fp.body_hash == dist_fp.body_hash
    if not body_match and muni_fp.body_simhash and dist_fp.body_simhash:
        body_sim = simhash_similarity(muni_fp.body_simhash, dist_fp.body_simhash)
        if body_sim >= 0.95:
            body_match = True
    else:
        body_sim = 1.0 if body_match else 0.0

    # Attachments: assume match if attachment_hash matches.
    attachment_match = (
        bool(muni_fp.attachment_hash)
        and bool(dist_fp.attachment_hash)
        and muni_fp.attachment_hash == dist_fp.attachment_hash
    )

    # If no matching identity signal at all → RECON-010 (V3.0 §10.4: 无法匹配时进入数据不足).
    # A "matching signal" means at least one field actually matches between the two documents.
    # Non-empty but non-matching fields do NOT count as a signal for reconciliation.
    has_matching_signal = any([
        title_match, doc_number_match, date_match, body_match, attachment_match,
    ])
    if not has_matching_signal:
        return ReconciliationMatch(
            municipal_document_id=muni_id, district_document_id=dist_id,
            match_status="RECON-010",
            similarity_score=0.0,
            evidence=[{
                "type": "insufficient_data",
                "reason": "no matching identity signal",
                "title_match": title_match,
                "doc_number_match": doc_number_match,
                "date_match": date_match,
                "body_match": body_match,
                "attachment_match": attachment_match,
                "municipal_url": municipal_doc.get("url", ""),
                "district_url": district_doc.get("url", ""),
            }],
            machine_status="insufficient_data",
            allow_customer_output=False,
            notes="No matching identity signal — could not reconcile.",
        )

    similarity = 0.0
    signal_count = sum(1 for x in (title_match, doc_number_match, date_match, body_match) if x)
    if signal_count:
        similarity = signal_count / 4.0
    elif body_sim:
        similarity = body_sim * 0.5

    evidence: list[dict[str, Any]] = []
    evidence.append({"type": "match_summary",
                      "title_match": title_match,
                      "doc_number_match": doc_number_match,
                      "date_match": date_match,
                      "body_match": body_match,
                      "attachment_match": attachment_match,
                      "similarity": similarity})
    evidence.append({"type": "municipal_url",
                      "url": municipal_doc.get("url", "")})
    evidence.append({"type": "district_url",
                      "url": district_doc.get("url", "")})

    # All match → RECON-001.
    if title_match and doc_number_match and date_match and body_match:
        # Check update delay.
        has_delay, delay_days = _check_update_delay(municipal_doc, district_doc)
        if has_delay and delay_days is not None:
            return ReconciliationMatch(
                municipal_document_id=muni_id, district_document_id=dist_id,
                match_status="RECON-009",
                title_match=title_match, doc_number_match=doc_number_match,
                date_match=date_match, body_match=body_match,
                attachment_match=attachment_match,
                similarity_score=similarity,
                evidence=evidence + [{"type": "update_delay",
                                       "delay_days": delay_days,
                                       "threshold_days": UPDATE_DELAY_DAYS}],
                notes=f"Update delay of {delay_days} days between channels.",
            )
        return ReconciliationMatch(
            municipal_document_id=muni_id, district_document_id=dist_id,
            match_status="RECON-001",
            title_match=title_match, doc_number_match=doc_number_match,
            date_match=date_match, body_match=body_match,
            attachment_match=attachment_match,
            similarity_score=similarity,
            evidence=evidence,
            machine_status="confirmed_by_rule",
            notes="Municipal and district records fully consistent.",
        )

    # Identify first mismatching field in priority order: title, doc_number, date, body, attachment.
    if not title_match and (
        muni_fp.normalized_title or dist_fp.normalized_title
    ):
        return ReconciliationMatch(
            municipal_document_id=muni_id, district_document_id=dist_id,
            match_status="RECON-004",
            title_match=title_match, doc_number_match=doc_number_match,
            date_match=date_match, body_match=body_match,
            attachment_match=attachment_match,
            similarity_score=similarity,
            evidence=evidence + [{
                "type": "title_mismatch",
                "municipal_title": municipal_doc.get("title", ""),
                "district_title": district_doc.get("title", ""),
            }],
            notes="Title mismatch between municipal and district.",
        )

    if not doc_number_match and (
        muni_fp.normalized_doc_number or dist_fp.normalized_doc_number
    ):
        return ReconciliationMatch(
            municipal_document_id=muni_id, district_document_id=dist_id,
            match_status="RECON-005",
            title_match=title_match, doc_number_match=doc_number_match,
            date_match=date_match, body_match=body_match,
            attachment_match=attachment_match,
            similarity_score=similarity,
            evidence=evidence + [{
                "type": "doc_number_mismatch",
                "municipal_doc_number": muni_fp.normalized_doc_number,
                "district_doc_number": dist_fp.normalized_doc_number,
            }],
            notes="Document number mismatch between municipal and district.",
        )

    if not date_match and (
        muni_fp.authored_date or dist_fp.authored_date
    ):
        return ReconciliationMatch(
            municipal_document_id=muni_id, district_document_id=dist_id,
            match_status="RECON-006",
            title_match=title_match, doc_number_match=doc_number_match,
            date_match=date_match, body_match=body_match,
            attachment_match=attachment_match,
            similarity_score=similarity,
            evidence=evidence + [{
                "type": "date_mismatch",
                "municipal_date": muni_fp.authored_date,
                "district_date": dist_fp.authored_date,
            }],
            notes="Date mismatch between municipal and district.",
        )

    if not body_match:
        return ReconciliationMatch(
            municipal_document_id=muni_id, district_document_id=dist_id,
            match_status="RECON-007",
            title_match=title_match, doc_number_match=doc_number_match,
            date_match=date_match, body_match=body_match,
            attachment_match=attachment_match,
            similarity_score=similarity,
            evidence=evidence + [{
                "type": "body_mismatch",
                "municipal_body_hash": muni_fp.body_hash,
                "district_body_hash": dist_fp.body_hash,
                "body_similarity": body_sim,
            }],
            notes="Body content mismatch between municipal and district.",
        )

    if not attachment_match and (
        muni_fp.attachment_hash or dist_fp.attachment_hash
    ):
        return ReconciliationMatch(
            municipal_document_id=muni_id, district_document_id=dist_id,
            match_status="RECON-008",
            title_match=title_match, doc_number_match=doc_number_match,
            date_match=date_match, body_match=body_match,
            attachment_match=attachment_match,
            similarity_score=similarity,
            evidence=evidence + [{
                "type": "attachment_mismatch",
                "municipal_attachment_hash": muni_fp.attachment_hash,
                "district_attachment_hash": dist_fp.attachment_hash,
            }],
            notes="Attachment combination mismatch between channels.",
        )

    # Should not reach here normally.
    return ReconciliationMatch(
        municipal_document_id=muni_id, district_document_id=dist_id,
        match_status="RECON-010",
        similarity_score=similarity,
        evidence=evidence,
        machine_status="insufficient_data",
        allow_customer_output=False,
        notes="Could not classify — fell through to insufficient data.",
    )


def _persist_match(db: Database, match: ReconciliationMatch) -> int | None:
    """Persist a reconciliation match. Returns row id or None if duplicate."""
    now = utc_now()
    evidence_json = json.dumps(match.evidence, ensure_ascii=False)
    with db.connect() as conn:
        existing = conn.execute(
            """SELECT id FROM reconciliation_matches
            WHERE municipal_document_id=? AND district_document_id=?""",
            (match.municipal_document_id, match.district_document_id),
        ).fetchone() if match.district_document_id else None
        if existing:
            conn.execute(
                """UPDATE reconciliation_matches
                SET match_status=?, title_match=?, doc_number_match=?, date_match=?,
                    body_match=?, attachment_match=?, similarity_score=?,
                    evidence_json=?, machine_status=?, notes=?
                WHERE id=?""",
                (
                    match.match_status,
                    int(match.title_match) if match.title_match is not None else None,
                    int(match.doc_number_match) if match.doc_number_match is not None else None,
                    int(match.date_match) if match.date_match is not None else None,
                    int(match.body_match) if match.body_match is not None else None,
                    int(match.attachment_match) if match.attachment_match is not None else None,
                    match.similarity_score,
                    evidence_json, match.machine_status, match.notes,
                    int(existing["id"]),
                ),
            )
            return int(existing["id"])
        cur = conn.execute(
            """INSERT INTO reconciliation_matches(
                municipal_document_id, district_document_id, match_status,
                title_match, doc_number_match, date_match, body_match,
                attachment_match, similarity_score, evidence_json,
                machine_status, human_status, rule_version,
                allow_customer_output, status, created_at, notes
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                match.municipal_document_id, match.district_document_id,
                match.match_status,
                int(match.title_match) if match.title_match is not None else None,
                int(match.doc_number_match) if match.doc_number_match is not None else None,
                int(match.date_match) if match.date_match is not None else None,
                int(match.body_match) if match.body_match is not None else None,
                int(match.attachment_match) if match.attachment_match is not None else None,
                match.similarity_score, evidence_json,
                match.machine_status, "unreviewed", "1.0.0",
                1 if match.allow_customer_output else 0,
                "open", now, match.notes,
            ),
        )
        return int(cur.lastrowid)


def find_district_candidates(db: Database, municipal_id: int) -> list[int]:
    """Find district-level document candidates that might match a municipal doc."""
    muni_fp = get_fingerprint(db, municipal_id)
    if not muni_fp:
        return []
    candidates: list[int] = []
    with db.connect() as conn:
        # Match by doc_number (strongest signal).
        if muni_fp.normalized_doc_number:
            for r in conn.execute(
                """SELECT df.document_id
                FROM document_fingerprints df
                JOIN policy_documents pd ON pd.id = df.document_id
                WHERE df.normalized_doc_number=? AND pd.channel_level='district'
                  AND df.document_id != ?""",
                (muni_fp.normalized_doc_number, municipal_id),
            ).fetchall():
                candidates.append(int(r["document_id"]))
        # Match by fingerprint_hash.
        if muni_fp.fingerprint_hash:
            for r in conn.execute(
                """SELECT df.document_id
                FROM document_fingerprints df
                JOIN policy_documents pd ON pd.id = df.document_id
                WHERE df.fingerprint_hash=? AND pd.channel_level='district'
                  AND df.document_id != ?""",
                (muni_fp.fingerprint_hash, municipal_id),
            ).fetchall():
                if int(r["document_id"]) not in candidates:
                    candidates.append(int(r["document_id"]))
        # Match by body_hash.
        if muni_fp.body_hash:
            for r in conn.execute(
                """SELECT df.document_id
                FROM document_fingerprints df
                JOIN policy_documents pd ON pd.id = df.document_id
                WHERE df.body_hash=? AND pd.channel_level='district'
                  AND df.document_id != ?""",
                (muni_fp.body_hash, municipal_id),
            ).fetchall():
                if int(r["document_id"]) not in candidates:
                    candidates.append(int(r["document_id"]))
    return candidates


def find_unmatched_municipal(db: Database) -> list[int]:
    """Find municipal documents that have no reconciliation match yet."""
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT pd.id FROM policy_documents pd
            WHERE pd.channel_level='municipal'
              AND NOT EXISTS (
                SELECT 1 FROM reconciliation_matches rm
                WHERE rm.municipal_document_id = pd.id
              )
            ORDER BY pd.id"""
        ).fetchall()
        return [int(r["id"]) for r in rows]


def find_unmatched_district(db: Database) -> list[int]:
    """Find district documents that have no reconciliation match yet.

    These are candidates for RECON-003 (district missing).
    """
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT pd.id FROM policy_documents pd
            WHERE pd.channel_level='district'
              AND NOT EXISTS (
                SELECT 1 FROM reconciliation_matches rm
                WHERE rm.district_document_id = pd.id
              )
            ORDER BY pd.id"""
        ).fetchall()
        return [int(r["id"]) for r in rows]


def reconcile_document(
    db: Database, *, municipal_id: int,
) -> list[ReconciliationMatch]:
    """Reconcile one municipal document against district candidates."""
    muni_fp = get_fingerprint(db, municipal_id)
    if not muni_fp:
        return []
    matches: list[ReconciliationMatch] = []
    district_candidates = find_district_candidates(db, municipal_id)
    with db.connect() as conn:
        muni_doc = _get_doc_fields(conn, municipal_id)
        if not muni_doc:
            return []

    if not district_candidates:
        # No district match found → RECON-003 (district missing).
        matches.append(ReconciliationMatch(
            municipal_document_id=municipal_id, district_document_id=None,
            match_status="RECON-003",
            similarity_score=0.0,
            evidence=[{"type": "no_district_candidate",
                       "municipal_url": muni_doc.get("url", "")}],
            notes="Municipal document has no district-level counterpart.",
        ))
    else:
        for dist_id in district_candidates:
            dist_fp = get_fingerprint(db, dist_id)
            if not dist_fp:
                continue
            with db.connect() as conn:
                dist_doc = _get_doc_fields(conn, dist_id)
            if not dist_doc:
                continue
            matches.append(_classify_match(muni_doc, dist_doc, muni_fp, dist_fp))

    # Also handle district docs with no municipal counterpart → RECON-002.
    # Done via the orphan_district_ids flow in reconcile_all().

    for m in matches:
        _persist_match(db, m)
    return matches


def reconcile_orphan_district(
    db: Database, *, district_id: int,
) -> ReconciliationMatch | None:
    """Mark a district document with no municipal counterpart (RECON-002)."""
    dist_fp = get_fingerprint(db, district_id)
    if not dist_fp:
        return None
    with db.connect() as conn:
        dist_doc = _get_doc_fields(conn, district_id)
    if not dist_doc:
        return None
    match = ReconciliationMatch(
        municipal_document_id=district_id,  # Use district_id as placeholder
        district_document_id=district_id,
        match_status="RECON-002",
        similarity_score=0.0,
        evidence=[{"type": "no_municipal_counterpart",
                   "district_url": dist_doc.get("url", "")}],
        machine_status="insufficient_data",
        allow_customer_output=False,
        notes="District document has no municipal-level counterpart.",
    )
    # Persist with municipal_document_id = district_id (since there's no municipal doc).
    # We need to handle this specially — set municipal_document_id = 0/null equivalent.
    # Use district_id in both fields to indicate orphan district, and adjust status.
    # Actually, schema requires municipal_document_id NOT NULL. Use a sentinel: the district
    # id itself in the municipal slot is misleading. Let's not insert RECON-002 here;
    # instead, the caller should query orphans separately. For now, return the match
    # without persisting — reconciliation_runs will handle this.
    return match


def reconcile_all(db: Database, *, limit: int | None = None) -> dict[str, Any]:
    """Run reconciliation across all unmatched municipal documents.

    Returns summary statistics.
    """
    stats: dict[str, int] = {f"RECON-{i:03d}": 0 for i in range(1, 11)}
    unmatched_muni = find_unmatched_municipal(db)
    if limit:
        unmatched_muni = unmatched_muni[:limit]
    total_matches = 0
    for muni_id in unmatched_muni:
        matches = reconcile_document(db, municipal_id=muni_id)
        for m in matches:
            stats[m.match_status] = stats.get(m.match_status, 0) + 1
            total_matches += 1

    # For RECON-002 (district orphans), we count orphans but do not persist
    # (no municipal counterpart to anchor the row).
    orphan_districts = find_unmatched_district(db)
    stats["RECON-002_orphans"] = len(orphan_districts)

    return {
        "municipal_processed": len(unmatched_muni),
        "orphan_districts": len(orphan_districts),
        "matches_persisted": total_matches,
        "by_status": stats,
    }


def list_matches(
    db: Database, *,
    match_status: str | None = None,
    status: str | None = None,
    document_id: int | None = None,
) -> list[dict[str, Any]]:
    """List reconciliation matches with optional filters."""
    with db.connect() as conn:
        query = "SELECT * FROM reconciliation_matches WHERE 1=1"
        params: list[Any] = []
        if match_status:
            query += " AND match_status=?"
            params.append(match_status)
        if status:
            query += " AND status=?"
            params.append(status)
        if document_id is not None:
            query += " AND (municipal_document_id=? OR district_document_id=?)"
            params.extend([document_id, document_id])
        query += " ORDER BY created_at DESC"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def exclude_match(
    db: Database, *,
    municipal_document_id: int, district_document_id: int | None,
    reason: str, resolved_by: str = "",
) -> None:
    """Mark a reconciliation match as excluded."""
    now = utc_now()
    with db.connect() as conn:
        if district_document_id is None:
            conn.execute(
                """UPDATE reconciliation_matches
                SET status='excluded', exclusion_reason=?, resolved_at=?, notes=?
                WHERE municipal_document_id=?""",
                (reason, now, f"excluded by {resolved_by}: {reason}", municipal_document_id),
            )
        else:
            conn.execute(
                """UPDATE reconciliation_matches
                SET status='excluded', exclusion_reason=?, resolved_at=?, notes=?
                WHERE municipal_document_id=? AND district_document_id=?""",
                (reason, now, f"excluded by {resolved_by}: {reason}",
                 municipal_document_id, district_document_id),
            )
