"""M4 test suite: archive anomalies, duplicates, dual-channel reconciliation.

Covers:
- ARCH-001~006 archive anomaly rules (V3.0 §10.1).
- DUP-001~008 duplicate types (V3.0 §10.2).
- RECON-001~010 reconciliation match statuses (V3.0 §10.3).
- M4 API endpoints (smoke-level).
- V3.0 §10.4 guarantees: single round missing never flags, normal废止/失效/
  migration可排除, normal cross-channel sync不判重复, evidence has双方证据.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from app.db import Database


# ---------- DB / App fixtures ----------

def _setup_db(tmp_path: Path) -> Database:
    db_path = tmp_path / "m4.db"
    db = Database(str(db_path))
    db.initialize()
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO scan_jobs(
                districts_json, mode, status, created_at, safety_json, max_documents
            ) VALUES('["普陀区"]','full','completed',?,?,0)""",
            (datetime.now(timezone.utc).isoformat(), "{}"),
        )
    return db


def _seed_document(
    db: Database, *,
    title: str = "测试政策文件",
    url: str | None = None,
    page_document_number: str = "普府〔2024〕10号",
    issuing_agency: str = "上海市普陀区人民政府",
    authored_date: str = "2024-03-15",
    body_text: str = "上海市普陀区人民政府\n普府〔2024〕10号\n2024年3月15日",
    channel_level: str = "district",
    source_site: str = "putuo",
    first_seen_at: str | None = None,
    last_seen_at: str | None = None,
) -> int:
    url = url or "https://example.test/m4-doc"
    content_hash = hashlib.sha256(body_text.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    fs = first_seen_at or now
    ls = last_seen_at or now
    with db.connect() as conn:
        job_id = conn.execute("SELECT id FROM scan_jobs ORDER BY id DESC LIMIT 1").fetchone()["id"]
        cur = conn.execute(
            """INSERT INTO policy_documents(
                district, source_id, source_site, url, title, issuing_agency,
                page_document_number, published_date, authored_date, body_text,
                topic_category, disclosure_attribute, content_hash,
                first_seen_job_id, last_seen_job_id, first_seen_at, last_seen_at,
                channel_level
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("普陀区", "", source_site, url, title, issuing_agency,
             page_document_number, None, authored_date, body_text,
             "", "", content_hash, int(job_id), int(job_id), fs, ls,
             channel_level),
        )
        return int(cur.lastrowid)


def _seed_observation(
    db: Database, *, document_id: int, present: bool = True,
    source_level: str = "district", url: str = "",
    http_status: int | None = 200, page_status: str = "ok",
    observed_at: str | None = None,
) -> int:
    from app.archive import ArchiveObservationInput, record_observation
    if observed_at:
        # Bypass utc_now by directly inserting with the provided timestamp.
        with db.connect() as conn:
            cur = conn.execute(
                """INSERT INTO archive_observations(
                    document_id, job_id, target_key, source_level, source_site,
                    channel_id, url, present, http_status, page_status, content_hash,
                    observed_at, notes
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (document_id, None, "", source_level, "", "", url,
                 1 if present else 0, http_status, page_status, "",
                 observed_at, ""),
            )
            return int(cur.lastrowid)
    obs = ArchiveObservationInput(
        document_id=document_id, source_level=source_level, url=url,
        present=present, http_status=http_status, page_status=page_status,
    )
    return record_observation(db, obs)


# ---------- Migration / schema tests ----------

class TestM4Migration:
    def test_m4_tables_created(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        with db.connect() as conn:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        for expected in (
            "archive_observations", "archive_findings",
            "document_fingerprints", "duplicate_pairs",
            "reconciliation_matches",
        ):
            assert expected in tables, f"missing table {expected}"

    def test_m4_policy_documents_columns(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        with db.connect() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(policy_documents)")}
        for expected in (
            "archive_status", "is_archived", "normal_archive", "archive_reason",
            "migration_target_url", "cross_channel_document_id",
            "fingerprint_hash", "channel_level",
        ):
            assert expected in cols, f"missing column {expected}"

    def test_m4_rule_versions_seeded(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        with db.connect() as conn:
            rows = conn.execute(
                "SELECT rule_id FROM rule_versions WHERE rule_id LIKE 'ARCH-%' OR rule_id LIKE 'DUP-%' OR rule_id LIKE 'RECON-%'"
            ).fetchall()
        rule_ids = {r["rule_id"] for r in rows}
        assert {"ARCH-001", "ARCH-006"}.issubset(rule_ids)
        assert {"DUP-001", "DUP-008"}.issubset(rule_ids)
        assert {"RECON-001", "RECON-010"}.issubset(rule_ids)

    def test_migration_is_idempotent(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        # Running initialize() twice should not error or duplicate rules.
        db.initialize()
        with db.connect() as conn:
            arch_count = conn.execute(
                "SELECT COUNT(*) FROM rule_versions WHERE rule_id='ARCH-001'"
            ).fetchone()[0]
        assert arch_count == 1


# ---------- Archive module tests ----------

class TestArchiveObservations:
    def test_record_observation_persists_row(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        obs_id = _seed_observation(db, document_id=doc_id, present=True)
        assert obs_id > 0
        from app.archive import list_observations
        obs = list_observations(db, doc_id)
        assert len(obs) == 1
        assert int(obs[0]["present"]) == 1

    def test_list_observations_filter_by_source_level(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        _seed_observation(db, document_id=doc_id, source_level="district", present=True)
        _seed_observation(db, document_id=doc_id, source_level="municipal", present=False,
                          page_status="missing", http_status=404)
        from app.archive import list_observations
        dist_obs = list_observations(db, doc_id, source_level="district")
        muni_obs = list_observations(db, doc_id, source_level="municipal")
        assert len(dist_obs) == 1
        assert len(muni_obs) == 1
        assert int(muni_obs[0]["present"]) == 0


class TestArchiveAnomalyDetection:
    """V3.0 §10.4 guarantees:
    - Single round missing never flags.
    - Normal废止/失效/迁移可排除.
    - 归档 finding 至少有两轮证据.
    """

    def test_single_round_missing_does_not_flag(self, tmp_path: Path):
        """V3.0 §10.4: 单轮缺失不报异常."""
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        # Only one round, missing.
        _seed_observation(db, document_id=doc_id, present=False, page_status="missing",
                          http_status=404)
        from app.archive import evaluate_archive_anomalies, list_findings
        findings = evaluate_archive_anomalies(db, document_id=doc_id)
        assert not findings, "Single round missing must not produce a finding"
        # No archive_findings rows persisted.
        all_findings = list_findings(db)
        assert len(all_findings) == 0

    def test_two_rounds_missing_flags_arch_001(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        # Round 1: present
        _seed_observation(db, document_id=doc_id, present=True,
                          observed_at="2026-01-01T00:00:00+00:00")
        # Round 2: missing
        _seed_observation(db, document_id=doc_id, present=False, page_status="missing",
                          http_status=404, observed_at="2026-02-01T00:00:00+00:00")
        # Round 3: missing
        _seed_observation(db, document_id=doc_id, present=False, page_status="missing",
                          http_status=404, observed_at="2026-03-01T00:00:00+00:00")
        from app.archive import evaluate_archive_anomalies, list_findings
        findings = evaluate_archive_anomalies(db, document_id=doc_id)
        rule_codes = {f.rule_code for f in findings}
        assert "ARCH-001" in rule_codes, "Two consecutive missing rounds must flag ARCH-001"
        # Finding has two-round evidence.
        persisted = list_findings(db, status="open")
        arch_001 = next(f for f in persisted if f["rule_code"] == "ARCH-001")
        assert arch_001["first_observation_id"] is not None
        assert arch_001["second_observation_id"] is not None

    def test_normal_repealed_excluded(self, tmp_path: Path):
        """V3.0 §10.4: 正常废止、失效、迁移可排除."""
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        with db.connect() as conn:
            conn.execute(
                "UPDATE policy_documents SET archive_status='repealed' WHERE id=?",
                (doc_id,),
            )
        _seed_observation(db, document_id=doc_id, present=True,
                          observed_at="2026-01-01T00:00:00+00:00")
        _seed_observation(db, document_id=doc_id, present=False, page_status="missing",
                          http_status=404, observed_at="2026-02-01T00:00:00+00:00")
        _seed_observation(db, document_id=doc_id, present=False, page_status="missing",
                          http_status=404, observed_at="2026-03-01T00:00:00+00:00")
        from app.archive import evaluate_archive_anomalies, list_findings
        findings = evaluate_archive_anomalies(db, document_id=doc_id)
        assert not any(f.rule_code == "ARCH-001" for f in findings), \
            "Repealed documents should not flag ARCH-001"
        with db.connect() as conn:
            row = conn.execute(
                "SELECT normal_archive, archive_status FROM policy_documents WHERE id=?",
                (doc_id,),
            ).fetchone()
        assert int(row["normal_archive"]) == 1
        assert row["archive_status"] == "archived"

    def test_normal_expired_excluded(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        with db.connect() as conn:
            conn.execute(
                "UPDATE policy_documents SET archive_status='expired' WHERE id=?",
                (doc_id,),
            )
        _seed_observation(db, document_id=doc_id, present=True,
                          observed_at="2026-01-01T00:00:00+00:00")
        _seed_observation(db, document_id=doc_id, present=False, page_status="missing",
                          http_status=404, observed_at="2026-02-01T00:00:00+00:00")
        _seed_observation(db, document_id=doc_id, present=False, page_status="missing",
                          http_status=404, observed_at="2026-03-01T00:00:00+00:00")
        from app.archive import evaluate_archive_anomalies
        findings = evaluate_archive_anomalies(db, document_id=doc_id)
        assert not any(f.rule_code == "ARCH-001" for f in findings)

    def test_migrated_not_flagged_when_target_present(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        target_url = "https://example.test/migrated"
        with db.connect() as conn:
            conn.execute(
                """UPDATE policy_documents
                SET archive_status='migrated', migration_target_url=? WHERE id=?""",
                (target_url, doc_id),
            )
        # Two rounds missing on original URL.
        _seed_observation(db, document_id=doc_id, present=True,
                          observed_at="2026-01-01T00:00:00+00:00")
        _seed_observation(db, document_id=doc_id, present=False, page_status="missing",
                          http_status=404, observed_at="2026-02-01T00:00:00+00:00")
        _seed_observation(db, document_id=doc_id, present=False, page_status="missing",
                          http_status=404, observed_at="2026-03-01T00:00:00+00:00")
        from app.archive import evaluate_archive_anomalies
        findings = evaluate_archive_anomalies(db, document_id=doc_id)
        # ARCH-001 should not fire because status=migrated.
        assert not any(f.rule_code == "ARCH-001" for f in findings)
        # ARCH-006 should fire (migration target not yet verified).
        arch_006 = [f for f in findings if f.rule_code == "ARCH-006"]
        assert len(arch_006) == 1
        assert arch_006[0].machine_status == "insufficient_data"

    def test_arch_002_page_alive_but_archived(self, tmp_path: Path):
        """页面仍更新但栏目归档: URL returns 2xx but document marked missing."""
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        # Round 1: present.
        _seed_observation(db, document_id=doc_id, present=True,
                          observed_at="2026-01-01T00:00:00+00:00")
        # Round 2: missing but page returns 200 (page alive).
        _seed_observation(db, document_id=doc_id, present=False, page_status="ok",
                          http_status=200, observed_at="2026-02-01T00:00:00+00:00")
        # Round 3: missing but page returns 200.
        _seed_observation(db, document_id=doc_id, present=False, page_status="ok",
                          http_status=200, observed_at="2026-03-01T00:00:00+00:00")
        from app.archive import evaluate_archive_anomalies
        findings = evaluate_archive_anomalies(db, document_id=doc_id)
        rule_codes = {f.rule_code for f in findings}
        assert "ARCH-001" in rule_codes
        assert "ARCH-002" in rule_codes

    def test_arch_003_long_retention(self, tmp_path: Path):
        """长期保留栏目异常归档: doc was present for >1 year."""
        db = _setup_db(tmp_path)
        # Use documents with old first_seen_at and recent last_seen_at.
        old_iso = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        recent_iso = datetime.now(timezone.utc).isoformat()
        doc_id = _seed_document(db, first_seen_at=old_iso, last_seen_at=recent_iso)
        _seed_observation(db, document_id=doc_id, present=True,
                          observed_at=old_iso)
        _seed_observation(db, document_id=doc_id, present=False, page_status="missing",
                          http_status=404, observed_at=recent_iso)
        _seed_observation(db, document_id=doc_id, present=False, page_status="missing",
                          http_status=404, observed_at=recent_iso)
        from app.archive import evaluate_archive_anomalies
        findings = evaluate_archive_anomalies(db, document_id=doc_id)
        rule_codes = {f.rule_code for f in findings}
        assert "ARCH-003" in rule_codes

    def test_arch_004_municipal_present_district_missing(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db, channel_level="district")
        # Municipal channel: present.
        _seed_observation(db, document_id=doc_id, source_level="municipal",
                          present=True, observed_at="2026-01-01T00:00:00+00:00")
        # District channel: missing.
        _seed_observation(db, document_id=doc_id, source_level="district",
                          present=False, page_status="missing", http_status=404,
                          observed_at="2026-02-01T00:00:00+00:00")
        from app.archive import evaluate_archive_anomalies
        findings = evaluate_archive_anomalies(db, document_id=doc_id)
        rule_codes = {f.rule_code for f in findings}
        assert "ARCH-004" in rule_codes

    def test_arch_005_district_present_municipal_missing(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db, channel_level="district")
        # District channel: present.
        _seed_observation(db, document_id=doc_id, source_level="district",
                          present=True, observed_at="2026-01-01T00:00:00+00:00")
        # Municipal channel: missing.
        _seed_observation(db, document_id=doc_id, source_level="municipal",
                          present=False, page_status="missing", http_status=404,
                          observed_at="2026-02-01T00:00:00+00:00")
        from app.archive import evaluate_archive_anomalies
        findings = evaluate_archive_anomalies(db, document_id=doc_id)
        rule_codes = {f.rule_code for f in findings}
        assert "ARCH-005" in rule_codes

    def test_arch_006_migration_target_unreachable(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        target_url = "https://example.test/broken-migration"
        with db.connect() as conn:
            conn.execute(
                """UPDATE policy_documents
                SET archive_status='migrated', migration_target_url=? WHERE id=?""",
                (target_url, doc_id),
            )
        # Round present, then migration target probed: 404.
        _seed_observation(db, document_id=doc_id, present=True,
                          observed_at="2026-01-01T00:00:00+00:00")
        _seed_observation(db, document_id=doc_id, present=False,
                          url=target_url, page_status="not_found",
                          http_status=404, observed_at="2026-02-01T00:00:00+00:00")
        from app.archive import evaluate_archive_anomalies
        findings = evaluate_archive_anomalies(db, document_id=doc_id)
        arch_006 = [f for f in findings if f.rule_code == "ARCH-006"]
        assert len(arch_006) == 1
        assert arch_006[0].machine_status == "suspected"
        assert arch_006[0].allow_customer_output is True

    def test_exclude_finding_marks_excluded(self, tmp_path: Path):
        """Normal废止/失效/迁移可排除: caller can exclude a finding."""
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        _seed_observation(db, document_id=doc_id, present=True,
                          observed_at="2026-01-01T00:00:00+00:00")
        _seed_observation(db, document_id=doc_id, present=False, page_status="missing",
                          http_status=404, observed_at="2026-02-01T00:00:00+00:00")
        _seed_observation(db, document_id=doc_id, present=False, page_status="missing",
                          http_status=404, observed_at="2026-03-01T00:00:00+00:00")
        from app.archive import evaluate_archive_anomalies, exclude_finding, list_findings
        evaluate_archive_anomalies(db, document_id=doc_id)
        exclude_finding(db, document_id=doc_id, rule_code="ARCH-001",
                        reason="normal migration", resolved_by="tester")
        excluded = list_findings(db, status="excluded")
        assert any(f["document_id"] == doc_id and f["rule_code"] == "ARCH-001"
                   for f in excluded)


# ---------- Duplicates module tests ----------

class TestFingerprints:
    def test_compute_fingerprint_normalizes_inputs(self):
        from app.duplicates import FingerprintInputs, compute_fingerprint
        fp = compute_fingerprint(FingerprintInputs(
            document_id=1,
            page_document_number="普府〔2024〕10号",
            title="测试标题（注释）",
            authored_date="2024-03-15",
            issuing_agency="上海市普陀区人民政府",
            body_text="正文内容",
        ))
        assert fp.normalized_doc_number  # normalized form
        assert fp.normalized_title  # parentheses stripped
        assert fp.body_hash  # sha256 of body_text
        assert fp.fingerprint_hash  # combined hash

    def test_normalize_title_strips_parentheses(self):
        from app.duplicates import normalize_title
        assert normalize_title("政策（试行）") == "政策"
        assert normalize_title("政策[2024]") == "政策"
        assert normalize_title("  政策  ") == "政策"

    def test_body_simhash_is_deterministic(self):
        from app.duplicates import body_simhash
        a = body_simhash("同一正文内容")
        b = body_simhash("同一正文内容")
        assert a == b
        assert len(a) == 64

    def test_simhash_similarity_identical_strings(self):
        from app.duplicates import body_simhash, simhash_similarity
        a = body_simhash("这是一段政策正文，包含很多内容用于指纹比对。")
        b = body_simhash("这是一段政策正文，包含很多内容用于指纹比对。")
        assert simhash_similarity(a, b) == 1.0

    def test_persist_and_get_fingerprint(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_id = _seed_document(db)
        from app.duplicates import (
            FingerprintInputs, compute_fingerprint, persist_fingerprint, get_fingerprint,
        )
        fp = compute_fingerprint(FingerprintInputs(
            document_id=doc_id,
            page_document_number="普府〔2024〕10号",
            title="测试标题",
            body_text="正文",
        ))
        persist_fingerprint(db, fp)
        # Verify policy_documents.fingerprint_hash also stamped.
        with db.connect() as conn:
            doc_fp = conn.execute(
                "SELECT fingerprint_hash FROM policy_documents WHERE id=?",
                (doc_id,),
            ).fetchone()
        assert doc_fp["fingerprint_hash"] == fp.fingerprint_hash
        # Verify retrieval via get_fingerprint.
        loaded = get_fingerprint(db, doc_id)
        assert loaded is not None
        assert loaded.fingerprint_hash == fp.fingerprint_hash


class TestDuplicateClassification:
    """V3.0 §10.4: 正常跨渠道同步不判重复.
    V3.0 §10.2: 8 duplicate types.
    """

    def test_dup_001_technical_duplicate_same_url_same_body(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        body = "同一正文内容"
        url_a = "https://example.test/same-url"
        doc_a = _seed_document(db, url=url_a, body_text=body)
        doc_b = _seed_document(db, url=url_a, body_text=body)
        from app.duplicates import (
            FingerprintInputs, compute_fingerprint, persist_fingerprint,
            detect_duplicates,
        )
        for did in (doc_a, doc_b):
            fp = compute_fingerprint(FingerprintInputs(
                document_id=did, title="title", body_text=body,
            ))
            persist_fingerprint(db, fp)
        matches = detect_duplicates(db, document_id=doc_a, candidate_ids=[doc_b])
        types = {m.duplicate_type for m in matches}
        assert "DUP-001" in types
        dup_001 = next(m for m in matches if m.duplicate_type == "DUP-001")
        assert dup_001.is_normal is True

    def test_dup_002_same_target_same_body_diff_url(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        body = "同一正文内容same_target"
        doc_a = _seed_document(db, url="https://example.test/a", body_text=body,
                                source_site="putuo")
        doc_b = _seed_document(db, url="https://example.test/b", body_text=body,
                                source_site="putuo")
        # Same target_key via scan_item_results (use distinct item_index to avoid UNIQUE conflict).
        with db.connect() as conn:
            for idx, did in enumerate((doc_a, doc_b), start=1):
                conn.execute(
                    """INSERT INTO scan_item_results(
                        job_id, target_key, source_label, page_number, item_index,
                        title, url, detail_status, checked_at, document_id
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (1, "putuo_qzfwj", "label", 1, idx, "title",
                     f"https://example.test/{did}", "completed", "2026-01-01", did),
                )
        from app.duplicates import (
            FingerprintInputs, compute_fingerprint, persist_fingerprint,
            detect_duplicates,
        )
        for did in (doc_a, doc_b):
            fp = compute_fingerprint(FingerprintInputs(
                document_id=did, title="title", body_text=body,
            ))
            persist_fingerprint(db, fp)
        matches = detect_duplicates(db, document_id=doc_a, candidate_ids=[doc_b])
        types = {m.duplicate_type for m in matches}
        assert "DUP-002" in types

    def test_dup_003_different_url_same_policy(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        body = "同一正文内容 for dup 003"
        doc_a = _seed_document(db, url="https://example.test/x1", body_text=body,
                                page_document_number="普府〔2024〕10号",
                                issuing_agency="普陀区人民政府")
        doc_b = _seed_document(db, url="https://example.test/x2", body_text=body,
                                page_document_number="普府〔2024〕10号",
                                issuing_agency="普陀区人民政府")
        from app.duplicates import (
            FingerprintInputs, compute_fingerprint, persist_fingerprint,
            detect_duplicates,
        )
        for did in (doc_a, doc_b):
            fp = compute_fingerprint(FingerprintInputs(
                document_id=did, title="same title",
                page_document_number="普府〔2024〕10号",
                issuing_agency="普陀区人民政府", body_text=body,
            ))
            persist_fingerprint(db, fp)
        matches = detect_duplicates(db, document_id=doc_a, candidate_ids=[doc_b])
        types = {m.duplicate_type for m in matches}
        # With different URLs and same doc_number+title+agency → DUP-003.
        assert "DUP-003" in types

    def test_dup_005_cross_channel_normal_sync(self, tmp_path: Path):
        """V3.0 §10.4: 正常跨渠道同步不判重复."""
        db = _setup_db(tmp_path)
        body = "跨渠道同步正文"
        # Municipal channel.
        doc_muni = _seed_document(db, url="https://muni.test/p", body_text=body,
                                   channel_level="municipal", source_site="muni")
        # District channel.
        doc_dist = _seed_document(db, url="https://dist.test/p", body_text=body,
                                   channel_level="district", source_site="putuo")
        from app.duplicates import (
            FingerprintInputs, compute_fingerprint, persist_fingerprint,
            detect_duplicates,
        )
        for did in (doc_muni, doc_dist):
            fp = compute_fingerprint(FingerprintInputs(
                document_id=did, title="same title", body_text=body,
            ))
            persist_fingerprint(db, fp)
        matches = detect_duplicates(db, document_id=doc_muni, candidate_ids=[doc_dist])
        types = {m.duplicate_type for m in matches}
        assert "DUP-005" in types
        dup_005 = next(m for m in matches if m.duplicate_type == "DUP-005")
        assert dup_005.is_normal is True
        assert dup_005.machine_status == "auto_excluded"

    def test_dup_007_same_doc_number_diff_content(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        body_a = "完全不同的正文 A 内容"
        body_b = "完全不同的正文 B 内容"
        doc_a = _seed_document(db, url="https://example.test/d1", body_text=body_a,
                                page_document_number="普府〔2024〕10号")
        doc_b = _seed_document(db, url="https://example.test/d2", body_text=body_b,
                                page_document_number="普府〔2024〕10号")
        from app.duplicates import (
            FingerprintInputs, compute_fingerprint, persist_fingerprint,
            detect_duplicates,
        )
        for did, body in ((doc_a, body_a), (doc_b, body_b)):
            fp = compute_fingerprint(FingerprintInputs(
                document_id=did, title="title",
                page_document_number="普府〔2024〕10号",
                body_text=body,
            ))
            persist_fingerprint(db, fp)
        matches = detect_duplicates(db, document_id=doc_a, candidate_ids=[doc_b])
        types = {m.duplicate_type for m in matches}
        assert "DUP-007" in types

    def test_dup_008_diff_doc_number_similar_content(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        body = "完全相同的正文内容用于 dup 008 测试"
        doc_a = _seed_document(db, url="https://example.test/e1", body_text=body,
                                page_document_number="普府〔2024〕10号")
        doc_b = _seed_document(db, url="https://example.test/e2", body_text=body,
                                page_document_number="普府〔2024〕11号")
        from app.duplicates import (
            FingerprintInputs, compute_fingerprint, persist_fingerprint,
            detect_duplicates,
        )
        for did in (doc_a, doc_b):
            fp = compute_fingerprint(FingerprintInputs(
                document_id=did, title="title",
                page_document_number="普府〔2024〕" + ("10号" if did == doc_a else "11号"),
                body_text=body,
            ))
            persist_fingerprint(db, fp)
        matches = detect_duplicates(db, document_id=doc_a, candidate_ids=[doc_b])
        types = {m.duplicate_type for m in matches}
        # Same body hash with different doc numbers → DUP-008 (or DUP-003 if no doc nums).
        assert "DUP-008" in types

    def test_no_duplicates_when_documents_are_distinct(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_a = _seed_document(db, url="https://example.test/u1",
                                body_text="正文A unique content",
                                page_document_number="普府〔2024〕10号")
        doc_b = _seed_document(db, url="https://example.test/u2",
                                body_text="正文B unique content entirely different",
                                page_document_number="普府〔2024〕11号")
        from app.duplicates import (
            FingerprintInputs, compute_fingerprint, persist_fingerprint,
            detect_duplicates,
        )
        for did, body, num in (
            (doc_a, "正文A unique content", "普府〔2024〕10号"),
            (doc_b, "正文B unique content entirely different", "普府〔2024〕11号"),
        ):
            fp = compute_fingerprint(FingerprintInputs(
                document_id=did, title="title",
                page_document_number=num, body_text=body,
            ))
            persist_fingerprint(db, fp)
        matches = detect_duplicates(db, document_id=doc_a, candidate_ids=[doc_b])
        assert matches == []

    def test_exclude_duplicate_marks_excluded(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        body = "同一正文内容 for exclude"
        url = "https://example.test/exclude-dup"
        doc_a = _seed_document(db, url=url, body_text=body)
        doc_b = _seed_document(db, url=url, body_text=body)
        from app.duplicates import (
            FingerprintInputs, compute_fingerprint, persist_fingerprint,
            detect_duplicates, exclude_duplicate, list_duplicates,
        )
        for did in (doc_a, doc_b):
            fp = compute_fingerprint(FingerprintInputs(
                document_id=did, title="title", body_text=body,
            ))
            persist_fingerprint(db, fp)
        detect_duplicates(db, document_id=doc_a, candidate_ids=[doc_b])
        a_id, b_id = sorted([doc_a, doc_b])
        exclude_duplicate(db, document_id_a=a_id, document_id_b=b_id,
                          duplicate_type="DUP-001", reason="normal sync",
                          resolved_by="tester")
        excluded = list_duplicates(db, status="excluded")
        assert any(
            e["document_id_a"] == a_id and e["document_id_b"] == b_id
            and e["duplicate_type"] == "DUP-001"
            for e in excluded
        )


# ---------- Reconciliation module tests ----------

class TestReconciliation:
    """V3.0 §10.3 + §10.4: RECON-001~010.
    Guarantees: 无法匹配时进入数据不足; 对账差异有双方证据.
    """

    def _seed_pair(self, db, *, muni_overrides=None, dist_overrides=None):
        muni_overrides = muni_overrides or {}
        dist_overrides = dist_overrides or {}
        # Allow callers to override url via muni_overrides/dist_overrides.
        muni_url = muni_overrides.pop("url", "https://muni.test/doc")
        dist_url = dist_overrides.pop("url", "https://dist.test/doc")
        doc_muni = _seed_document(db, channel_level="municipal", source_site="muni",
                                   url=muni_url, **muni_overrides)
        doc_dist = _seed_document(db, channel_level="district", source_site="putuo",
                                   url=dist_url, **dist_overrides)
        from app.duplicates import (
            FingerprintInputs, compute_fingerprint, persist_fingerprint,
        )
        for did, overrides in ((doc_muni, muni_overrides), (doc_dist, dist_overrides)):
            fp = compute_fingerprint(FingerprintInputs(
                document_id=did,
                title=overrides.get("title", "测试标题"),
                page_document_number=overrides.get("page_document_number",
                                                    "普府〔2024〕10号"),
                issuing_agency=overrides.get("issuing_agency",
                                              "上海市普陀区人民政府"),
                authored_date=overrides.get("authored_date", "2024-03-15"),
                body_text=overrides.get("body_text", "正文内容"),
            ))
            persist_fingerprint(db, fp)
        return doc_muni, doc_dist

    def test_recon_001_full_match(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        body = "完全一致的正文内容"
        doc_m, doc_d = self._seed_pair(db, muni_overrides={"body_text": body},
                                         dist_overrides={"body_text": body})
        from app.reconciliation import reconcile_document
        matches = reconcile_document(db, municipal_id=doc_m)
        statuses = {m.match_status for m in matches}
        assert "RECON-001" in statuses

    def test_recon_003_no_district_candidate(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        body = "unique municipal body"
        doc_muni = _seed_document(db, channel_level="municipal", source_site="muni",
                                   url="https://muni.test/orphan", body_text=body,
                                   page_document_number="沪府发〔2024〕99号")
        from app.duplicates import (
            FingerprintInputs, compute_fingerprint, persist_fingerprint,
        )
        fp = compute_fingerprint(FingerprintInputs(
            document_id=doc_muni, title="orphan",
            page_document_number="沪府发〔2024〕99号", body_text=body,
        ))
        persist_fingerprint(db, fp)
        from app.reconciliation import reconcile_document
        matches = reconcile_document(db, municipal_id=doc_muni)
        statuses = {m.match_status for m in matches}
        assert "RECON-003" in statuses

    def test_recon_004_title_mismatch(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        body = "正文相同但标题不同"
        doc_m, doc_d = self._seed_pair(
            db,
            muni_overrides={"title": "标题一", "body_text": body},
            dist_overrides={"title": "标题二", "body_text": body},
        )
        from app.reconciliation import reconcile_document
        matches = reconcile_document(db, municipal_id=doc_m)
        statuses = {m.match_status for m in matches}
        assert "RECON-004" in statuses

    def test_recon_005_doc_number_mismatch(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        body = "正文相同但文号不同"
        doc_m, doc_d = self._seed_pair(
            db,
            muni_overrides={"body_text": body,
                              "page_document_number": "沪府发〔2024〕10号"},
            dist_overrides={"body_text": body,
                              "page_document_number": "普府〔2024〕10号"},
        )
        from app.reconciliation import reconcile_document
        matches = reconcile_document(db, municipal_id=doc_m)
        statuses = {m.match_status for m in matches}
        # Title same, body same, but doc numbers differ → RECON-005.
        assert "RECON-005" in statuses

    def test_recon_006_date_mismatch(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        body = "正文相同但日期不同"
        doc_m, doc_d = self._seed_pair(
            db,
            muni_overrides={"body_text": body, "authored_date": "2024-03-15"},
            dist_overrides={"body_text": body, "authored_date": "2024-04-20"},
        )
        from app.reconciliation import reconcile_document
        matches = reconcile_document(db, municipal_id=doc_m)
        statuses = {m.match_status for m in matches}
        # doc_number match, title match, body match, but date differs → RECON-006.
        assert "RECON-006" in statuses

    def test_recon_007_body_mismatch(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        doc_m, doc_d = self._seed_pair(
            db,
            muni_overrides={"body_text": "正文 A content one"},
            dist_overrides={"body_text": "正文 B content two"},
        )
        from app.reconciliation import reconcile_document
        matches = reconcile_document(db, municipal_id=doc_m)
        statuses = {m.match_status for m in matches}
        # Title/doc_number/date all match, body differs → RECON-007.
        assert "RECON-007" in statuses

    def test_recon_009_update_delay(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        body = "正文相同但更新时间相差很大"
        old_iso = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        recent_iso = datetime.now(timezone.utc).isoformat()
        doc_m, doc_d = self._seed_pair(
            db,
            muni_overrides={"body_text": body, "last_seen_at": old_iso,
                              "first_seen_at": old_iso},
            dist_overrides={"body_text": body, "last_seen_at": recent_iso,
                              "first_seen_at": recent_iso},
        )
        from app.reconciliation import reconcile_document
        matches = reconcile_document(db, municipal_id=doc_m)
        statuses = {m.match_status for m in matches}
        assert "RECON-009" in statuses

    def test_recon_010_insufficient_data(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        # Two docs with no shared identity signal.
        doc_m = _seed_document(db, channel_level="municipal", source_site="muni",
                                url="https://muni.test/x", body_text="muni body unique",
                                page_document_number="", title="unique title muni",
                                issuing_agency="")
        doc_d = _seed_document(db, channel_level="district", source_site="putuo",
                                url="https://dist.test/y", body_text="district body unique",
                                page_document_number="", title="unique title dist",
                                issuing_agency="")
        from app.duplicates import (
            FingerprintInputs, compute_fingerprint, persist_fingerprint,
        )
        for did, body in ((doc_m, "muni body unique"), (doc_d, "district body unique")):
            fp = compute_fingerprint(FingerprintInputs(
                document_id=did, title="unique title " + ("muni" if did == doc_m else "dist"),
                body_text=body,
            ))
            persist_fingerprint(db, fp)
        # Force candidate matching via direct candidate_ids.
        from app.reconciliation import _classify_match, _get_doc_fields
        from app.duplicates import get_fingerprint
        with db.connect() as conn:
            muni_doc = _get_doc_fields(conn, doc_m)
            dist_doc = _get_doc_fields(conn, doc_d)
        muni_fp = get_fingerprint(db, doc_m)
        dist_fp = get_fingerprint(db, doc_d)
        match = _classify_match(muni_doc, dist_doc, muni_fp, dist_fp)
        assert match.match_status == "RECON-010"

    def test_reconcile_all_summary(self, tmp_path: Path):
        db = _setup_db(tmp_path)
        body = "reconcile all body"
        doc_m = _seed_document(db, channel_level="municipal", source_site="muni",
                                url="https://muni.test/all", body_text=body,
                                page_document_number="沪府发〔2024〕1号")
        doc_d = _seed_document(db, channel_level="district", source_site="putuo",
                                url="https://dist.test/all", body_text=body,
                                page_document_number="沪府发〔2024〕1号")
        from app.duplicates import (
            FingerprintInputs, compute_fingerprint, persist_fingerprint,
        )
        for did in (doc_m, doc_d):
            fp = compute_fingerprint(FingerprintInputs(
                document_id=did, title="title",
                page_document_number="沪府发〔2024〕1号", body_text=body,
            ))
            persist_fingerprint(db, fp)
        from app.reconciliation import reconcile_all
        summary = reconcile_all(db)
        assert summary["municipal_processed"] >= 1
        assert summary["matches_persisted"] >= 1
        assert "by_status" in summary

    def test_evidence_has_both_urls(self, tmp_path: Path):
        """V3.0 §10.4: 对账差异有双方证据."""
        db = _setup_db(tmp_path)
        body = "双方证据正文"
        doc_m, doc_d = self._seed_pair(
            db,
            muni_overrides={"body_text": body, "url": "https://muni.test/evidence"},
            dist_overrides={"body_text": body, "url": "https://dist.test/evidence"},
        )
        from app.reconciliation import reconcile_document, list_matches
        reconcile_document(db, municipal_id=doc_m)
        matches = list_matches(db, document_id=doc_m)
        assert matches, "expected at least one match persisted"
        evidence = __import__("json").loads(matches[0]["evidence_json"])
        urls = [e.get("url") for e in evidence if e.get("type") in ("municipal_url", "district_url")]
        assert "https://muni.test/evidence" in urls
        assert "https://dist.test/evidence" in urls


# ---------- API smoke tests ----------

@pytest.mark.real_auth
class TestM4API:
    """Smoke tests for M4 endpoints — full auth flow tested elsewhere.

    Uses the same pattern as test_api_review.py: monkeypatch the module-level
    db/manager and use TestClient as a context manager to trigger lifespan.
    Marked with @pytest.mark.real_auth so auth is enabled (not bypassed).
    """

    def test_archive_observation_endpoint_requires_auth(self, tmp_path: Path, monkeypatch):
        from fastapi.testclient import TestClient
        import app.main as main_module
        from app.db import Database
        from app.jobs import JobManager
        from app.runtime_config import reset_runtime_settings_cache

        # Enable auth for this test (real_auth marker sets env, but ensure cache is fresh).
        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("POLICY_AUTH_DISABLED", "0")
        reset_runtime_settings_cache()
        db = Database(str(tmp_path / "m4_api.db"))
        db.initialize()
        monkeypatch.setattr(main_module, "db", db)
        monkeypatch.setattr(main_module, "manager", JobManager(db))
        with TestClient(main_module.app) as client:
            # No login → 401.
            resp = client.post("/api/archive/observations/1", json={"source_level": "district"})
            assert resp.status_code in (401, 403)

    def test_fingerprint_endpoint_requires_admin(self, tmp_path: Path, monkeypatch):
        from fastapi.testclient import TestClient
        import app.main as main_module
        from app.db import Database
        from app.jobs import JobManager
        from app.runtime_config import reset_runtime_settings_cache

        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("POLICY_AUTH_DISABLED", "0")
        reset_runtime_settings_cache()
        db = Database(str(tmp_path / "m4_api_fp.db"))
        db.initialize()
        monkeypatch.setattr(main_module, "db", db)
        monkeypatch.setattr(main_module, "manager", JobManager(db))
        with TestClient(main_module.app) as client:
            resp = client.post("/api/fingerprints/1", json={})
            assert resp.status_code in (401, 403)

    def test_duplicates_endpoint_requires_login(self, tmp_path: Path, monkeypatch):
        from fastapi.testclient import TestClient
        import app.main as main_module
        from app.db import Database
        from app.jobs import JobManager
        from app.runtime_config import reset_runtime_settings_cache

        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("POLICY_AUTH_DISABLED", "0")
        reset_runtime_settings_cache()
        db = Database(str(tmp_path / "m4_api_dup.db"))
        db.initialize()
        monkeypatch.setattr(main_module, "db", db)
        monkeypatch.setattr(main_module, "manager", JobManager(db))
        with TestClient(main_module.app) as client:
            resp = client.get("/api/duplicates")
            assert resp.status_code in (401, 403)

    def test_reconciliation_matches_requires_login(self, tmp_path: Path, monkeypatch):
        from fastapi.testclient import TestClient
        import app.main as main_module
        from app.db import Database
        from app.jobs import JobManager
        from app.runtime_config import reset_runtime_settings_cache

        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("POLICY_AUTH_DISABLED", "0")
        reset_runtime_settings_cache()
        db = Database(str(tmp_path / "m4_api_recon.db"))
        db.initialize()
        monkeypatch.setattr(main_module, "db", db)
        monkeypatch.setattr(main_module, "manager", JobManager(db))
        with TestClient(main_module.app) as client:
            resp = client.get("/api/reconciliation/matches")
            assert resp.status_code in (401, 403)

    def test_archive_findings_requires_login(self, tmp_path: Path, monkeypatch):
        from fastapi.testclient import TestClient
        import app.main as main_module
        from app.db import Database
        from app.jobs import JobManager
        from app.runtime_config import reset_runtime_settings_cache

        monkeypatch.setenv("APP_ENV", "test")
        monkeypatch.setenv("POLICY_AUTH_DISABLED", "0")
        reset_runtime_settings_cache()
        db = Database(str(tmp_path / "m4_api_af.db"))
        db.initialize()
        monkeypatch.setattr(main_module, "db", db)
        monkeypatch.setattr(main_module, "manager", JobManager(db))
        with TestClient(main_module.app) as client:
            resp = client.get("/api/archive/findings")
            assert resp.status_code in (401, 403)
