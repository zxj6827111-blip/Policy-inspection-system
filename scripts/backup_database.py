#!/usr/bin/env python3
"""备份数据库 CLI（M6-7）。

使用 SQLite 在线 backup() API + WAL checkpoint 生成自包含备份，
并写入 SHA256 sidecar。可选清理过期备份。

示例:
    python scripts/backup_database.py --label m6-rehearsal
    python scripts/backup_database.py --label weekly --dest-dir data/backups --prune --retention-days 30
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backup import backup_database, prune_old_backups
from app.db import Database, DB_PATH


def main() -> int:
    parser = argparse.ArgumentParser(
        description="备份 SQLite 数据库（在线 backup API + WAL checkpoint）。",
    )
    parser.add_argument("--label", default="manual", help="备份标签（默认 manual）")
    parser.add_argument("--dest-dir", default="data/backups", help="备份输出目录（默认 data/backups）")
    parser.add_argument("--prune", action="store_true", help="同时清理过期备份")
    parser.add_argument("--retention-days", type=int, default=30, help="保留天数（默认 30）")
    parser.add_argument("--db", default="", help="源数据库路径（默认 DB_PATH）")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else DB_PATH
    if not db_path.exists():
        print(f"数据库不存在: {db_path}", file=sys.stderr)
        return 1

    dest_dir = Path(args.dest_dir)
    if not dest_dir.is_absolute():
        dest_dir = ROOT / dest_dir

    db = Database(db_path)
    try:
        backup_path = backup_database(db, dest_dir, label=args.label)
    except Exception as exc:
        print(f"备份失败: {exc}", file=sys.stderr)
        return 1

    print(f"备份完成: {backup_path}")
    print(f"  SHA256 sidecar: {backup_path.with_name(backup_path.name + '.sha256')}")

    if args.prune:
        deleted = prune_old_backups(dest_dir, retention_days=args.retention_days)
        print(f"  清理过期备份: 删除 {len(deleted)} 个文件")
        for f in deleted:
            print(f"    - {f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
