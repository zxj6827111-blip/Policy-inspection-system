from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.historical_recheck import apply_recheck_result


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish rechecked findings with backup and lineage")
    parser.add_argument("--database", type=Path, default=Path("data/policy_inspector.db"))
    parser.add_argument("--job-id", type=int, action="append", required=True, dest="job_ids")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    backup = args.backup or Path("data") / (
        "policy_inspector.before-historical-recheck-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".db"
    )
    result = apply_recheck_result(args.database, args.job_ids, backup)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
