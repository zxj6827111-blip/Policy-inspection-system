#!/usr/bin/env python3
"""恢复数据库 CLI（M6-7）。

先校验备份完整性与 SHA256，再覆盖目标库。支持 --dry-run 仅校验。

示例:
    python scripts/restore_database.py --backup-path data/backups/policy_inspector_manual_20260727_120000.db --dry-run
    python scripts/restore_database.py --backup-path data/backups/policy_inspector_manual_20260727_120000.db
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backup import restore_database
from app.db import DB_PATH


def main() -> int:
    parser = argparse.ArgumentParser(
        description="从备份恢复 SQLite 数据库（含完整性 + SHA256 校验）。",
    )
    parser.add_argument("--backup-path", required=True, help="备份文件路径")
    parser.add_argument("--dest-db", default="", help="恢复目标路径（默认 DB_PATH）")
    parser.add_argument("--dry-run", action="store_true", help="仅校验不覆盖")
    args = parser.parse_args()

    backup_path = Path(args.backup_path)
    dest_db = Path(args.dest_db) if args.dest_db else DB_PATH
    if not backup_path.exists():
        print(f"备份文件不存在: {backup_path}", file=sys.stderr)
        return 1

    try:
        result = restore_database(backup_path, dest_db, dry_run=args.dry_run)
    except Exception as exc:
        print(f"恢复失败: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["restored"] or args.dry_run else 1


if __name__ == "__main__":
    raise SystemExit(main())
