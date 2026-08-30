#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

try:
    from .cell_api import CellAddress, CellOperationResult, RailVoltages, ScanDebugCellAPI, ScanDebugConfig, packet_for_cell
except ImportError:
    from cell_api import CellAddress, CellOperationResult, RailVoltages, ScanDebugCellAPI, ScanDebugConfig, packet_for_cell


@dataclass(frozen=True)
class LevelSpec:
    code: int
    row: int
    target_uA: float
    lower_uA: float
    upper_uA: float


def build_level_specs(
    *,
    row_start: int = 0,
    row_end: int = 31,
    min_uA: float = 0.0,
    max_uA: float = 75.0,
) -> list[LevelSpec]:
    if not 0 <= row_start <= row_end <= 31:
        raise ValueError(f"row range must be within 0..31, got {row_start}..{row_end}")
    if max_uA <= min_uA:
        raise ValueError("max_uA must be greater than min_uA")
    step_uA = (max_uA - min_uA) / 31.0
    half_lsb_uA = step_uA / 2.0
    return [
        LevelSpec(
            code=row,
            row=row,
            target_uA=min_uA + row * step_uA,
            lower_uA=min_uA + row * step_uA - half_lsb_uA,
            upper_uA=min_uA + row * step_uA + half_lsb_uA,
        )
        for row in range(row_start, row_end + 1)
    ]


def _rails_for_sweep(vcc_set_v: tuple[float, ...], vcc_wl_set_v: tuple[float, ...]) -> list[RailVoltages]:
    return [RailVoltages(vcc_set, vcc_wl) for vcc_set in vcc_set_v for vcc_wl in vcc_wl_set_v]


def _inside(value_uA: float | None, spec: LevelSpec) -> bool:
    return value_uA is not None and spec.lower_uA <= value_uA <= spec.upper_uA


def _result_record(result: CellOperationResult) -> dict[str, object]:
    return asdict(result)


def _attempted_rail_prefix(
    api: ScanDebugCellAPI,
    *,
    cell: CellAddress,
    operation: str,
    rails: list[RailVoltages],
) -> int:
    """Return the number of consecutive sweep rails already recorded as successful."""

    manifest = getattr(api, "manifest", None)
    if not isinstance(manifest, Path) or not manifest.exists():
        return 0
    expected_packet = packet_for_cell(cell, 1 if operation == "set" else 0)
    attempted: set[tuple[float, float]] = set()
    with manifest.open(newline="") as handle:
        for row in csv.DictReader(handle):
            try:
                packet = int(str(row.get("packet", "")), 16)
                ok = str(row.get("ok", "")).strip().lower() == "true"
                pair = (round(float(row["vcc_set_V"]), 6), round(float(row["vcc_wl_set_V"]), 6))
            except (KeyError, TypeError, ValueError):
                continue
            if ok and packet == expected_packet:
                attempted.add(pair)

    prefix = 0
    for item in rails:
        pair = (round(item.vcc_set_v, 6), round(item.vcc_wl_set_v, 6))
        if pair not in attempted:
            break
        prefix += 1
    return prefix


def program_cell_level(
    api: ScanDebugCellAPI,
    *,
    spec: LevelSpec,
    col: int,
    confirm_reads: int = 10,
) -> dict[str, object]:
    if confirm_reads < 1:
        raise ValueError("confirm_reads must be positive")
    cell = CellAddress(spec.row, col)
    cell.validate()
    set_rails = _rails_for_sweep(api.config.set_sweep.vcc_set_v, api.config.set_sweep.vcc_wl_set_v)
    reset_rails = _rails_for_sweep(api.config.reset_sweep.vcc_set_v, api.config.reset_sweep.vcc_wl_set_v)
    set_index = _attempted_rail_prefix(api, cell=cell, operation="set", rails=set_rails)
    reset_index = _attempted_rail_prefix(api, cell=cell, operation="reset", rails=reset_rails)
    events: list[dict[str, object]] = []
    current = api._pulse_and_capture(cell, "read", api.config.read_rails, "level_initial_read")
    events.append({"kind": "initial_read", "result": _result_record(current)})

    while True:
        if not current.ok or current.current_uA is None:
            return {
                "cell": asdict(cell),
                "code": spec.code,
                "target_uA": spec.target_uA,
                "lower_uA": spec.lower_uA,
                "upper_uA": spec.upper_uA,
                "qualified": False,
                "reason": "read_failed",
                "final_current_uA": current.current_uA,
                "confirmation_currents_uA": [],
                "events": events,
            }

        if _inside(current.current_uA, spec):
            confirmations = [
                api._pulse_and_capture(cell, "read", api.config.read_rails, "level_confirm_read")
                for _ in range(confirm_reads)
            ]
            confirmation_currents = [item.current_uA for item in confirmations]
            stable = len(confirmations) == confirm_reads and all(
                item.ok and _inside(item.current_uA, spec) for item in confirmations
            )
            events.append(
                {
                    "kind": "qualification",
                    "results": [_result_record(item) for item in confirmations],
                }
            )
            return {
                "cell": asdict(cell),
                "code": spec.code,
                "target_uA": spec.target_uA,
                "lower_uA": spec.lower_uA,
                "upper_uA": spec.upper_uA,
                "qualified": stable,
                "reason": "qualified" if stable else "unstable_confirmation_reads",
                "final_current_uA": confirmation_currents[-1] if confirmation_currents else current.current_uA,
                "confirmation_currents_uA": confirmation_currents,
                "events": events,
            }

        operation = "set" if current.current_uA < spec.lower_uA else "reset"
        rails_list = set_rails if operation == "set" else reset_rails
        rail_index = set_index if operation == "set" else reset_index
        if rail_index >= len(rails_list):
            return {
                "cell": asdict(cell),
                "code": spec.code,
                "target_uA": spec.target_uA,
                "lower_uA": spec.lower_uA,
                "upper_uA": spec.upper_uA,
                "qualified": False,
                "reason": f"{operation}_sweep_exhausted",
                "final_current_uA": current.current_uA,
                "confirmation_currents_uA": [],
                "events": events,
            }
        rails = rails_list[rail_index]
        if operation == "set":
            set_index += 1
        else:
            reset_index += 1
        pulse = api._program_pulse(cell, operation, rails, f"level_{operation}_pulse")
        verify = api._pulse_and_capture(cell, "read", api.config.read_rails, f"level_read_after_{operation}")
        events.append(
            {
                "kind": "program_step",
                "operation": operation,
                "rails": asdict(rails),
                "pulse": _result_record(pulse),
                "verify": _result_record(verify),
            }
        )
        current = verify


def _completed_rows(path: Path, *, retry_failed: bool) -> set[int]:
    if not path.exists():
        return set()
    completed: set[int] = set()
    for line in path.read_text().splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        row = item.get("cell", {}).get("row") if isinstance(item.get("cell"), dict) else None
        if isinstance(row, int) and (item.get("qualified") or not retry_failed):
            completed.add(row)
    return completed


def program_column_levels(
    api: ScanDebugCellAPI,
    *,
    col: int = 0,
    row_start: int = 0,
    row_end: int = 31,
    min_uA: float = 0.0,
    max_uA: float = 75.0,
    confirm_reads: int = 10,
    retry_failed: bool = False,
    max_consecutive_errors: int = 3,
) -> dict[str, object]:
    CellAddress(row_start, col).validate()
    CellAddress(row_end, col).validate()
    specs = build_level_specs(row_start=row_start, row_end=row_end, min_uA=min_uA, max_uA=max_uA)
    result_path = api.config.run_dir / "column_level_programming.jsonl"
    completed = _completed_rows(result_path, retry_failed=retry_failed)
    results: list[dict[str, object]] = []
    consecutive_errors = 0
    for spec in specs:
        if spec.row in completed:
            api._append_progress(
                "program-column-levels",
                f"Skipping recorded row {spec.row}",
                row=spec.row,
                col=col,
                code=spec.code,
                target_uA=spec.target_uA,
            )
            continue
        api._append_progress(
            "program-column-levels",
            f"Programming row {spec.row}, column {col} to code {spec.code}",
            row=spec.row,
            col=col,
            code=spec.code,
            target_uA=spec.target_uA,
            lower_uA=spec.lower_uA,
            upper_uA=spec.upper_uA,
        )
        try:
            result = program_cell_level(api, spec=spec, col=col, confirm_reads=confirm_reads)
            consecutive_errors = 0
        except Exception as exc:
            consecutive_errors += 1
            result = {
                "cell": {"row": spec.row, "col": col},
                "code": spec.code,
                "target_uA": spec.target_uA,
                "lower_uA": spec.lower_uA,
                "upper_uA": spec.upper_uA,
                "qualified": False,
                "reason": "operation_error",
                "error": f"{type(exc).__name__}: {exc}",
                "events": [],
            }
        with result_path.open("a") as handle:
            handle.write(json.dumps(result, sort_keys=True) + "\n")
        results.append(result)
        api._append_progress(
            "program-column-levels",
            f"Row {spec.row} {'qualified' if result.get('qualified') else 'failed; skipping'}",
            row=spec.row,
            col=col,
            code=spec.code,
            target_uA=spec.target_uA,
            qualified=bool(result.get("qualified")),
            reason=result.get("reason", ""),
        )
        if consecutive_errors >= max(1, max_consecutive_errors):
            raise RuntimeError(f"stopping after {consecutive_errors} consecutive infrastructure errors")

    qualified = sum(1 for item in results if item.get("qualified"))
    summary = {
        "operation": "program-column-levels",
        "col": col,
        "row_start": row_start,
        "row_end": row_end,
        "min_uA": min_uA,
        "max_uA": max_uA,
        "step_uA": (max_uA - min_uA) / 31.0,
        "half_lsb_uA": (max_uA - min_uA) / 62.0,
        "confirm_reads": confirm_reads,
        "processed": len(results),
        "qualified": qualified,
        "failed": len(results) - qualified,
        "results_file": str(result_path),
    }
    (api.config.run_dir / "column_level_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Program one array column to 32 five-bit current levels")
    parser.add_argument("--col", type=int, default=0)
    parser.add_argument("--row-start", type=int, default=0)
    parser.add_argument("--row-end", type=int, default=31)
    parser.add_argument("--min-uA", type=float, default=0.0)
    parser.add_argument("--max-uA", type=float, default=75.0)
    parser.add_argument("--confirm-reads", type=int, default=10)
    parser.add_argument("--digital-sample-rate", type=int, default=50_000_000)
    parser.add_argument("--analog-sample-rate", type=int, default=6_250_000)
    parser.add_argument("--disable-persistent-fpga-runtime", action="store_true")
    parser.add_argument("--capture-program-pulses", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--max-consecutive-errors", type=int, default=3)
    parser.add_argument("--run-dir", default=f"api_v1/runs/column_levels_{time.strftime('%Y%m%d_%H%M%S')}")
    args = parser.parse_args()

    config = ScanDebugConfig(
        run_dir=Path(args.run_dir),
        digital_sample_rate=args.digital_sample_rate,
        analog_sample_rate=args.analog_sample_rate,
        persistent_fpga_runtime=not args.disable_persistent_fpga_runtime,
        capture_program_pulses=args.capture_program_pulses,
    )
    api = ScanDebugCellAPI(config)
    with api.hardware_queue("program-column-levels"):
        summary = program_column_levels(
            api,
            col=args.col,
            row_start=args.row_start,
            row_end=args.row_end,
            min_uA=args.min_uA,
            max_uA=args.max_uA,
            confirm_reads=args.confirm_reads,
            retry_failed=args.retry_failed,
            max_consecutive_errors=args.max_consecutive_errors,
        )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
