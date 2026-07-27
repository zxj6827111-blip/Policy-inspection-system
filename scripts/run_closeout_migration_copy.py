#!/usr/bin/env python3
"""Run phase1 + closeout migrations on a DB copy and emit counts report."""

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
from app.migrations.closeout import run_closeout_migration
from app.migrations.phase1 import run_phase1_migration


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def counts(path: Path) -> dict:
    conn = sqlite3.connect(path)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        out = {}
        for t in tables:
            try:
                out[t] = conn.execute(f"SELECT COUNT(*) FROM [{t}]").fetchone()[0]
            except sqlite3.Error as exc:
                out[t] = f"ERR:{exc}"
        return out
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=str(ROOT / "data" / "phase1_migration_copy.db"))
    parser.add_argument("--target", default=str(ROOT / "data" / "phase1_closeout_copy.db"))
    parser.add_argument("--report", default=str(ROOT / "tmp" / "phase1_closeout_migration_report.json"))
    args = parser.parse_args()
    source = Path(args.source)
    target = Path(args.target)
    if not source.exists():
        # fall back: copy from production path WITHOUT modifying it
        prod = ROOT / "data" / "policy_inspector.db"
        if not prod.exists():
            print("no source db", file=sys.stderr)
            return 1
        source = prod
    if target.exists():
        target.unlink()
    shutil.copy2(source, target)
    before = counts(target)
    before_sha = sha256(target)
    db = Database(target)
    with db.connect() as conn:
        r1 = run_phase1_migration(conn, force_backfill=True)
        r2 = run_closeout_migration(conn, force=True)
        org = seed_organizations(conn)
        # idempotent second pass
        r1b = run_phase1_migration(conn)
        r2b = run_closeout_migration(conn)
    after = counts(target)
    report = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "source": str(source.resolve()),
        "target": str(target.resolve()),
        "before_sha256": before_sha,
        "after_sha256": sha256(target),
        "before_counts": before,
        "after_counts": after,
        "phase1": r1,
        "closeout": r2,
        "phase1_rerun": r1b,
        "closeout_rerun": r2b,
        "organizations": org,
        "production_db_untouched_note": "This script never writes data/policy_inspector.db",
    }
    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("target", "before_sha256", "after_sha256", "after_counts")}, ensure_ascii=False, indent=2)[:3000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
