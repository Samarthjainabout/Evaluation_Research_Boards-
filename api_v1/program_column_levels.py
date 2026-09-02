#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

try:
    from .cell_api import CellAddress, CellOperationResult, RailVoltages, ScanDebugCellAPI, ScanDebugConfig, SweepConfig, packet_for_cell
except ImportError:
    from cell_api import CellAddress, CellOperationResult, RailVoltages, ScanDebugCellAPI, ScanDebugConfig, SweepConfig, packet_for_cell


@dataclass(frozen=True)
class LevelSpec:
    code: int
    row: int
    target_uA: float
    lower_uA: float
    upper_uA: float

    @property
    def target_conductance_uS(self) -> float:
        return self.target_uA / READ_CONDUCTANCE_VOLTAGE_V

    @property
    def lower_conductance_uS(self) -> float:
        return self.lower_uA / READ_CONDUCTANCE_VOLTAGE_V

    @property
    def upper_conductance_uS(self) -> float:
        return self.upper_uA / READ_CONDUCTANCE_VOLTAGE_V


DETAILED_CSV_FIELDS = [
    "row",
    "col",
    "code",
    "target_uA",
    "target_conductance_uS",
    "lower_uA",
    "lower_conductance_uS",
    "upper_uA",
    "upper_conductance_uS",
    "qualified",
    "final_reason",
    "final_current_uA",
    "final_conductance_uS",
    "program_pulses",
    "event_index",
    "event_kind",
    "measurement_role",
    "measurement_index",
    "operation",
    "rail_selection",
    "vcc_set_V",
    "vcc_wl_set_V",
    "current_uA",
    "read_voltage_V",
    "conductance_uS",
    "packet",
    "decoded_packet",
    "ok",
    "local_output_dir",
    "error",
    "bracket_after_json",
]

READ_CONDUCTANCE_VOLTAGE_V = 0.5


@dataclass
class DirectionalWlBracket:
    """Advance nested Vcc_set/WL rails, then bisect WL after an overshoot.

    Rails may contain one or more contiguous Vcc_set blocks.  Each block must
    contain a strictly increasing WL sweep.  Bracketing never crosses a
    Vcc_set boundary: when the next outer Vcc_set value begins, its WL bracket
    starts again from the weakest value.
    """

    rails: list[RailVoltages]
    next_index: int = 0
    weak_code: int | None = None
    strong_code: int | None = None
    active_vcc_set: float | None = None

    def __post_init__(self) -> None:
        if not self.rails:
            raise ValueError("programming sweep must contain at least one rail")
        seen_vcc: set[float] = set()
        previous_vcc: float | None = None
        previous_code: int | None = None
        for item in self.rails:
            vcc = round(item.vcc_set_v, 9)
            code = self._wl_code(item.vcc_wl_set_v)
            if previous_vcc is None or vcc != previous_vcc:
                if vcc in seen_vcc:
                    raise ValueError("each Vcc_set sweep block must be contiguous")
                seen_vcc.add(vcc)
                previous_vcc = vcc
                previous_code = None
            if previous_code is not None and code <= previous_code:
                raise ValueError("Vcc_wl_set sweep must be strictly increasing within each Vcc_set block")
            previous_code = code
        if not 0 <= self.next_index <= len(self.rails):
            raise ValueError("initial sweep index is outside the rail list")
        if self.next_index > 0 and self.weak_code is None:
            previous = self.rails[self.next_index - 1]
            self.active_vcc_set = previous.vcc_set_v
            self.weak_code = self._wl_code(previous.vcc_wl_set_v)

    @staticmethod
    def _wl_code(voltage: float) -> int:
        return round(voltage * 65535 / 5.0)

    @staticmethod
    def _wl_voltage(code: int) -> float:
        return code * 5.0 / 65535

    def next_rail(self) -> tuple[RailVoltages, str] | None:
        if self.weak_code is not None and self.strong_code is not None:
            if self.strong_code - self.weak_code <= 1:
                return None
            midpoint_code = (self.weak_code + self.strong_code) // 2
            return RailVoltages(float(self.active_vcc_set), self._wl_voltage(midpoint_code)), "bracket_midpoint"
        if self.strong_code is not None:
            # The weakest workbook voltage already crossed the target.  Do not
            # increase it; reuse only that weakest permitted correction.
            weakest = next(
                item for item in self.rails
                if abs(item.vcc_set_v - float(self.active_vcc_set)) <= 1e-9
            )
            return weakest, "weakest_correction"
        if self.next_index >= len(self.rails):
            return None
        rail = self.rails[self.next_index]
        self.next_index += 1
        if self.active_vcc_set is None or abs(rail.vcc_set_v - self.active_vcc_set) > 1e-9:
            self.active_vcc_set = rail.vcc_set_v
            self.weak_code = None
            self.strong_code = None
        return rail, "projected_sweep"

    def observe(self, rail: RailVoltages, *, insufficient: bool) -> None:
        if self.active_vcc_set is None or abs(rail.vcc_set_v - self.active_vcc_set) > 1e-9:
            self.active_vcc_set = rail.vcc_set_v
            self.weak_code = None
            self.strong_code = None
        code = self._wl_code(rail.vcc_wl_set_v)
        if insufficient:
            self.weak_code = code if self.weak_code is None else max(self.weak_code, code)
        else:
            self.strong_code = code if self.strong_code is None else min(self.strong_code, code)

    def snapshot(self) -> dict[str, float | int | None]:
        return {
            "next_projected_index": self.next_index,
            "active_vcc_set_V": self.active_vcc_set,
            "weak_wl_V": self._wl_voltage(self.weak_code) if self.weak_code is not None else None,
            "strong_wl_V": self._wl_voltage(self.strong_code) if self.strong_code is not None else None,
        }


def build_level_specs(
    *,
    row_start: int = 0,
    row_end: int = 31,
    min_uS: float | None = None,
    max_uS: float | None = None,
    min_uA: float | None = None,
    max_uA: float | None = None,
    row0_code0: bool = False,
) -> list[LevelSpec]:
    if not 0 <= row_start <= row_end <= 31:
        raise ValueError(f"row range must be within 0..31, got {row_start}..{row_end}")
    if min_uS is not None and min_uA is not None:
        raise ValueError("use either min_uS or legacy min_uA, not both")
    if max_uS is not None and max_uA is not None:
        raise ValueError("use either max_uS or legacy max_uA, not both")
    resolved_min_uS = min_uS if min_uS is not None else (min_uA / READ_CONDUCTANCE_VOLTAGE_V if min_uA is not None else 0.0)
    resolved_max_uS = max_uS if max_uS is not None else (max_uA / READ_CONDUCTANCE_VOLTAGE_V if max_uA is not None else 150.0)
    if resolved_max_uS <= resolved_min_uS:
        raise ValueError("max_uS must be greater than min_uS")
    min_uA = resolved_min_uS * READ_CONDUCTANCE_VOLTAGE_V
    max_uA = resolved_max_uS * READ_CONDUCTANCE_VOLTAGE_V
    step_uA = (max_uA - min_uA) / 31.0
    half_lsb_uA = step_uA / 2.0
    specs: list[LevelSpec] = []
    rows = range(row_start, row_end + 1) if row0_code0 else range(row_end, row_start - 1, -1)
    for row in rows:
        code = row if row0_code0 else 31 - row
        target_uA = min_uA + code * step_uA
        specs.append(
            LevelSpec(
                code=code,
                row=row,
                target_uA=target_uA,
                lower_uA=target_uA - half_lsb_uA,
                upper_uA=target_uA + half_lsb_uA,
            )
        )
    return specs


def _rails_for_sweep(vcc_set_v: tuple[float, ...], vcc_wl_set_v: tuple[float, ...]) -> list[RailVoltages]:
    return [RailVoltages(vcc_set, vcc_wl) for vcc_set in vcc_set_v for vcc_wl in vcc_wl_set_v]


def extend_wl_sweep_to_max(values: tuple[float, ...], maximum_v: float) -> tuple[float, ...]:
    """Preserve the projected reset sweep and extend it in 60 mV steps."""

    if not values:
        raise ValueError("Vcc_wl_set sweep must contain at least one value")
    if maximum_v < values[-1] - 1e-9:
        raise ValueError(
            f"requested Vcc_wl_set maximum {maximum_v} V is below the existing "
            f"{values[-1]} V maximum"
        )
    extended = list(values)
    while extended[-1] + 0.06 < maximum_v - 1e-9:
        extended.append(round(extended[-1] + 0.06, 2))
    if maximum_v > extended[-1] + 1e-9:
        extended.append(round(maximum_v, 3))
    return tuple(extended)


def inclusive_voltage_sweep(start_v: float, stop_v: float, step_v: float) -> tuple[float, ...]:
    """Build an inclusive ascending voltage sweep without float drift."""

    if step_v <= 0:
        raise ValueError("voltage sweep step must be positive")
    if stop_v < start_v:
        raise ValueError("voltage sweep stop must be greater than or equal to start")
    values: list[float] = []
    index = 0
    while start_v + index * step_v <= stop_v + 1e-9:
        values.append(round(start_v + index * step_v, 6))
        index += 1
    if not values or values[-1] < stop_v - 1e-9:
        values.append(round(stop_v, 6))
    return tuple(values)


def _inside(value_uA: float | None, spec: LevelSpec) -> bool:
    return value_uA is not None and spec.lower_uA <= value_uA <= spec.upper_uA


def _result_record(result: CellOperationResult) -> dict[str, object]:
    return asdict(result)


def _conductance_uS(current_uA: object) -> float | str:
    try:
        return float(current_uA) / READ_CONDUCTANCE_VOLTAGE_V
    except (TypeError, ValueError):
        return ""


def _spec_fields(spec: LevelSpec) -> dict[str, float]:
    return {
        "target_uA": spec.target_uA,
        "target_conductance_uS": spec.target_conductance_uS,
        "lower_uA": spec.lower_uA,
        "lower_conductance_uS": spec.lower_conductance_uS,
        "upper_uA": spec.upper_uA,
        "upper_conductance_uS": spec.upper_conductance_uS,
    }


def _append_detailed_result(path: Path, result: dict[str, object]) -> None:
    """Append one flattened CSV row for every pulse and read in a cell result."""

    cell = result.get("cell") if isinstance(result.get("cell"), dict) else {}
    common = {
        "row": cell.get("row", ""),
        "col": cell.get("col", ""),
        "code": result.get("code", ""),
        "target_uA": result.get("target_uA", ""),
        "target_conductance_uS": _conductance_uS(result.get("target_uA")),
        "lower_uA": result.get("lower_uA", ""),
        "lower_conductance_uS": _conductance_uS(result.get("lower_uA")),
        "upper_uA": result.get("upper_uA", ""),
        "upper_conductance_uS": _conductance_uS(result.get("upper_uA")),
        "qualified": result.get("qualified", False),
        "final_reason": result.get("reason", ""),
        "final_current_uA": result.get("final_current_uA", ""),
        "final_conductance_uS": _conductance_uS(result.get("final_current_uA")),
        "program_pulses": result.get("program_pulses", ""),
    }
    rows: list[dict[str, object]] = []

    def append_measurement(
        event_index: int,
        event_kind: str,
        role: str,
        measurement_index: int,
        measurement: object,
        *,
        rail_selection: object = "",
        bracket_after: object = "",
    ) -> None:
        item = measurement if isinstance(measurement, dict) else {}
        rails = item.get("rails") if isinstance(item.get("rails"), dict) else {}
        rows.append(
            {
                **common,
                "event_index": event_index,
                "event_kind": event_kind,
                "measurement_role": role,
                "measurement_index": measurement_index,
                "operation": item.get("operation", ""),
                "rail_selection": rail_selection,
                "vcc_set_V": rails.get("vcc_set_v", ""),
                "vcc_wl_set_V": rails.get("vcc_wl_set_v", ""),
                "current_uA": item.get("current_uA", ""),
                "read_voltage_V": READ_CONDUCTANCE_VOLTAGE_V if item.get("operation") == "read" else "",
                "conductance_uS": _conductance_uS(item.get("current_uA")),
                "packet": item.get("packet", ""),
                "decoded_packet": item.get("decoded_packet", ""),
                "ok": item.get("ok", ""),
                "local_output_dir": item.get("local_output_dir", ""),
                "error": item.get("error", ""),
                "bracket_after_json": (
                    json.dumps(bracket_after, sort_keys=True)
                    if isinstance(bracket_after, dict)
                    else ""
                ),
            }
        )

    events = result.get("events") if isinstance(result.get("events"), list) else []
    for event_index, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        event_kind = str(event.get("kind", ""))
        if event_kind == "initial_read":
            append_measurement(event_index, event_kind, "initial_read", 0, event.get("result"))
        elif event_kind == "program_step":
            selection = event.get("rail_selection", "")
            bracket_after = event.get("bracket_after", "")
            append_measurement(
                event_index,
                event_kind,
                "program_pulse",
                0,
                event.get("pulse"),
                rail_selection=selection,
                bracket_after=bracket_after,
            )
            append_measurement(
                event_index,
                event_kind,
                "verification_read",
                0,
                event.get("verify"),
                rail_selection=selection,
                bracket_after=bracket_after,
            )
        elif event_kind == "qualification":
            measurements = event.get("results") if isinstance(event.get("results"), list) else []
            for measurement_index, measurement in enumerate(measurements, start=1):
                append_measurement(
                    event_index,
                    event_kind,
                    "qualification_read",
                    measurement_index,
                    measurement,
                )

    write_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=DETAILED_CSV_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


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
    max_program_pulses: int = 64,
    qualify_above_only: bool = False,
) -> dict[str, object]:
    if confirm_reads < 1:
        raise ValueError("confirm_reads must be positive")
    if max_program_pulses < 1:
        raise ValueError("max_program_pulses must be positive")
    cell = CellAddress(spec.row, col)
    cell.validate()
    set_rails = _rails_for_sweep(api.config.set_sweep.vcc_set_v, api.config.set_sweep.vcc_wl_set_v)
    reset_rails = _rails_for_sweep(api.config.reset_sweep.vcc_set_v, api.config.reset_sweep.vcc_wl_set_v)
    set_index = _attempted_rail_prefix(api, cell=cell, operation="set", rails=set_rails)
    reset_index = _attempted_rail_prefix(api, cell=cell, operation="reset", rails=reset_rails)
    set_bracket = DirectionalWlBracket(set_rails, next_index=set_index)
    reset_bracket = DirectionalWlBracket(reset_rails, next_index=reset_index)
    program_pulses = 0
    events: list[dict[str, object]] = []
    current = api._pulse_and_capture(cell, "read", api.config.read_rails, "level_initial_read")
    events.append({"kind": "initial_read", "result": _result_record(current)})

    while True:
        if not current.ok or current.current_uA is None:
            return {
                "cell": asdict(cell),
                "code": spec.code,
                **_spec_fields(spec),
                "qualified": False,
                "reason": "read_failed",
                "final_current_uA": current.current_uA,
                "confirmation_currents_uA": [],
                "program_pulses": program_pulses,
                "events": events,
            }

        inside_target = (
            current.current_uA >= spec.target_uA
            if qualify_above_only
            else _inside(current.current_uA, spec)
        )
        if inside_target:
            confirmations = [
                api._pulse_and_capture(cell, "read", api.config.read_rails, "level_confirm_read")
                for _ in range(confirm_reads)
            ]
            confirmation_currents = [item.current_uA for item in confirmations]
            stable = len(confirmations) == confirm_reads and all(
                item.ok
                and item.current_uA is not None
                and (
                    item.current_uA >= spec.target_uA
                    if qualify_above_only
                    else _inside(item.current_uA, spec)
                )
                for item in confirmations
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
                **_spec_fields(spec),
                "qualified": stable,
                "reason": "qualified" if stable else "unstable_confirmation_reads",
                "final_current_uA": confirmation_currents[-1] if confirmation_currents else current.current_uA,
                "confirmation_currents_uA": confirmation_currents,
                "program_pulses": program_pulses,
                "events": events,
            }

        operation = "set" if qualify_above_only or current.current_uA < spec.lower_uA else "reset"
        bracket = set_bracket if operation == "set" else reset_bracket
        if program_pulses >= max_program_pulses:
            return {
                "cell": asdict(cell),
                "code": spec.code,
                **_spec_fields(spec),
                "qualified": False,
                "reason": "program_pulse_limit_reached",
                "final_current_uA": current.current_uA,
                "confirmation_currents_uA": [],
                "program_pulses": program_pulses,
                "events": events,
            }
        candidate = bracket.next_rail()
        if candidate is None:
            return {
                "cell": asdict(cell),
                "code": spec.code,
                **_spec_fields(spec),
                "qualified": False,
                "reason": f"{operation}_bracket_exhausted",
                "final_current_uA": current.current_uA,
                "confirmation_currents_uA": [],
                "program_pulses": program_pulses,
                "events": events,
            }
        rails, selection = candidate
        pulse = api._program_pulse(cell, operation, rails, f"level_{operation}_pulse")
        verify = api._pulse_and_capture(cell, "read", api.config.read_rails, f"level_read_after_{operation}")
        program_pulses += 1
        if verify.current_uA is not None:
            insufficient = (
                verify.current_uA < spec.lower_uA
                if operation == "set"
                else verify.current_uA > spec.upper_uA
            )
            excessive = (
                verify.current_uA > spec.upper_uA
                if operation == "set"
                else verify.current_uA < spec.lower_uA
            )
            if insufficient or excessive:
                bracket.observe(rails, insufficient=insufficient)
        events.append(
            {
                "kind": "program_step",
                "operation": operation,
                "rails": asdict(rails),
                "rail_selection": selection,
                "bracket_after": bracket.snapshot(),
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
    min_uS: float | None = None,
    max_uS: float | None = None,
    min_uA: float | None = None,
    max_uA: float | None = None,
    row0_code0: bool = False,
    confirm_reads: int = 10,
    max_program_pulses: int = 64,
    retry_failed: bool = False,
    max_consecutive_errors: int = 3,
) -> dict[str, object]:
    CellAddress(row_start, col).validate()
    CellAddress(row_end, col).validate()
    specs = build_level_specs(
        row_start=row_start,
        row_end=row_end,
        min_uS=min_uS,
        max_uS=max_uS,
        min_uA=min_uA,
        max_uA=max_uA,
        row0_code0=row0_code0,
    )
    result_path = api.config.run_dir / "column_level_programming.jsonl"
    detailed_csv_path = api.config.run_dir / "column_level_detailed.csv"
    recovery_path = api.config.run_dir / "infrastructure_recovery.jsonl"
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
                target_conductance_uS=spec.target_conductance_uS,
            )
            continue
        api._append_progress(
            "program-column-levels",
            f"Programming row {spec.row}, column {col} to code {spec.code}",
            row=spec.row,
            col=col,
            code=spec.code,
            target_uA=spec.target_uA,
            target_conductance_uS=spec.target_conductance_uS,
            lower_uA=spec.lower_uA,
            lower_conductance_uS=spec.lower_conductance_uS,
            upper_uA=spec.upper_uA,
            upper_conductance_uS=spec.upper_conductance_uS,
        )
        attempt = 0
        while True:
            attempt += 1
            try:
                result = program_cell_level(
                    api,
                    spec=spec,
                    col=col,
                    confirm_reads=confirm_reads,
                    max_program_pulses=max_program_pulses,
                )
                consecutive_errors = 0
                break
            except Exception as exc:
                event = {
                    "time": time.time(),
                    "row": spec.row,
                    "col": col,
                    "code": spec.code,
                    "attempt": attempt,
                    "error": f"{type(exc).__name__}: {exc}",
                    "action": "retry_same_row",
                }
                with recovery_path.open("a") as handle:
                    handle.write(json.dumps(event, sort_keys=True) + "\n")
                api._append_progress(
                    "program-column-levels",
                    f"Row {spec.row} infrastructure recovery attempt {attempt}; pausing on this row",
                    row=spec.row,
                    col=col,
                    code=spec.code,
                    error=str(exc),
                )
                delay = 15.0 if attempt % max(1, max_consecutive_errors) == 0 else 5.0
                time.sleep(delay)
        with result_path.open("a") as handle:
            handle.write(json.dumps(result, sort_keys=True) + "\n")
        _append_detailed_result(detailed_csv_path, result)
        results.append(result)
        api._append_progress(
            "program-column-levels",
            f"Row {spec.row} {'qualified' if result.get('qualified') else 'failed; skipping'}",
            row=spec.row,
            col=col,
            code=spec.code,
            target_uA=spec.target_uA,
            target_conductance_uS=spec.target_conductance_uS,
            qualified=bool(result.get("qualified")),
            reason=result.get("reason", ""),
        )

    qualified = sum(1 for item in results if item.get("qualified"))
    resolved_min_uS = specs[0].target_conductance_uS
    resolved_max_uS = specs[-1].target_conductance_uS
    step_uS = (resolved_max_uS - resolved_min_uS) / 31.0
    summary = {
        "operation": "program-column-levels",
        "col": col,
        "row_start": row_start,
        "row_end": row_end,
        "min_conductance_uS": resolved_min_uS,
        "max_conductance_uS": resolved_max_uS,
        "step_conductance_uS": step_uS,
        "half_lsb_conductance_uS": step_uS / 2.0,
        "min_uA": resolved_min_uS * READ_CONDUCTANCE_VOLTAGE_V,
        "max_uA": resolved_max_uS * READ_CONDUCTANCE_VOLTAGE_V,
        "mapping": "row_0_code_0_to_row_31_code_31" if row0_code0 else "row_31_code_0_to_row_0_code_31",
        "step_uA": step_uS * READ_CONDUCTANCE_VOLTAGE_V,
        "half_lsb_uA": step_uS * READ_CONDUCTANCE_VOLTAGE_V / 2.0,
        "confirm_reads": confirm_reads,
        "max_program_pulses": max_program_pulses,
        "read_vcc_set_V": api.config.read_rails.vcc_set_v,
        "read_vcc_wl_set_V": api.config.read_rails.vcc_wl_set_v,
        "set_program_vcc_set_V": list(api.config.set_sweep.vcc_set_v),
        "set_program_vcc_wl_set_V": list(api.config.set_sweep.vcc_wl_set_v),
        "reset_program_vcc_set_V": list(api.config.reset_sweep.vcc_set_v),
        "reset_program_vcc_wl_set_V": list(api.config.reset_sweep.vcc_wl_set_v),
        "processed": len(results),
        "qualified": qualified,
        "failed": len(results) - qualified,
        "results_file": str(result_path),
        "detailed_csv_file": str(detailed_csv_path),
    }
    (api.config.run_dir / "column_level_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Program one array column to 32 five-bit conductance levels")
    parser.add_argument("--col", type=int, default=0)
    parser.add_argument("--row-start", type=int, default=0)
    parser.add_argument("--row-end", type=int, default=31)
    parser.add_argument("--min-uS", type=float, help="code 0 conductance in uS (default: 0)")
    parser.add_argument("--max-uS", type=float, help="code 31 conductance in uS (default: 150)")
    parser.add_argument("--min-uA", type=float, help=argparse.SUPPRESS)
    parser.add_argument("--max-uA", type=float, help=argparse.SUPPRESS)
    parser.add_argument(
        "--row0-code0",
        action="store_true",
        help="map row 0 to code 0 and row 31 to code 31 (default preserves the reverse mapping)",
    )
    parser.add_argument("--confirm-reads", type=int, default=10)
    parser.add_argument("--max-program-pulses", type=int, default=64)
    parser.add_argument(
        "--set-vcc-set",
        type=float,
        help="override the fixed Vcc_set used for set-direction correction pulses",
    )
    parser.add_argument(
        "--reset-vcc-set",
        type=float,
        help="override the fixed Vcc_set used for reset-direction correction pulses",
    )
    parser.add_argument(
        "--reset-vcc-set-range",
        type=float,
        nargs=3,
        metavar=("START", "STOP", "STEP"),
        help="reset-only outer Vcc_set sweep; the complete WL sweep runs inside each Vcc_set value",
    )
    parser.add_argument(
        "--reset-vcc-wl-max",
        type=float,
        help="extend the reset-only Vcc_wl_set ramp to this maximum in 60 mV steps",
    )
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
    if args.set_vcc_set is not None:
        config.set_sweep = SweepConfig.from_ranges(
            vcc_set_v=(args.set_vcc_set,),
            vcc_wl_set_v=config.set_sweep.vcc_wl_set_v,
            threshold_uA=config.set_sweep.threshold_uA,
            direction=config.set_sweep.direction,
            confirm_reads=config.set_sweep.confirm_reads,
            stop_on_threshold=config.set_sweep.stop_on_threshold,
        )
    if args.reset_vcc_set is not None and args.reset_vcc_set_range is not None:
        parser.error("use either --reset-vcc-set or --reset-vcc-set-range, not both")
    if args.reset_vcc_set is not None:
        config.reset_sweep = SweepConfig.from_ranges(
            vcc_set_v=(args.reset_vcc_set,),
            vcc_wl_set_v=config.reset_sweep.vcc_wl_set_v,
            threshold_uA=config.reset_sweep.threshold_uA,
            direction=config.reset_sweep.direction,
            confirm_reads=config.reset_sweep.confirm_reads,
            stop_on_threshold=config.reset_sweep.stop_on_threshold,
        )
    if args.reset_vcc_set_range is not None:
        start_v, stop_v, step_v = args.reset_vcc_set_range
        config.reset_sweep = SweepConfig.from_ranges(
            vcc_set_v=inclusive_voltage_sweep(start_v, stop_v, step_v),
            vcc_wl_set_v=config.reset_sweep.vcc_wl_set_v,
            threshold_uA=config.reset_sweep.threshold_uA,
            direction=config.reset_sweep.direction,
            confirm_reads=config.reset_sweep.confirm_reads,
            stop_on_threshold=config.reset_sweep.stop_on_threshold,
        )
    if args.reset_vcc_wl_max is not None:
        config.reset_sweep = SweepConfig.from_ranges(
            vcc_set_v=config.reset_sweep.vcc_set_v,
            vcc_wl_set_v=extend_wl_sweep_to_max(
                config.reset_sweep.vcc_wl_set_v,
                args.reset_vcc_wl_max,
            ),
            threshold_uA=config.reset_sweep.threshold_uA,
            direction=config.reset_sweep.direction,
            confirm_reads=config.reset_sweep.confirm_reads,
            stop_on_threshold=config.reset_sweep.stop_on_threshold,
        )
    api = ScanDebugCellAPI(config)
    with api.hardware_queue("program-column-levels"):
        summary = program_column_levels(
            api,
            col=args.col,
            row_start=args.row_start,
            row_end=args.row_end,
            min_uS=args.min_uS,
            max_uS=args.max_uS,
            min_uA=args.min_uA,
            max_uA=args.max_uA,
            row0_code0=args.row0_code0,
            confirm_reads=args.confirm_reads,
            max_program_pulses=args.max_program_pulses,
            retry_failed=args.retry_failed,
            max_consecutive_errors=args.max_consecutive_errors,
        )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
