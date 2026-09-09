#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

try:
    from .cell_api import (
        DEFAULT_READ_CALIBRATION,
        RESET_PROGRAM_VCC_SET_V,
        SET_PROGRAM_VCC_SET_V,
        SET_PROGRAM_VCC_WL_V,
        ScanDebugCellAPI,
        ScanDebugConfig,
        SweepConfig,
    )
    from .program_column_levels import (
        READ_CONDUCTANCE_VOLTAGE_V,
        LevelSpec,
        inclusive_voltage_sweep,
        program_cell_level,
    )
except ImportError:
    from cell_api import (
        DEFAULT_READ_CALIBRATION,
        RESET_PROGRAM_VCC_SET_V,
        SET_PROGRAM_VCC_SET_V,
        SET_PROGRAM_VCC_WL_V,
        ScanDebugCellAPI,
        ScanDebugConfig,
        SweepConfig,
    )
    from program_column_levels import (
        READ_CONDUCTANCE_VOLTAGE_V,
        LevelSpec,
        inclusive_voltage_sweep,
        program_cell_level,
    )


def _completed_rows(path: Path) -> set[int]:
    if not path.exists():
        return set()
    latest: dict[int, dict[str, object]] = {}
    for line in path.read_text().splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        row = item.get("row")
        if not isinstance(row, int) and isinstance(item.get("cell"), dict):
            row = item["cell"].get("row")
        if isinstance(row, int):
            latest[row] = item
    # A completed but electrically unstable row must not be pulsed again when a
    # hardware outage resumes the run. Only infrastructure failures are retried.
    return {
        row
        for row, item in latest.items()
        if item.get("reason") != "operation_error"
    }


def program_column_threshold(
    api: ScanDebugCellAPI,
    *,
    col: int,
    row_start: int = 0,
    row_end: int = 31,
    correction_tolerance_uA: float = 5.0,
    confirm_reads: int = 10,
    max_program_pulses: int = 64,
    row_retries: int = 3,
    max_consecutive_errors: int = 3,
    set_only: bool = False,
    reset_only: bool = False,
) -> dict[str, object]:
    if not 0 <= col <= 31:
        raise ValueError(f"column must be within 0..31, got {col}")
    if not 0 <= row_start <= row_end <= 31:
        raise ValueError(f"row range must be within 0..31, got {row_start}..{row_end}")
    if correction_tolerance_uA <= 0:
        raise ValueError("correction tolerance must be positive")
    if set_only and reset_only:
        raise ValueError("set-only and reset-only modes are mutually exclusive")

    result_path = api.config.run_dir / "column_threshold_programming.jsonl"
    completed = _completed_rows(result_path)
    recovery_path = api.config.run_dir / "infrastructure_recovery.jsonl"
    results: list[dict[str, object]] = []
    consecutive_errors = 0

    for row in range(row_start, row_end + 1):
        if row in completed:
            api._append_progress(
                "program-column-threshold",
                f"Skipping previously completed row {row}",
                row=row,
                col=col,
                threshold_uA=api.config.set_sweep.threshold_uA,
            )
            continue
        api._append_progress(
            "program-column-threshold",
            f"Programming row {row}, column {col} to the {'Reset' if reset_only else 'Set'} threshold",
            row=row,
            col=col,
            threshold_uA=api.config.set_sweep.threshold_uA,
        )
        result: dict[str, object] | None = None
        attempt = 0
        while result is None:
            attempt += 1
            try:
                target = api.config.set_sweep.threshold_uA
                result = program_cell_level(
                    api,
                    spec=LevelSpec(
                        code=31,
                        row=row,
                        target_uA=target,
                        lower_uA=target - correction_tolerance_uA,
                        upper_uA=target + correction_tolerance_uA,
                    ),
                    col=col,
                    confirm_reads=confirm_reads,
                    max_program_pulses=max_program_pulses,
                    qualify_above_only=set_only,
                    qualify_below_only=reset_only,
                )
                result["row"] = row
                result["col"] = col
                result["threshold_uA"] = target
                result["target_hit"] = bool(result.get("qualified"))
                consecutive_errors = 0
                continue
            except Exception as exc:
                event = {
                    "time": time.time(),
                    "row": row,
                    "col": col,
                    "attempt": attempt,
                    "error": f"{type(exc).__name__}: {exc}",
                    "action": "retry_same_row",
                }
                with recovery_path.open("a") as handle:
                    handle.write(json.dumps(event, sort_keys=True) + "\n")
                api._append_progress(
                    "program-column-threshold",
                    f"Row {row} infrastructure recovery attempt {attempt}; pausing on this row",
                    row=row,
                    col=col,
                    error=str(exc),
                )
                # Never skip a cell because Saleae/SSH/FPGA infrastructure is
                # unavailable. Retry in batches, with a longer pause between
                # batches so a USB passthrough can be reattached safely.
                delay = 15.0 if attempt % max(1, row_retries) == 0 else 5.0
                time.sleep(delay)
        assert result is not None

        with result_path.open("a") as handle:
            handle.write(json.dumps(result, sort_keys=True) + "\n")
        results.append(result)
        api._append_progress(
            "program-column-threshold",
            f"Row {row} {'qualified' if result.get('target_hit') else 'did not qualify'}",
            row=row,
            col=col,
            threshold_uA=api.config.set_sweep.threshold_uA,
            target_hit=bool(result.get("target_hit")),
            best_read_uA=result.get("best_read_uA"),
            reason=result.get("reason", ""),
        )

    summary = {
        "operation": "program-column-threshold",
        "col": col,
        "row_start": row_start,
        "row_end": row_end,
        "threshold_uA": api.config.set_sweep.threshold_uA,
        "correction_band_uA": [
            api.config.set_sweep.threshold_uA - correction_tolerance_uA,
            api.config.set_sweep.threshold_uA + correction_tolerance_uA,
        ],
        "confirm_reads": confirm_reads,
        "mode": (
            "set_only_minimum_threshold"
            if set_only
            else "reset_only_maximum_threshold"
            if reset_only
            else "bidirectional_exact_band"
        ),
        "processed": len(results),
        "qualified": sum(1 for item in results if item.get("target_hit")),
        "failed": sum(1 for item in results if not item.get("target_hit")),
        "results_file": str(result_path),
    }
    (api.config.run_dir / "column_threshold_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Program one column to a current/conductance threshold")
    parser.add_argument("--col", type=int, required=True)
    parser.add_argument("--row-start", type=int, default=0)
    parser.add_argument("--row-end", type=int, default=31)
    parser.add_argument("--threshold-uA", type=float, default=70.0)
    parser.add_argument("--threshold-uS", type=float, help="target conductance in uS at the configured read voltage")
    parser.add_argument(
        "--read-calibration",
        default=str(DEFAULT_READ_CALIBRATION),
        help="A12-A13 read-offset profile; pass an empty value only for raw diagnostics",
    )
    parser.add_argument("--correction-tolerance-uA", type=float, default=5.0)
    parser.add_argument("--correction-tolerance-uS", type=float, help="conductance tolerance on each side of the target")
    parser.add_argument("--set-vcc-set", type=float, default=SET_PROGRAM_VCC_SET_V)
    reset_vcc_group = parser.add_mutually_exclusive_group()
    reset_vcc_group.add_argument("--reset-vcc-set", type=float)
    reset_vcc_group.add_argument(
        "--reset-vcc-set-range",
        type=float,
        nargs=3,
        metavar=("START", "STOP", "STEP"),
        help="outer RESET Vcc_set sweep; the complete Vcc_wl_set sweep runs at each value",
    )
    reset_vcc_group.add_argument(
        "--reset-vcc-set-values",
        type=float,
        nargs="+",
        metavar="V",
        help="explicit outer RESET Vcc_set values in execution order",
    )
    parser.add_argument("--confirm-reads", type=int, default=10)
    parser.add_argument("--max-program-pulses", type=int, default=64)
    parser.add_argument("--row-retries", type=int, default=3)
    parser.add_argument("--max-consecutive-errors", type=int, default=3)
    direction_group = parser.add_mutually_exclusive_group()
    direction_group.add_argument(
        "--set-only",
        action="store_true",
        help="never apply reset corrections; cells already at or above the threshold receive only stability reads",
    )
    direction_group.add_argument(
        "--reset-only",
        action="store_true",
        help="never apply set corrections; cells already at or below the threshold receive only stability reads",
    )
    parser.add_argument("--run-dir", default=f"api_v1/runs/column_threshold_{time.strftime('%Y%m%d_%H%M%S')}")
    args = parser.parse_args()

    if args.confirm_reads < 1:
        parser.error("--confirm-reads must be positive")
    threshold_uA = (
        args.threshold_uS * READ_CONDUCTANCE_VOLTAGE_V
        if args.threshold_uS is not None
        else args.threshold_uA
    )
    correction_tolerance_uA = (
        args.correction_tolerance_uS * READ_CONDUCTANCE_VOLTAGE_V
        if args.correction_tolerance_uS is not None
        else args.correction_tolerance_uA
    )

    set_sweep = SweepConfig.from_ranges(
        vcc_set_v=(args.set_vcc_set,),
        vcc_wl_set_v=SET_PROGRAM_VCC_WL_V,
        threshold_uA=threshold_uA,
        direction="above",
        confirm_reads=args.confirm_reads,
    )
    config = ScanDebugConfig(
        run_dir=Path(args.run_dir),
        set_sweep=set_sweep,
        read_calibration_path=Path(args.read_calibration) if args.read_calibration else None,
        read_feedback_attempts=3,
    )
    reset_vcc_set_values = (
        tuple(args.reset_vcc_set_values)
        if args.reset_vcc_set_values is not None
        else inclusive_voltage_sweep(*args.reset_vcc_set_range)
        if args.reset_vcc_set_range is not None
        else (args.reset_vcc_set if args.reset_vcc_set is not None else RESET_PROGRAM_VCC_SET_V,)
    )
    config.reset_sweep = SweepConfig.from_ranges(
        vcc_set_v=reset_vcc_set_values,
        vcc_wl_set_v=config.reset_sweep.vcc_wl_set_v,
        threshold_uA=config.reset_sweep.threshold_uA,
        direction=config.reset_sweep.direction,
        confirm_reads=config.reset_sweep.confirm_reads,
        stop_on_threshold=config.reset_sweep.stop_on_threshold,
    )
    api = ScanDebugCellAPI(config)
    with api.hardware_queue("program-column-threshold"):
        summary = program_column_threshold(
            api,
            col=args.col,
            row_start=args.row_start,
            row_end=args.row_end,
            correction_tolerance_uA=correction_tolerance_uA,
            confirm_reads=args.confirm_reads,
            max_program_pulses=args.max_program_pulses,
            row_retries=args.row_retries,
            max_consecutive_errors=args.max_consecutive_errors,
            set_only=args.set_only,
            reset_only=args.reset_only,
        )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
