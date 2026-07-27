#!/usr/bin/env python3
"""One-shot admin bootstrap. Password MUST come from env or interactive prompt — never hardcoded."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.auth.service import AuthService
from app.db import Database


def main() -> int:
    parser = argparse.ArgumentParser(description="Initialize first admin user (no default password).")
    parser.add_argument("--db", default=os.environ.get("POLICY_DB_PATH", ""), help="SQLite DB path")
    parser.add_argument("--username", default=os.environ.get("POLICY_ADMIN_USERNAME", "admin"))
    parser.add_argument(
        "--password-env",
        default="POLICY_ADMIN_PASSWORD",
        help="Environment variable holding the admin password",
    )
    parser.add_argument("--display-name", default="Administrator")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else (ROOT / "data" / "policy_inspector.db")
    password = os.environ.get(args.password_env, "").strip()
    if not password:
        password = getpass.getpass("Admin password (min 8 chars): ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Passwords do not match", file=sys.stderr)
            return 2
    if len(password) < 8:
        print("Password too short", file=sys.stderr)
        return 2

    db = Database(db_path)
    db.initialize()
    auth = AuthService(db)
    try:
        user_id = auth.create_user(
            args.username,
            password,
            ["admin"],
            display_name=args.display_name,
            created_by="bootstrap_cli",
        )
    except ValueError as exc:
        print(f"Failed: {exc}", file=sys.stderr)
        return 1
    print(f"Created admin user id={user_id} username={args.username} db={db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
