#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from api_v1.program_column_levels import _append_detailed_result
except ImportError:
    from program_column_levels import _append_detailed_result


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild detailed column CSV, including conductance columns")
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()

    source = args.run_dir / "column_level_programming.jsonl"
    target = args.run_dir / "column_level_detailed.csv"
    temporary = args.run_dir / "column_level_detailed.csv.tmp"
    if temporary.exists():
        temporary.unlink()
    if source.exists():
        for line in source.read_text().splitlines():
            try:
                result = json.loads(line)
            except json.JSONDecodeError:
                continue
            _append_detailed_result(temporary, result)
    if temporary.exists():
        temporary.replace(target)
    print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
