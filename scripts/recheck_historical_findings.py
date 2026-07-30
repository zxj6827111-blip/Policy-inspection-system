from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.historical_recheck import recheck_jobs, write_recheck_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only M1 recheck of stored documents with old/new finding comparison files."
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("data/policy_inspector.db"),
    )
    parser.add_argument(
        "--job-id",
        type=int,
        action="append",
        required=True,
        dest="job_ids",
    )
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir or Path("data/reports") / (
        "historical-recheck-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    )
    result = recheck_jobs(args.database, args.job_ids)
    written = write_recheck_report(result, output_dir)
    print(json.dumps(result["summary"]["totals"], ensure_ascii=False, sort_keys=True))
    print(f"report={written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
