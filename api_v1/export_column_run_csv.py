#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path


def _row_number(item: dict[str, object]) -> int | None:
    row = item.get("row")
    if isinstance(row, int):
        return row
    cell = item.get("cell")
    if isinstance(cell, dict) and isinstance(cell.get("row"), int):
        return int(cell["row"])
    return None


def _latest_results(path: Path) -> dict[int, dict[str, object]]:
    latest: dict[int, dict[str, object]] = {}
    if not path.exists():
        return latest
    for line in path.read_text().splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        row = _row_number(item)
        if row is not None:
            latest[row] = item
    return latest


def export(run_dir: Path, column: int) -> tuple[Path, Path]:
    manifest_path = run_dir / "manifest.csv"
    result_path = run_dir / "column_threshold_programming.jsonl"
    if not manifest_path.exists():
        raise FileNotFoundError(manifest_path)

    results = _latest_results(result_path)
    with manifest_path.open(newline="") as handle:
        source = [row for row in csv.DictReader(handle) if row.get("cell", "").endswith(f"_{column}")]

    event_count: dict[int, int] = defaultdict(int)
    pulse_count: dict[int, int] = defaultdict(int)
    read_count: dict[int, int] = defaultdict(int)
    qualification_count: dict[int, int] = defaultdict(int)
    last_read: dict[int, float | None] = defaultdict(lambda: None)
    active_pulse: dict[int, int | None] = defaultdict(lambda: None)
    detailed: list[dict[str, object]] = []

    for item in source:
        row_text, col_text = item["cell"].split("_", 1)
        row = int(row_text)
        col = int(col_text)
        event_count[row] += 1
        operation = item.get("operation", "")
        stage = item.get("stage", "")
        is_pulse = operation in {"set", "reset"} or stage.endswith("_pulse")
        is_read = operation == "read"
        if is_pulse:
            pulse_count[row] += 1
            active_pulse[row] = pulse_count[row]
        if is_read:
            read_count[row] += 1
        is_qualification = "confirm_read" in stage
        if is_qualification:
            qualification_count[row] += 1

        current_text = item.get("la_set_window_mean_uA", "")
        current = float(current_text) if current_text not in {"", None} else None
        previous_current = last_read[row]
        response_delta = None
        if is_read and active_pulse[row] is not None and current is not None and previous_current is not None:
            response_delta = current - previous_current

        result = results.get(row, {})
        lower = result.get("lower_uA", 65.0)
        upper = result.get("upper_uA", 75.0)
        target = result.get("target_uA", result.get("threshold_uA", 70.0))
        detailed.append(
            {
                "manifest_index": item.get("index", ""),
                "row": row,
                "column": col,
                "cell": f"({row},{col})",
                "cell_event_number": event_count[row],
                "stage": stage,
                "event_type": "program_pulse" if is_pulse else "qualification_read" if is_qualification else "read",
                "operation": operation,
                "pulse_number": pulse_count[row] if is_pulse else "",
                "associated_pulse_number": active_pulse[row] if is_read and active_pulse[row] is not None else "",
                "read_number": read_count[row] if is_read else "",
                "qualification_read_number": qualification_count[row] if is_qualification else "",
                "current_uA": current if current is not None else "",
                "prior_read_current_uA": previous_current if previous_current is not None else "",
                "response_delta_uA": response_delta if response_delta is not None else "",
                "vcc_set_V": item.get("vcc_set_V", ""),
                "vcc_wl_set_V": item.get("vcc_wl_set_V", ""),
                "packet": item.get("packet", ""),
                "decoded_packet": item.get("decoded_packet", ""),
                "bits_lsb_first": item.get("bits_lsb_first", ""),
                "target_uA": target,
                "lower_limit_uA": lower,
                "upper_limit_uA": upper,
                "within_target_band": lower <= current <= upper if current is not None else "",
                "cell_final_current_uA": result.get("final_current_uA", result.get("best_read_uA", "")),
                "cell_program_pulses": result.get("program_pulses", ""),
                "cell_qualified": result.get("qualified", result.get("target_hit", "")),
                "cell_final_reason": result.get("reason", "qualified" if result.get("target_hit") else ""),
                "capture_ok": item.get("ok", ""),
                "capture_error": item.get("error", ""),
                "kind": item.get("kind", ""),
                "bitstream": item.get("bitstream", ""),
                "local_output_dir": item.get("local_output_dir", ""),
            }
        )
        if is_read and current is not None:
            last_read[row] = current
        if is_qualification:
            active_pulse[row] = None

    detail_path = run_dir / f"column_{column}_all_pulse_read_events.csv"
    with detail_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(detailed[0]))
        writer.writeheader()
        writer.writerows(detailed)

    summary_rows: list[dict[str, object]] = []
    for row in range(32):
        result = results.get(row, {})
        currents = [float(x["current_uA"]) for x in detailed if x["row"] == row and x["current_uA"] != ""]
        summary_rows.append(
            {
                "row": row,
                "column": column,
                "cell": f"({row},{column})",
                "total_events": event_count[row],
                "program_pulses": pulse_count[row],
                "reads": read_count[row],
                "qualification_reads": qualification_count[row],
                "first_read_uA": currents[0] if currents else "",
                "last_read_uA": currents[-1] if currents else "",
                "minimum_read_uA": min(currents) if currents else "",
                "maximum_read_uA": max(currents) if currents else "",
                "target_uA": result.get("target_uA", result.get("threshold_uA", 70.0)),
                "lower_limit_uA": result.get("lower_uA", 65.0),
                "upper_limit_uA": result.get("upper_uA", 75.0),
                "qualified": result.get("qualified", result.get("target_hit", "")),
                "final_reason": result.get("reason", "qualified" if result.get("target_hit") else ""),
            }
        )
    summary_path = run_dir / f"column_{column}_cell_summary.csv"
    with summary_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(summary_rows[0]))
        writer.writeheader()
        writer.writerows(summary_rows)
    return detail_path, summary_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Export a column programming run as detailed CSV files")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--column", type=int, required=True)
    args = parser.parse_args()
    detail, summary = export(args.run_dir, args.column)
    print(detail)
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
