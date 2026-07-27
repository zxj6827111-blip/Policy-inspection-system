#!/usr/bin/env python3
"""M6-8 生产副本备份/恢复全流程演练脚本。

在不触碰真实 DB_PATH 的前提下，对生产副本执行完整的
备份 → 校验 → 恢复 → 再校验 → 行数比对 流程，输出带 PASS/FAIL 的结构化报告。

注意：本脚本仅读取 --source-db（默认 DB_PATH），所有写入操作都发生在
--tmp-dir 下，绝不会覆盖真实数据库。

示例:
    python scripts/rehearsal_backup_restore.py
    python scripts/rehearsal_backup_restore.py --source-db data/policy_inspector.db --tmp-dir tmp/rehearsal
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backup import backup_database, restore_database, validate_backup
from app.db import Database, DB_PATH


def _consistent_copy(source: Path, dest: Path) -> None:
    """使用 SQLite 在线 backup API 创建源库的一致副本（避免 WAL 不一致）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    # 清理可能残留的 -wal/-shm，确保副本干净
    for suffix in ("-wal", "-shm"):
        side = dest.with_name(dest.name + suffix)
        if side.exists():
            side.unlink()
    src = sqlite3.connect(str(source))
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
        dst.commit()
        dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        dst.close()
        src.close()
    # 副本自包含：清理 -wal/-shm
    for suffix in ("-wal", "-shm"):
        side = dest.with_name(dest.name + suffix)
        if side.exists():
            try:
                side.unlink()
            except OSError:
                pass


def _all_table_counts(path: Path) -> dict[str, int]:
    """统计数据库所有用户表的行数。"""
    conn = sqlite3.connect(str(path))
    try:
        tables = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {t: conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0] for t in tables}
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="M6-8 生产副本备份/恢复全流程演练（不触碰真实 DB_PATH）。",
    )
    parser.add_argument("--source-db", default="", help="源数据库路径（默认 DB_PATH，仅读取）")
    parser.add_argument("--tmp-dir", default="tmp/rehearsal", help="临时工作目录（默认 tmp/rehearsal）")
    args = parser.parse_args()

    source_db = Path(args.source_db) if args.source_db else DB_PATH
    tmp_dir = Path(args.tmp_dir)
    if not tmp_dir.is_absolute():
        tmp_dir = ROOT / tmp_dir
    tmp_dir.mkdir(parents=True, exist_ok=True)

    pre_rehearsal = tmp_dir / "pre_rehearsal.db"
    post_restore = tmp_dir / "post_restore.db"
    backup_dir = tmp_dir / "backups"

    report: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "source_db": str(source_db),
        "tmp_dir": str(tmp_dir),
        "steps": [],
        "overall": "PASS",
    }

    def step(name: str, status: str, **detail: Any) -> None:
        report["steps"].append({"name": name, "status": status, **detail})
        if status != "PASS" and report["overall"] == "PASS":
            report["overall"] = "FAIL"

    def finish_and_print() -> int:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["overall"] == "PASS" else 1

    # 步骤 1：拷贝源库到 pre_rehearsal.db（仅读取源库，不修改）
    try:
        if not source_db.exists():
            raise FileNotFoundError(f"源数据库不存在: {source_db}")
        _consistent_copy(source_db, pre_rehearsal)
        step("1_copy_source_to_pre_rehearsal", "PASS", dest=str(pre_rehearsal))
    except Exception as exc:
        step("1_copy_source_to_pre_rehearsal", "FAIL", error=str(exc))
        return finish_and_print()

    # 步骤 2：对 pre_rehearsal 执行备份
    backup_path: Path | None = None
    try:
        pre_db = Database(pre_rehearsal)
        backup_path = backup_database(pre_db, backup_dir, label="rehearsal")
        step("2_backup_pre_rehearsal", "PASS", backup=str(backup_path))
    except Exception as exc:
        step("2_backup_pre_rehearsal", "FAIL", error=str(exc))
        return finish_and_print()

    # 步骤 3：校验备份
    try:
        validation = validate_backup(backup_path)
        step("3_validate_backup", "PASS" if validation["valid"] else "FAIL", **validation)
    except Exception as exc:
        step("3_validate_backup", "FAIL", error=str(exc))
        return finish_and_print()

    # 步骤 4：从备份恢复到 post_restore.db（不触碰 source_db）
    try:
        if post_restore.exists():
            post_restore.unlink()
        restore_result = restore_database(backup_path, post_restore, dry_run=False)
        step("4_restore_to_post_restore", "PASS" if restore_result["restored"] else "FAIL", **restore_result)
    except Exception as exc:
        step("4_restore_to_post_restore", "FAIL", error=str(exc))
        return finish_and_print()

    # 步骤 5：校验 post_restore.db
    try:
        post_validation = validate_backup(post_restore)
        step("5_validate_post_restore", "PASS" if post_validation["valid"] else "FAIL", **post_validation)
    except Exception as exc:
        step("5_validate_post_restore", "FAIL", error=str(exc))
        return finish_and_print()

    # 步骤 6：比对 pre_rehearsal 与 post_restore 的表行数（应一致）
    try:
        pre_counts = _all_table_counts(pre_rehearsal)
        post_counts = _all_table_counts(post_restore)
        diff = {
            t: {"pre": pre_counts.get(t), "post": post_counts.get(t)}
            for t in sorted(set(pre_counts) | set(post_counts))
            if pre_counts.get(t) != post_counts.get(t)
        }
        match = not diff
        step("6_compare_table_counts", "PASS" if match else "FAIL",
             table_count_diff=diff,
             pre_total_tables=len(pre_counts),
             post_total_tables=len(post_counts))
    except Exception as exc:
        step("6_compare_table_counts", "FAIL", error=str(exc))

    # 步骤 7：M3-M5 规则目录完整性校验（post_restore 应能查到所有规则版本）
    try:
        post_db = Database(post_restore)
        with post_db.connect() as conn:
            rule_rows = conn.execute(
                "SELECT rule_id, version FROM rule_versions ORDER BY rule_id"
            ).fetchall()
        rule_codes = {r["rule_id"] for r in rule_rows}
        expected_prefixes = ("META-", "DOC-", "DATE-", "AGENCY-", "ORG-", "PDF-", "ATT-",
                             "ARCH-", "DUP-", "RECON-", "READ-", "MEDIA-", "INTERP-", "RECT-")
        missing_prefixes = [
            p for p in expected_prefixes
            if not any(c.startswith(p) for c in rule_codes)
        ]
        step("7_rule_catalog_integrity", "PASS" if not missing_prefixes else "FAIL",
             rule_count=len(rule_codes),
             missing_prefixes=missing_prefixes)
    except Exception as exc:
        step("7_rule_catalog_integrity", "FAIL", error=str(exc))

    # 步骤 8：M3-M5 规则表查询演练（post_restore 上执行各 finding 表的代表性查询；
    # 表存在即可，行数为 0 也算 PASS —— 表示该规则尚未在副本上跑过）
    try:
        with post_db.connect() as conn:
            m3_count = conn.execute(
                "SELECT COUNT(*) FROM rule_findings WHERE category IN ('pdf_integrity','pdf_compare','attachment')"
            ).fetchone()[0]
            archive_count = conn.execute("SELECT COUNT(*) FROM archive_findings").fetchone()[0]
            dup_count = conn.execute("SELECT COUNT(*) FROM duplicate_pairs").fetchone()[0]
            recon_count = conn.execute("SELECT COUNT(*) FROM reconciliation_matches").fetchone()[0]
            read_count = conn.execute("SELECT COUNT(*) FROM reading_linkage_checks").fetchone()[0]
            media_count = conn.execute("SELECT COUNT(*) FROM media_checks").fetchone()[0]
            interp_count = conn.execute("SELECT COUNT(*) FROM interpretation_checks").fetchone()[0]
            rect_count = conn.execute("SELECT COUNT(*) FROM rectification_tasks").fetchone()[0]
        step("8_m3_m5_table_queries", "PASS",
             rule_findings_m3=m3_count, archive_findings=archive_count,
             duplicate_pairs=dup_count, reconciliation_matches=recon_count,
             reading_linkage_checks=read_count, media_checks=media_count,
             interpretation_checks=interp_count, rectification_tasks=rect_count)
    except Exception as exc:
        step("8_m3_m5_table_queries", "FAIL", error=str(exc))

    # 步骤 9：Excel 导出演练（post_restore 上对最近一个 job 执行 export_job）
    try:
        with post_db.connect() as conn:
            job_row = conn.execute(
                "SELECT id FROM scan_jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if not job_row:
            step("9_excel_export_smoke", "PASS", skipped=True, reason="no scan_jobs in replica")
        else:
            from app.exporter import export_job
            xlsx_path = export_job(post_db, int(job_row["id"]))
            ok = xlsx_path.exists() and xlsx_path.stat().st_size > 0
            step("9_excel_export_smoke", "PASS" if ok else "FAIL",
                 job_id=int(job_row["id"]), output=str(xlsx_path),
                 size_bytes=xlsx_path.stat().st_size if ok else 0)
    except Exception as exc:
        step("9_excel_export_smoke", "FAIL", error=str(exc))

    # 步骤 10：M6 观测性/健康检查端点演练
    # 必须在导入 app.main 前将环境变量切到 test，否则 production fail-closed 会拦截鉴权
    try:
        import os
        os.environ["APP_ENV"] = "test"
        os.environ["POLICY_AUTH_DISABLED"] = "1"
        from app.runtime_config import reset_runtime_settings_cache
        reset_runtime_settings_cache()
        from fastapi.testclient import TestClient
        from app import main as main_module
        # 将副本注入到 app.state（不修改真实 DB_PATH）
        main_module.app.state.db = post_db
        with TestClient(main_module.app) as client:
            health = client.get("/health").json()
            metrics_resp = client.get("/metrics")
            metrics_ok = metrics_resp.status_code == 200
            deep_resp = client.get("/health/deep")
            deep_ok = deep_resp.status_code == 200
            ocr_resp = client.get("/api/ocr-queue?limit=5")
            ocr_ok = ocr_resp.status_code == 200
            settings_resp = client.get("/api/system-settings")
            settings_ok = settings_resp.status_code == 200
            backups_resp = client.get("/api/backups")
            backups_ok = backups_resp.status_code == 200
        all_ok = all([metrics_ok, deep_ok, ocr_ok, settings_ok, backups_ok])
        step("10_m6_endpoints_smoke", "PASS" if all_ok else "FAIL",
             health_status=health.get("status"),
             metrics_ok=metrics_ok, deep_health_ok=deep_ok,
             ocr_queue_ok=ocr_ok, system_settings_ok=settings_ok,
             backups_ok=backups_ok)
    except Exception as exc:
        step("10_m6_endpoints_smoke", "FAIL", error=str(exc))

    return finish_and_print()


if __name__ == "__main__":
    raise SystemExit(main())
