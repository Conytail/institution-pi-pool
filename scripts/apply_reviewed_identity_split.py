#!/usr/bin/env python3
"""Safely dry-run or apply an explicitly reviewed canonical PI split."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from pi_index.pipeline.reviewed_identity_split import apply_reviewed_identity_split


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="PI Pool SQLite database")
    parser.add_argument("--manifest", required=True, help="Reviewed split JSON manifest")
    parser.add_argument("--report", help="Optional atomic JSON report output")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the split transaction; without this flag the database is read-only",
    )
    parser.add_argument("--run-id", help="Optional audit run ID")
    args = parser.parse_args()
    result = apply_reviewed_identity_split(
        args.db,
        args.manifest,
        report_path=args.report,
        apply=args.apply,
        run_id=args.run_id,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
