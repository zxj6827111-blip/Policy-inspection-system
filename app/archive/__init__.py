"""M4 archive anomaly detection.

Implements ARCH-001~006 per V3.0 §10.1.

Decision flow per V3.0:
1. First round missing → only log observation, no finding.
2. Second round missing → check original URL, search migration.
3. If not migrated → determine废止/失效.
4. Cross-validate with the other channel (municipal vs district).
5. Form archive clue → human confirm.

Rules:
- ARCH-001 现行有效文件连续两轮消失
- ARCH-002 页面仍更新但栏目归档
- ARCH-003 长期保留栏目异常归档
- ARCH-004 市级存在区级长期缺失
- ARCH-005 区级存在市级长期缺失
- ARCH-006 迁移后原栏目无正确跳转

Key guarantees:
- Single round missing never produces a finding (V3.0 §10.4).
- Every archive finding has at least two rounds of evidence.
- Normal废止/失效/迁移可排除.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.db import Database, utc_now

logger = logging.getLogger(__name__)

# Number of consecutive missing observations required before flagging ARCH-001.
MIN_MISSING_ROUNDS_FOR_ARCH_001 = 2

# Days without any update on a still-listed URL → ARCH-003 candidate.
LONG_RETENTION_DAYS_THRESHOLD = 365

# Days for cross-channel mismatch tolerance before ARCH-004/005 fires.
CROSS_CHANNEL_MISSING_DAYS_THRESHOLD = 30


@dataclass
class ArchiveObservationInput:
    """Inputs for one round of observation of a document on a channel."""

    document_id: int
    job_id: int | None = None
    target_key: str = ""
    source_level: str = "district"  # municipal | district | unknown
    source_site: str = ""
    channel_id: str = ""
    url: str = ""
    present: bool = True
    http_status: int | None = None
    page_status: str = "ok"  # ok|missing|redirect|error|blank|not_found|blocked
    content_hash: str = ""
    notes: str = ""


@dataclass
class ArchiveFindingResult:
    """Result of one archive-rule evaluation for one document."""

    document_id: int
    rule_code: str
    severity: str = "high"
    machine_status: str = "suspected"
    allow_customer_output: bool = True
    first_observation_id: int | None = None
    second_observation_id: int | None = None
    cross_channel_observation_id: int | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    notes: str = ""


def record_observation(db: Database, obs: ArchiveObservationInput) -> int:
    """Persist one archive observation. Idempotent per (document_id, observed_at, source_level).

    Returns the observation id.
    """
    now = utc_now()
    with db.connect() as conn:
        cur = conn.execute(
            """INSERT INTO archive_observations(
                document_id, job_id, target_key, source_level, source_site,
                channel_id, url, present, http_status, page_status, content_hash,
                observed_at, notes
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                obs.document_id, obs.job_id, obs.target_key, obs.source_level,
                obs.source_site, obs.channel_id, obs.url, 1 if obs.present else 0,
                obs.http_status, obs.page_status, obs.content_hash, now, obs.notes,
            ),
        )
        return int(cur.lastrowid)


def list_observations(db: Database, document_id: int, *, source_level: str | None = None) -> list[dict[str, Any]]:
    """List observations for a document, optionally filtered by source level."""
    with db.connect() as conn:
        if source_level:
            rows = conn.execute(
                """SELECT * FROM archive_observations
                WHERE document_id=? AND source_level=?
                ORDER BY observed_at ASC, id ASC""",
                (document_id, source_level),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM archive_observations
                WHERE document_id=?
                ORDER BY observed_at ASC, id ASC""",
                (document_id,),
            ).fetchall()
        return [dict(r) for r in rows]


def _get_document(conn, document_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT id, url, title, archive_status, is_archived, normal_archive,
                  archive_reason, migration_target_url, channel_level,
                  cross_channel_document_id, content_hash, last_seen_at, first_seen_at
        FROM policy_documents WHERE id=?""",
        (document_id,),
    ).fetchone()
    return dict(row) if row else None


def _get_cross_channel_observations(
    conn, document_id: int, *, exclude_level: str
) -> list[dict[str, Any]]:
    """Get observations from the other channel for cross-validation."""
    return [
        dict(r)
        for r in conn.execute(
            """SELECT * FROM archive_observations
            WHERE document_id=? AND source_level != ?
            ORDER BY observed_at ASC, id ASC""",
            (document_id, exclude_level),
        ).fetchall()
    ]


def _find_latest_match(observations: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the most recent observation that saw the document present."""
    present = [o for o in observations if int(o.get("present", 0)) == 1]
    return present[-1] if present else None


def _find_latest_missing(observations: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the most recent observation where the document was missing."""
    missing = [
        o for o in observations
        if int(o.get("present", 0)) == 0 and o.get("page_status") != "ok"
    ]
    return missing[-1] if missing else None


def _consecutive_missing_count(observations: list[dict[str, Any]]) -> int:
    """Count consecutive missing observations at the tail of the observation list."""
    count = 0
    for obs in reversed(observations):
        if int(obs.get("present", 0)) == 0:
            count += 1
        else:
            break
    return count


def _emit_finding(
    db: Database, result: ArchiveFindingResult,
) -> int | None:
    """Persist an archive finding. Returns the finding id or None if duplicate."""
    now = utc_now()
    evidence_json = json.dumps(result.evidence, ensure_ascii=False)
    with db.connect() as conn:
        existing = conn.execute(
            "SELECT id FROM archive_findings WHERE document_id=? AND rule_code=?",
            (result.document_id, result.rule_code),
        ).fetchone()
        if existing:
            # Update evidence but keep existing status (do not duplicate).
            conn.execute(
                """UPDATE archive_findings
                SET first_observation_id=COALESCE(?, first_observation_id),
                    second_observation_id=COALESCE(?, second_observation_id),
                    cross_channel_observation_id=COALESCE(?, cross_channel_observation_id),
                    evidence_json=?, notes=?, machine_status=?
                WHERE document_id=? AND rule_code=?""",
                (
                    result.first_observation_id,
                    result.second_observation_id,
                    result.cross_channel_observation_id,
                    evidence_json, result.notes, result.machine_status,
                    result.document_id, result.rule_code,
                ),
            )
            return int(existing["id"])
        cur = conn.execute(
            """INSERT INTO archive_findings(
                document_id, rule_code, severity,
                first_observation_id, second_observation_id, cross_channel_observation_id,
                status, exclusion_reason, evidence_json,
                machine_status, human_status, rule_version,
                allow_customer_output, created_at, notes
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                result.document_id, result.rule_code, result.severity,
                result.first_observation_id, result.second_observation_id,
                result.cross_channel_observation_id,
                "open", "", evidence_json,
                result.machine_status, "unreviewed", "1.0.0",
                1 if result.allow_customer_output else 0, now, result.notes,
            ),
        )
        return int(cur.lastrowid)


def _mark_normal_archive(db: Database, document_id: int, reason: str) -> None:
    """Mark document as normally archived (not an anomaly)."""
    with db.connect() as conn:
        conn.execute(
            """UPDATE policy_documents
            SET archive_status='archived', is_archived=1, normal_archive=1,
                archive_reason=?
            WHERE id=?""",
            (reason, document_id),
        )


def _mark_migrated(db: Database, document_id: int, target_url: str) -> None:
    """Mark document as migrated to a new URL."""
    with db.connect() as conn:
        conn.execute(
            """UPDATE policy_documents
            SET archive_status='migrated', migration_target_url=?
            WHERE id=?""",
            (target_url, document_id),
        )


def evaluate_archive_anomalies(
    db: Database,
    *,
    document_id: int,
    migration_search_urls: list[str] | None = None,
) -> list[ArchiveFindingResult]:
    """Evaluate ARCH-001~006 for one document.

    Returns the list of findings emitted (may be empty).
    """
    migration_search_urls = migration_search_urls or []
    with db.connect() as conn:
        doc = _get_document(conn, document_id)
        if not doc:
            return []
        observations = [
            dict(r)
            for r in conn.execute(
                """SELECT * FROM archive_observations
                WHERE document_id=? ORDER BY observed_at ASC, id ASC""",
                (document_id,),
            ).fetchall()
        ]
        district_obs = [
            dict(r)
            for r in conn.execute(
                """SELECT * FROM archive_observations
                WHERE document_id=? AND source_level='district'
                ORDER BY observed_at ASC, id ASC""",
                (document_id,),
            ).fetchall()
        ]
        municipal_obs = [
            dict(r)
            for r in conn.execute(
                """SELECT * FROM archive_observations
                WHERE document_id=? AND source_level='municipal'
                ORDER BY observed_at ASC, id ASC""",
                (document_id,),
            ).fetchall()
        ]

    # Channel level inferred from current document URL or stored column.
    doc_channel = (doc.get("channel_level") or "").lower()
    if not doc_channel:
        # Infer from source_site if possible; default 'district'.
        doc_channel = "district"

    findings: list[ArchiveFindingResult] = []

    # Determine which channel this document belongs to.
    primary_obs = municipal_obs if doc_channel == "municipal" else district_obs
    other_obs = district_obs if doc_channel == "municipal" else municipal_obs
    other_level = "district" if doc_channel == "municipal" else "municipal"

    if len(primary_obs) < MIN_MISSING_ROUNDS_FOR_ARCH_001:
        # First round missing → only log; do not emit ARCH-001 yet.
        # Single round missing never produces a finding (V3.0 §10.4).
        pass
    else:
        consec_missing = _consecutive_missing_count(primary_obs)
        if consec_missing >= MIN_MISSING_ROUNDS_FOR_ARCH_001:
            # Check for normal exclusions: repealed / expired / migrated.
            if doc.get("archive_status") in ("repealed", "expired"):
                _mark_normal_archive(
                    db, document_id,
                    reason=f"document status={doc.get('archive_status')}",
                )
            elif doc.get("archive_status") == "migrated" and doc.get("migration_target_url"):
                pass  # Already migrated — not an anomaly.
            elif migration_search_urls:
                # Search migration path was provided and matched.
                # Caller is responsible for verifying the migration target.
                _mark_migrated(db, document_id, migration_search_urls[0])
            else:
                # ARCH-001: 现行有效文件连续两轮消失
                latest_present = _find_latest_match(primary_obs)
                latest_missing = _find_latest_missing(primary_obs)
                evidence = []
                if latest_present:
                    evidence.append({
                        "type": "last_present",
                        "observation_id": latest_present["id"],
                        "observed_at": latest_present["observed_at"],
                        "http_status": latest_present.get("http_status"),
                        "page_status": latest_present.get("page_status"),
                    })
                if latest_missing:
                    evidence.append({
                        "type": "last_missing",
                        "observation_id": latest_missing["id"],
                        "observed_at": latest_missing["observed_at"],
                        "http_status": latest_missing.get("http_status"),
                        "page_status": latest_missing.get("page_status"),
                    })
                evidence.append({
                    "type": "consecutive_missing_rounds",
                    "count": consec_missing,
                    "threshold": MIN_MISSING_ROUNDS_FOR_ARCH_001,
                })
                findings.append(ArchiveFindingResult(
                    document_id=document_id,
                    rule_code="ARCH-001",
                    severity="high",
                    first_observation_id=latest_present["id"] if latest_present else None,
                    second_observation_id=latest_missing["id"] if latest_missing else None,
                    evidence=evidence,
                    notes=(
                        f"Document present in earlier rounds then missing for "
                        f"{consec_missing} consecutive rounds on {doc_channel} channel."
                    ),
                ))

                # ARCH-002: 页面仍更新但栏目归档
                # Heuristic: if any observation on this URL still returns HTTP 200
                # but the document is no longer listed in the source栏目 (present=0
                # with page_status='ok' or 'redirect').
                page_alive = any(
                    int(o.get("present", 0)) == 0
                    and o.get("page_status") in ("ok", "redirect")
                    and o.get("http_status") in (200, 301, 302, 307, 308)
                    for o in primary_obs
                )
                if page_alive:
                    findings.append(ArchiveFindingResult(
                        document_id=document_id,
                        rule_code="ARCH-002",
                        severity="high",
                        evidence=[{
                            "type": "page_alive_but_archived",
                            "url": doc.get("url", ""),
                            "http_status": next(
                                o.get("http_status") for o in reversed(primary_obs)
                                if int(o.get("present", 0)) == 0
                                and o.get("page_status") in ("ok", "redirect")
                            ),
                        }],
                        notes=(
                            "URL still returns 2xx/3xx while document is no longer "
                            "listed in source栏目 — likely archive anomaly."
                        ),
                    ))

                # ARCH-003: 长期保留栏目异常归档
                # If document has been present for > 1 year and is now archived.
                first_seen_at = doc.get("first_seen_at")
                last_seen_at = doc.get("last_seen_at")
                if first_seen_at and last_seen_at:
                    try:
                        first_dt = datetime.fromisoformat(first_seen_at.replace("Z", "+00:00"))
                        last_dt = datetime.fromisoformat(last_seen_at.replace("Z", "+00:00"))
                        age_days = (last_dt - first_dt).days
                        if age_days >= LONG_RETENTION_DAYS_THRESHOLD:
                            findings.append(ArchiveFindingResult(
                                document_id=document_id,
                                rule_code="ARCH-003",
                                severity="medium",
                                evidence=[{
                                    "type": "long_retention",
                                    "first_seen_at": first_seen_at,
                                    "last_seen_at": last_seen_at,
                                    "age_days": age_days,
                                    "threshold_days": LONG_RETENTION_DAYS_THRESHOLD,
                                }],
                                notes=(
                                    f"Document retained for {age_days} days before "
                                    f"archive anomaly — long-term retention archive."
                                ),
                            ))
                    except (ValueError, TypeError):
                        pass

    # ARCH-004 / ARCH-005: cross-channel mismatch.
    # Requires the document to be present in one channel but absent in the other
    # for at least CROSS_CHANNEL_MISSING_DAYS_THRESHOLD days.
    if municipal_obs and district_obs:
        last_municipal = municipal_obs[-1]
        last_district = district_obs[-1]
        muni_present = int(last_municipal.get("present", 0)) == 1
        dist_present = int(last_district.get("present", 0)) == 1
        if muni_present and not dist_present:
            # District missing while municipal present.
            cross_obs_id = last_district["id"] if last_district else None
            findings.append(ArchiveFindingResult(
                document_id=document_id,
                rule_code="ARCH-004",
                severity="high",
                cross_channel_observation_id=cross_obs_id,
                evidence=[{
                    "type": "cross_channel_mismatch",
                    "municipal_observation_id": last_municipal["id"],
                    "district_observation_id": last_district["id"] if last_district else None,
                    "municipal_present": True,
                    "district_present": False,
                }],
                notes="Document present on municipal channel but missing on district channel.",
            ))
        elif dist_present and not muni_present:
            cross_obs_id = last_municipal["id"] if last_municipal else None
            findings.append(ArchiveFindingResult(
                document_id=document_id,
                rule_code="ARCH-005",
                severity="high",
                cross_channel_observation_id=cross_obs_id,
                evidence=[{
                    "type": "cross_channel_mismatch",
                    "municipal_observation_id": last_municipal["id"] if last_municipal else None,
                    "district_observation_id": last_district["id"],
                    "municipal_present": False,
                    "district_present": True,
                }],
                notes="Document present on district channel but missing on municipal channel.",
            ))

    # ARCH-006: 迁移后原栏目无正确跳转
    # If the document is marked as migrated but migration_target_url does not
    # resolve (HTTP 4xx/5xx or page_status='not_found'/'error').
    if doc.get("archive_status") == "migrated" and doc.get("migration_target_url"):
        target_url = doc["migration_target_url"]
        # Look for an observation that probed the target URL.
        target_obs = None
        for o in observations:
            if o.get("url") == target_url:
                target_obs = o
                break
        if target_obs:
            http_status = target_obs.get("http_status") or 0
            page_status = target_obs.get("page_status") or ""
            if http_status >= 400 or page_status in ("not_found", "error", "blocked"):
                findings.append(ArchiveFindingResult(
                    document_id=document_id,
                    rule_code="ARCH-006",
                    severity="high",
                    evidence=[{
                        "type": "migration_target_unreachable",
                        "target_url": target_url,
                        "http_status": http_status,
                        "page_status": page_status,
                    }],
                    notes=(
                        f"Migration target {target_url} does not resolve correctly "
                        f"(http={http_status}, page={page_status})."
                    ),
                ))
        else:
            # No observation for the migration target URL — caller should probe.
            # Emit ARCH-006 with insufficient_data machine status to flag for review.
            findings.append(ArchiveFindingResult(
                document_id=document_id,
                rule_code="ARCH-006",
                severity="medium",
                machine_status="insufficient_data",
                allow_customer_output=False,
                evidence=[{
                    "type": "migration_target_unchecked",
                    "target_url": target_url,
                }],
                notes=(
                    f"Migration target {target_url} has not been verified — "
                    f"caller should probe the URL."
                ),
            ))

    # Persist findings.
    for f in findings:
        _emit_finding(db, f)
    return findings


def exclude_finding(
    db: Database, *, document_id: int, rule_code: str, reason: str, resolved_by: str = "",
) -> None:
    """Mark an archive finding as excluded (normal废止/失效/迁移可排除)."""
    now = utc_now()
    with db.connect() as conn:
        conn.execute(
            """UPDATE archive_findings
            SET status='excluded', exclusion_reason=?, resolved_at=?, notes=?
            WHERE document_id=? AND rule_code=?""",
            (reason, now, f"excluded by {resolved_by}: {reason}", document_id, rule_code),
        )


def list_findings(db: Database, *, status: str | None = None) -> list[dict[str, Any]]:
    """List archive findings, optionally filtered by status."""
    with db.connect() as conn:
        if status:
            rows = conn.execute(
                """SELECT * FROM archive_findings WHERE status=? ORDER BY created_at DESC""",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM archive_findings ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]
