"""M4 duplicate detection.

Implements DUP-001~008 per V3.0 §10.2.

Document identity fingerprint (V3.0 §10.2):
- normalized_doc_number
- normalized_title
- authored_date
- issuing_agency
- body_hash (sha256 of body_text)
- pdf_hash (sha256 of pdf payload)
- attachment_hash (combination of attachment content hashes)
- body_simhash (rolling hash for similarity detection)

Duplicate types:
- DUP-001: 技术重复（同 URL 同内容同哈希）
- DUP-002: 同栏目重复（同栏目内不同 URL 同一文件）
- DUP-003: 不同 URL 同一政策文件
- DUP-004: 跨栏目正常重复（应排除，is_normal=1）
- DUP-005: 跨渠道正常同步（应排除，is_normal=1）
- DUP-006: 同 PDF 不同页面重复
- DUP-007: 文号相同内容不同
- DUP-008: 文号不同内容高度相似

Key guarantees (V3.0 §10.4):
- Normal跨渠道同步不判重复 → DUP-005 with is_normal=1
- Normal跨栏目重复不判重复 → DUP-004 with is_normal=1
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from app.db import Database, utc_now
from app.rules import normalize_document_number

logger = logging.getLogger(__name__)

# Similarity thresholds
TITLE_SIMILARITY_THRESHOLD = 0.85
BODY_SIMILARITY_THRESHOLD = 0.80
BODY_HASH_MATCH = 1.0  # Exact match

# Truncate body for simhash to avoid huge computation
BODY_SIMHASH_LENGTH = 5000


def _sha256_hex(value: str | bytes) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8", errors="ignore")
    return hashlib.sha256(value).hexdigest()


def normalize_title(title: str) -> str:
    """Normalize a policy title for fingerprint comparison."""
    if not title:
        return ""
    s = unicodedata.normalize("NFKC", title)
    s = re.sub(r"[\s\u3000]+", "", s)
    # Normalize all bracket types to full-width 〔 〕 first, then strip contents.
    s = s.replace("(", "（").replace(")", "）")
    s = s.replace("[", "〔").replace("]", "〕")
    s = s.replace("【", "〔").replace("】", "〕")
    s = s.replace("「", "〔").replace("」", "〕")
    s = s.replace("｢", "〔").replace("｣", "〕")
    # Strip parenthetical annotations (supports （ ） 〔 〕 brackets).
    s = re.sub(r"[（(〔【].*?[)）〕】]", "", s)
    s = s.strip("。·、，,")
    return s


def body_simhash(text: str, *, k: int = 64) -> str:
    """Compute a simple rolling simhash for body similarity.

    Uses shingled word counts to produce a fixed-length bit vector.
    """
    if not text:
        return ""
    text = re.sub(r"\s+", "", text)[:BODY_SIMHASH_LENGTH]
    # Split into bigrams (works for both Chinese and ASCII)
    if len(text) < 2:
        return ""
    shingles = [text[i : i + 2] for i in range(len(text) - 1)]
    bits = [0] * k
    for sh in shingles:
        h = int(hashlib.md5(sh.encode("utf-8", errors="ignore")).hexdigest(), 16)
        for i in range(k):
            bit = (h >> i) & 1
            bits[i] += 1 if bit else -1
    return "".join("1" if b > 0 else "0" for b in bits)


def hamming_distance(a: str, b: str) -> int:
    if len(a) != len(b) or not a:
        return max(len(a), len(b))
    return sum(1 for x, y in zip(a, b) if x != y)


def simhash_similarity(a: str, b: str, *, k: int = 64) -> float:
    """Convert Hamming distance to similarity in [0, 1]."""
    if not a or not b:
        return 0.0
    if len(a) != len(b):
        return 0.0
    return 1.0 - (hamming_distance(a, b) / k)


def attachment_hash(attachment_hashes: list[str]) -> str:
    """Combine attachment content hashes into one combined hash (sorted)."""
    if not attachment_hashes:
        return ""
    combined = "|".join(sorted(attachment_hashes))
    return _sha256_hex(combined)


@dataclass
class DocumentFingerprint:
    document_id: int
    normalized_doc_number: str = ""
    normalized_title: str = ""
    authored_date: str | None = None
    issuing_agency: str = ""
    body_hash: str = ""
    pdf_hash: str = ""
    attachment_hash: str = ""
    fingerprint_hash: str = ""
    body_simhash: str = ""


@dataclass
class FingerprintInputs:
    document_id: int
    page_document_number: str = ""
    title: str = ""
    authored_date: str | None = None
    issuing_agency: str = ""
    body_text: str = ""
    pdf_hash: str = ""
    attachment_hashes: list[str] = field(default_factory=list)


def compute_fingerprint(inputs: FingerprintInputs) -> DocumentFingerprint:
    """Compute a document fingerprint from raw inputs."""
    norm_dn = normalize_document_number(inputs.page_document_number) if inputs.page_document_number else ""
    norm_title = normalize_title(inputs.title) if inputs.title else ""
    body_hash = _sha256_hex(inputs.body_text) if inputs.body_text else ""
    att_hash = attachment_hash(inputs.attachment_hashes) if inputs.attachment_hashes else ""
    body_sh = body_simhash(inputs.body_text) if inputs.body_text else ""
    # Fingerprint hash combines the strongest identity signals.
    fp_seed = "|".join([
        norm_dn,
        norm_title,
        inputs.authored_date or "",
        inputs.issuing_agency,
        body_hash,
        inputs.pdf_hash,
        att_hash,
    ])
    fingerprint_hash = _sha256_hex(fp_seed) if fp_seed.replace("|", "") else ""
    return DocumentFingerprint(
        document_id=inputs.document_id,
        normalized_doc_number=norm_dn,
        normalized_title=norm_title,
        authored_date=inputs.authored_date,
        issuing_agency=inputs.issuing_agency,
        body_hash=body_hash,
        pdf_hash=inputs.pdf_hash,
        attachment_hash=att_hash,
        fingerprint_hash=fingerprint_hash,
        body_simhash=body_sh,
    )


def persist_fingerprint(db: Database, fp: DocumentFingerprint) -> None:
    """Insert or update a document fingerprint."""
    now = utc_now()
    with db.connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO document_fingerprints(
                document_id, normalized_doc_number, normalized_title, authored_date,
                issuing_agency, body_hash, pdf_hash, attachment_hash,
                fingerprint_hash, body_simhash, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                fp.document_id, fp.normalized_doc_number, fp.normalized_title,
                fp.authored_date, fp.issuing_agency, fp.body_hash, fp.pdf_hash,
                fp.attachment_hash, fp.fingerprint_hash, fp.body_simhash, now,
            ),
        )
        # Also stamp the policy_documents.fingerprint_hash for fast lookup.
        conn.execute(
            "UPDATE policy_documents SET fingerprint_hash=? WHERE id=?",
            (fp.fingerprint_hash, fp.document_id),
        )


def get_fingerprint(db: Database, document_id: int) -> DocumentFingerprint | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM document_fingerprints WHERE document_id=?",
            (document_id,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        return DocumentFingerprint(
            document_id=d["document_id"],
            normalized_doc_number=d["normalized_doc_number"],
            normalized_title=d["normalized_title"],
            authored_date=d["authored_date"],
            issuing_agency=d["issuing_agency"],
            body_hash=d["body_hash"],
            pdf_hash=d["pdf_hash"],
            attachment_hash=d["attachment_hash"],
            fingerprint_hash=d["fingerprint_hash"],
            body_simhash=d["body_simhash"],
        )


@dataclass
class DuplicateMatch:
    document_id_a: int
    document_id_b: int
    duplicate_type: str
    similarity_score: float
    is_normal: bool = False
    evidence: list[dict[str, Any]] = field(default_factory=list)
    notes: str = ""
    machine_status: str = "suspected"
    allow_customer_output: bool = True


def _get_doc_for_compare(conn, document_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT id, url, title, source_site, channel_level, district,
                  page_document_number, authored_date, issuing_agency,
                  body_text, content_hash, fingerprint_hash
        FROM policy_documents WHERE id=?""",
        (document_id,),
    ).fetchone()
    return dict(row) if row else None


def _same_channel(conn, doc_a: dict, doc_b: dict) -> bool:
    """Two documents are in the same channel if both source_site values match
    or both have the same channel_level+district."""
    a_site = (doc_a.get("source_site") or "").lower()
    b_site = (doc_b.get("source_site") or "").lower()
    if a_site and b_site:
        return a_site == b_site
    a_ch = (doc_a.get("channel_level") or "").lower()
    b_ch = (doc_b.get("channel_level") or "").lower()
    a_district = (doc_a.get("district") or "").lower()
    b_district = (doc_b.get("district") or "").lower()
    return a_ch == b_ch and a_district == b_district


def _same_target(conn, doc_a: dict, doc_b: dict) -> bool:
    """Two documents are in the same target栏目 if they share a target_key
    (looked up from scan_item_results)."""
    a_targets = {
        r["target_key"]
        for r in conn.execute(
            "SELECT target_key FROM scan_item_results WHERE document_id=?",
            (doc_a["id"],),
        ).fetchall()
    }
    b_targets = {
        r["target_key"]
        for r in conn.execute(
            "SELECT target_key FROM scan_item_results WHERE document_id=?",
            (doc_b["id"],),
        ).fetchall()
    }
    return bool(a_targets & b_targets)


def _classify_duplicate(
    conn, doc_a: dict, doc_b: dict, fp_a: DocumentFingerprint, fp_b: DocumentFingerprint,
) -> DuplicateMatch | None:
    """Classify a duplicate relationship between two documents."""
    a_id = doc_a["id"]
    b_id = doc_b["id"]
    # Ensure canonical ordering (a < b) per schema constraint.
    if a_id > b_id:
        a_id, b_id = b_id, a_id
        doc_a, doc_b = doc_b, doc_a
        fp_a, fp_b = fp_b, fp_a

    same_url = (doc_a.get("url") or "") == (doc_b.get("url") or "")
    same_body_hash = bool(fp_a.body_hash) and fp_a.body_hash == fp_b.body_hash
    same_pdf_hash = bool(fp_a.pdf_hash) and fp_a.pdf_hash == fp_b.pdf_hash
    same_doc_number = bool(fp_a.normalized_doc_number) and fp_a.normalized_doc_number == fp_b.normalized_doc_number
    same_title = bool(fp_a.normalized_title) and fp_a.normalized_title == fp_b.normalized_title
    same_agency = bool(fp_a.issuing_agency) and fp_a.issuing_agency == fp_b.issuing_agency
    same_authored_date = bool(fp_a.authored_date) and fp_a.authored_date == fp_b.authored_date

    body_sim = simhash_similarity(fp_a.body_simhash, fp_b.body_simhash) if (
        fp_a.body_simhash and fp_b.body_simhash
    ) else 0.0

    same_channel = _same_channel(conn, doc_a, doc_b)
    same_target = _same_target(conn, doc_a, doc_b)
    same_channel_level = (
        (doc_a.get("channel_level") or "").lower() == (doc_b.get("channel_level") or "").lower()
    )

    # DUP-001: 技术重复（同 URL 同内容同哈希）
    if same_url and same_body_hash:
        return DuplicateMatch(
            document_id_a=a_id, document_id_b=b_id, duplicate_type="DUP-001",
            similarity_score=1.0, is_normal=True,
            evidence=[{"type": "same_url_same_body", "url": doc_a["url"],
                       "body_hash": fp_a.body_hash}],
            notes="Technical duplicate (same URL + same body hash).",
            machine_status="confirmed_by_rule", allow_customer_output=False,
        )

    # DUP-002: 同栏目重复（同栏目内不同 URL 同一文件）
    if same_target and not same_url and same_body_hash:
        return DuplicateMatch(
            document_id_a=a_id, document_id_b=b_id, duplicate_type="DUP-002",
            similarity_score=1.0, is_normal=False,
            evidence=[{"type": "same_target_same_body_diff_url",
                       "url_a": doc_a["url"], "url_b": doc_b["url"],
                       "body_hash": fp_a.body_hash}],
            notes="Same column duplicate (different URLs, same body in same target).",
        )

    # DUP-005: 跨渠道正常同步（应排除）— check BEFORE DUP-003.
    # Municipal and district channels legitimately mirror the same policy.
    # V3.0 §10.4: 正常跨渠道同步不判重复.
    if not same_channel_level and (
        same_doc_number or same_body_hash or (same_title and body_sim >= BODY_SIMILARITY_THRESHOLD)
    ):
        return DuplicateMatch(
            document_id_a=a_id, document_id_b=b_id, duplicate_type="DUP-005",
            similarity_score=max(body_sim, 0.9 if same_body_hash else 0.0),
            is_normal=True,
            evidence=[{
                "type": "cross_channel_normal_sync",
                "channel_a": doc_a.get("channel_level", ""),
                "channel_b": doc_b.get("channel_level", ""),
                "doc_number_match": same_doc_number,
                "body_hash_match": same_body_hash,
                "body_similarity": body_sim,
            }],
            notes="Cross-channel normal synchronization (excluded by default).",
            machine_status="auto_excluded", allow_customer_output=False,
        )

    # DUP-004: 跨栏目正常重复（应排除）
    if not same_target and not same_channel and (
        same_doc_number or same_body_hash or (same_title and body_sim >= BODY_SIMILARITY_THRESHOLD)
    ):
        return DuplicateMatch(
            document_id_a=a_id, document_id_b=b_id, duplicate_type="DUP-004",
            similarity_score=max(body_sim, 0.9 if same_body_hash else 0.0),
            is_normal=True,
            evidence=[{
                "type": "cross_target_normal_duplicate",
                "target_a": doc_a.get("source_site", ""),
                "target_b": doc_b.get("source_site", ""),
                "doc_number_match": same_doc_number,
                "body_hash_match": same_body_hash,
                "body_similarity": body_sim,
            }],
            notes="Cross-column normal duplicate (excluded by default).",
            machine_status="auto_excluded", allow_customer_output=False,
        )

    # DUP-007: 文号相同内容不同 — fire whenever same doc_number but different body hash.
    # Content similarity is irrelevant: same doc_number with different content is a conflict.
    if same_doc_number and not same_body_hash:
        return DuplicateMatch(
            document_id_a=a_id, document_id_b=b_id, duplicate_type="DUP-007",
            similarity_score=0.4,
            evidence=[{
                "type": "same_doc_number_diff_content",
                "doc_number": fp_a.normalized_doc_number,
                "body_similarity": body_sim,
            }],
            notes="Same document number but different body content (potential conflict).",
        )

    # DUP-008: 文号不同内容高度相似 — check BEFORE DUP-003 when doc_numbers differ.
    # Same body hash but different doc numbers → conflict (DUP-008).
    if not same_doc_number and same_body_hash and fp_a.body_hash:
        return DuplicateMatch(
            document_id_a=a_id, document_id_b=b_id, duplicate_type="DUP-008",
            similarity_score=0.95,
            evidence=[{
                "type": "diff_doc_number_same_body",
                "doc_number_a": fp_a.normalized_doc_number,
                "doc_number_b": fp_b.normalized_doc_number,
                "body_hash": fp_a.body_hash,
            }],
            notes="Different document numbers but identical body hash.",
        )

    # DUP-003: 不同 URL 同一政策文件 (strong identity match across URLs, same channel).
    # Requires same doc_number (not just body hash) to avoid catching DUP-008 cases.
    if not same_url and same_doc_number and same_title and (
        same_agency or same_body_hash
    ):
        return DuplicateMatch(
            document_id_a=a_id, document_id_b=b_id, duplicate_type="DUP-003",
            similarity_score=0.95,
            evidence=[{
                "type": "different_url_same_policy",
                "doc_number_match": same_doc_number,
                "title_match": same_title,
                "agency_match": same_agency,
                "body_hash_match": same_body_hash,
            }],
            notes="Different URLs point to the same policy document.",
        )

    # DUP-006: 同 PDF 不同页面重复 (same PDF hash but different pages)
    if same_pdf_hash and not same_body_hash and fp_a.pdf_hash:
        return DuplicateMatch(
            document_id_a=a_id, document_id_b=b_id, duplicate_type="DUP-006",
            similarity_score=0.7,
            evidence=[{
                "type": "same_pdf_diff_page",
                "pdf_hash": fp_a.pdf_hash,
                "body_a_hash": fp_a.body_hash,
                "body_b_hash": fp_b.body_hash,
            }],
            notes="Same PDF, different pages extracted as separate documents.",
        )

    # DUP-008 (variant): different doc numbers but highly similar body (simhash).
    if not same_doc_number and body_sim >= BODY_SIMILARITY_THRESHOLD:
        return DuplicateMatch(
            document_id_a=a_id, document_id_b=b_id, duplicate_type="DUP-008",
            similarity_score=body_sim,
            evidence=[{
                "type": "diff_doc_number_similar_content",
                "doc_number_a": fp_a.normalized_doc_number,
                "doc_number_b": fp_b.normalized_doc_number,
                "body_similarity": body_sim,
                "body_hash_match": same_body_hash,
            }],
            notes="Different document numbers but highly similar body content.",
        )

    return None


def _persist_duplicate(db: Database, match: DuplicateMatch) -> int | None:
    """Persist a duplicate pair finding. Returns the row id or None if duplicate."""
    now = utc_now()
    evidence_json = json.dumps(match.evidence, ensure_ascii=False)
    a_id, b_id = sorted([match.document_id_a, match.document_id_b])
    with db.connect() as conn:
        existing = conn.execute(
            """SELECT id FROM duplicate_pairs
            WHERE document_id_a=? AND document_id_b=? AND duplicate_type=?""",
            (a_id, b_id, match.duplicate_type),
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE duplicate_pairs
                SET similarity_score=?, is_normal=?, evidence_json=?, notes=?,
                    machine_status=?
                WHERE document_id_a=? AND document_id_b=? AND duplicate_type=?""",
                (
                    match.similarity_score, 1 if match.is_normal else 0,
                    evidence_json, match.notes, match.machine_status,
                    a_id, b_id, match.duplicate_type,
                ),
            )
            return int(existing["id"])
        cur = conn.execute(
            """INSERT INTO duplicate_pairs(
                document_id_a, document_id_b, duplicate_type, similarity_score,
                is_normal, status, exclusion_reason, evidence_json,
                machine_status, human_status, rule_version,
                allow_customer_output, created_at, notes
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                a_id, b_id, match.duplicate_type, match.similarity_score,
                1 if match.is_normal else 0, "open", "", evidence_json,
                match.machine_status, "unreviewed", "1.0.0",
                1 if match.allow_customer_output else 0, now, match.notes,
            ),
        )
        return int(cur.lastrowid)


def detect_duplicates(
    db: Database,
    *,
    document_id: int,
    candidate_ids: list[int] | None = None,
) -> list[DuplicateMatch]:
    """Detect duplicates for one document against a candidate set.

    If candidate_ids is None, all other documents with matching fingerprints
    are scanned.
    """
    fp_a = get_fingerprint(db, document_id)
    if not fp_a:
        return []
    matches: list[DuplicateMatch] = []
    with db.connect() as conn:
        doc_a = _get_doc_for_compare(conn, document_id)
        if not doc_a:
            return []
        # Candidate selection: prefer strong identity signals.
        candidates: list[int] = []
        if candidate_ids:
            candidates = [cid for cid in candidate_ids if cid != document_id]
        else:
            # Find candidates by any strong identity match.
            if fp_a.fingerprint_hash:
                for r in conn.execute(
                    """SELECT document_id FROM document_fingerprints
                    WHERE fingerprint_hash=? AND document_id != ?""",
                    (fp_a.fingerprint_hash, document_id),
                ).fetchall():
                    candidates.append(int(r["document_id"]))
            if fp_a.normalized_doc_number:
                for r in conn.execute(
                    """SELECT document_id FROM document_fingerprints
                    WHERE normalized_doc_number=? AND document_id != ?""",
                    (fp_a.normalized_doc_number, document_id),
                ).fetchall():
                    if int(r["document_id"]) not in candidates:
                        candidates.append(int(r["document_id"]))
            if fp_a.body_hash:
                for r in conn.execute(
                    """SELECT document_id FROM document_fingerprints
                    WHERE body_hash=? AND document_id != ?""",
                    (fp_a.body_hash, document_id),
                ).fetchall():
                    if int(r["document_id"]) not in candidates:
                        candidates.append(int(r["document_id"]))
            if fp_a.pdf_hash:
                for r in conn.execute(
                    """SELECT document_id FROM document_fingerprints
                    WHERE pdf_hash=? AND document_id != ?""",
                    (fp_a.pdf_hash, document_id),
                ).fetchall():
                    if int(r["document_id"]) not in candidates:
                        candidates.append(int(r["document_id"]))

        for cid in candidates:
            fp_b = get_fingerprint(db, cid)
            if not fp_b:
                continue
            doc_b = _get_doc_for_compare(conn, cid)
            if not doc_b:
                continue
            match = _classify_duplicate(conn, doc_a, doc_b, fp_a, fp_b)
            if match:
                matches.append(match)

    for m in matches:
        _persist_duplicate(db, m)
    return matches


def list_duplicates(
    db: Database, *,
    document_id: int | None = None,
    duplicate_type: str | None = None,
    is_normal: bool | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """List duplicate pairs with optional filters."""
    with db.connect() as conn:
        query = "SELECT * FROM duplicate_pairs WHERE 1=1"
        params: list[Any] = []
        if document_id is not None:
            query += " AND (document_id_a=? OR document_id_b=?)"
            params.extend([document_id, document_id])
        if duplicate_type:
            query += " AND duplicate_type=?"
            params.append(duplicate_type)
        if is_normal is not None:
            query += " AND is_normal=?"
            params.append(1 if is_normal else 0)
        if status:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY created_at DESC"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def exclude_duplicate(
    db: Database, *,
    document_id_a: int, document_id_b: int, duplicate_type: str,
    reason: str, resolved_by: str = "",
) -> None:
    """Mark a duplicate pair as excluded (e.g. normal cross-channel sync)."""
    now = utc_now()
    a_id, b_id = sorted([document_id_a, document_id_b])
    with db.connect() as conn:
        conn.execute(
            """UPDATE duplicate_pairs
            SET status='excluded', exclusion_reason=?, is_normal=1, resolved_at=?, notes=?
            WHERE document_id_a=? AND document_id_b=? AND duplicate_type=?""",
            (reason, now, f"excluded by {resolved_by}: {reason}",
             a_id, b_id, duplicate_type),
        )
