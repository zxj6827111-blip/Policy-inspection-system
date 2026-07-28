#!/usr/bin/env python3
"""校验备份 CLI（M6-7）。

以只读方式打开备份，运行 integrity_check / quick_check / foreign_key_check，
并统计关键表行数。退出码 0 表示通过，1 表示失败。

示例:
    python scripts/validate_backup.py --backup-path data/backups/policy_inspector_manual_20260727_120000.db
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backup import validate_backup


def main() -> int:
    parser = argparse.ArgumentParser(
        description="校验备份文件（integrity_check / quick_check / foreign_key_check / 关键表行数）。",
    )
    parser.add_argument("--backup-path", required=True, help="备份文件路径")
    args = parser.parse_args()

    backup_path = Path(args.backup_path)
    if not backup_path.exists():
        print(f"备份文件不存在: {backup_path}", file=sys.stderr)
        return 1

    try:
        result = validate_backup(backup_path)
    except Exception as exc:
        print(f"校验失败: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
