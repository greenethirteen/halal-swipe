#!/usr/bin/env python3
"""Import one or more WhatsApp export zips from the command line.

Usage:
  python scripts/import_zip.py "/path/to/WhatsApp Chat.zip"
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.database import init_db  # noqa: E402
from app.parser import import_zip_to_db  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python scripts/import_zip.py /path/to/export.zip [...]")
        return 1
    init_db()
    for arg in sys.argv[1:]:
        result = import_zip_to_db(arg)
        print(
            f"{Path(arg).name}: {result['inserted']} new, {result['duplicates']} duplicates, "
            f"{result['skipped']} skipped from {result['total_candidates']} candidates"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
