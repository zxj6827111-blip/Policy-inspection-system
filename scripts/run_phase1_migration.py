#!/usr/bin/env python3
"""Run phase-1 migration on a target DB (prefer a copy, not production blindly)."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import Database
from app.knowledge.organizations import seed_organizations
from app.migrations.phase1 import run_phase1_migration


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def table_counts(path: Path) -> dict:
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {t: conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0] for t in tables}
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="Target database path")
    parser.add_argument("--report", default="", help="JSON report output path")
    parser.add_argument("--force-backfill", action="store_true")
    args = parser.parse_args()
    db_path = Path(args.db)
    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 1

    before_sha = sha256(db_path)
    before_counts = table_counts(db_path)

    db = Database(db_path)
    with db.connect() as conn:
        # Ensure base schema objects exist for empty/partial DBs
        conn.executescript("PRAGMA foreign_keys=ON;")
        result = run_phase1_migration(conn, force_backfill=args.force_backfill)
        org_stats = seed_organizations(conn)

    after_sha = sha256(db_path)
    after_counts = table_counts(db_path)
    report = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "db": str(db_path.resolve()),
        "before_sha256": before_sha,
        "after_sha256": after_sha,
        "before_counts": before_counts,
        "after_counts": after_counts,
        "migration": result,
        "organizations": org_stats,
    }
    out = Path(args.report) if args.report else ROOT / "tmp" / "phase1_migration_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
