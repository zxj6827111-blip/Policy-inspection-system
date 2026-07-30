from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.putuo_body import clean_putuo_body_text, is_polluted_putuo_body


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect or repair legacy Putuo policy bodies that include full-page navigation text."
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("data/policy_inspector.db"),
        help="SQLite database path (default: data/policy_inspector.db)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write cleaned body text after creating a timestamped database backup.",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=5,
        help="Number of changed examples to print (default: 5).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    database = args.database.resolve()
    if not database.is_file():
        raise SystemExit(f"Database not found: {database}")

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """SELECT id,title,body_text FROM policy_documents
            WHERE district='普陀区' AND body_text IS NOT NULL AND body_text<>''"""
        ).fetchall()
        changes: list[tuple[str, int]] = []
        samples: list[tuple[int, str, int, int]] = []
        polluted = 0
        for row in rows:
            original = row["body_text"] or ""
            if not is_polluted_putuo_body(original):
                continue
            polluted += 1
            cleaned = clean_putuo_body_text(original, row["title"] or "")
            if cleaned == original:
                continue
            changes.append((cleaned, int(row["id"])))
            if len(samples) < max(0, args.sample_size):
                samples.append((int(row["id"]), row["title"] or "", len(original), len(cleaned)))

        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"[{mode}] database={database}")
        print(f"putuo_documents={len(rows)} polluted={polluted} repairable={len(changes)}")
        for document_id, title, before_len, after_len in samples:
            print(f"sample id={document_id} before={before_len} after={after_len} title={title}")

        if not args.apply:
            print("No database rows were changed. Re-run with --apply to create a backup and update repairable rows.")
            return 0

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = database.with_name(f"{database.stem}.before-putuo-body-repair-{timestamp}{database.suffix}")
        shutil.copy2(database, backup)
        try:
            connection.executemany(
                "UPDATE policy_documents SET body_text=? WHERE id=?",
                changes,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        print(f"backup={backup}")
        print(f"updated={len(changes)}")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
