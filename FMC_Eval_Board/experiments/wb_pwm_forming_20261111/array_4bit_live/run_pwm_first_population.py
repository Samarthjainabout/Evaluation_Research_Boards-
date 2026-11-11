from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from config import (  # noqa: E402
    CONTROL_SCRIPT,
    GLOBAL_HARDWARE_LOCK,
    POST_WRITE_READS,
    READ_HOLD_CLOCKS,
    RESET_PWM,
    RESET_VOLTAGE_V,
)
from parser import AttemptAccumulator  # noqa: E402
from runner import HardwareLock, VivadoRunner, utc_now  # noqa: E402


PWM_CODES = tuple(range(0x10, 0x100, 0x10))
SPLIT_ARMS = ("set_only", "wl_only", "coupled")
ATTEMPT_FIELDS = (
    "run_id",
    "job_id",
    "sequence",
    "row",
    "column",
    "baseline_uS",
    "split_arm",
    "phase",
    "split_step",
    "pwm_code",
    "pwm_hex",
    "vcc_set_V",
    "vcc_wl_V",
    "pre_median_uS",
    "post_median_uS",
    "post_min_uS",
    "post_max_uS",
    "accepted",
    "wb_ack",
    "tdc_pass",
    "analog_confirmed",
    "clip",
    "sentinel_pass",
    "sentinel_max_abs_drift_uA",
    "profile_restored",
    "job_complete",
    "result_class",
    "validation_errors",
    "stop_marker",
    "vivado_log",
    "console_log",
)
CELL_FIELDS = (
    "run_id",
    "sequence",
    "row",
    "column",
    "baseline_uS",
    "baseline_bin",
    "spatial_quadrant",
    "split_arm",
    "status",
    "attempts_executed",
    "phase2_attempted",
    "success_phase",
    "success_split_step",
    "success_pwm_code",
    "success_pwm_hex",
    "success_vcc_set_V",
    "success_vcc_wl_V",
    "final_median_uS",
    "final_max_uS",
    "stop_marker",
    "completed_utc",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def append_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    exists = path.is_file()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())


def read_control(path: Path) -> str:
    if not path.is_file():
        return "run"
    payload = json.loads(path.read_text(encoding="utf-8"))
    action = str(payload.get("action", "run")).lower()
    if action not in {"run", "pause", "stop"}:
        raise RuntimeError(f"invalid control action: {action!r}")
    return action


def hash_key(seed: str, row: int, column: int) -> str:
    return hashlib.sha256(f"{seed}|r{row}|c{column}".encode()).hexdigest()


def build_population(
    prescreen_csv: Path,
    threshold_uS: float,
    seed: str,
) -> list[dict[str, Any]]:
    with prescreen_csv.open("r", encoding="utf-8", newline="") as handle:
        source = list(csv.DictReader(handle))
    cells = [
        {
            "row": int(item["row"]),
            "column": int(item["column"]),
            "baseline_uS": float(item["conductance_uS"]),
        }
        for item in source
        if float(item["conductance_uS"]) < threshold_uS
    ]
    if not cells:
        return []

    by_baseline = sorted(cells, key=lambda item: (item["baseline_uS"], item["row"], item["column"]))
    rank = {(item["row"], item["column"]): index for index, item in enumerate(by_baseline)}
    for item in cells:
        index = rank[(item["row"], item["column"])]
        item["baseline_bin"] = min(3, (4 * index) // len(cells))
        item["spatial_quadrant"] = (item["row"] // 16) * 2 + item["column"] // 16

    groups: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for item in cells:
        groups.setdefault((item["baseline_bin"], item["spatial_quadrant"]), []).append(item)
    for stratum, members in sorted(groups.items()):
        members.sort(key=lambda item: hash_key(seed + "|split", item["row"], item["column"]))
        offset = int(hashlib.sha256(f"{seed}|{stratum}".encode()).hexdigest()[:8], 16) % 3
        for index, item in enumerate(members):
            item["split_arm"] = SPLIT_ARMS[(index + offset) % 3]

    cells.sort(key=lambda item: hash_key(seed + "|order", item["row"], item["column"]))
    for sequence, item in enumerate(cells, 1):
        item["sequence"] = sequence
    return cells


def write_population(path: Path, run_id: str, cells: list[dict[str, Any]]) -> None:
    fields = (
        "run_id",
        "sequence",
        "row",
        "column",
        "baseline_uS",
        "baseline_bin",
        "spatial_quadrant",
        "split_arm",
    )
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for item in cells:
            writer.writerow({"run_id": run_id, **item})
        handle.flush()
        os.fsync(handle.fileno())


def conditions_for_cell(
    manifest: dict[str, Any], cell: dict[str, Any]
) -> list[dict[str, Any]]:
    phase1_set = float(manifest["phase1_vcc_set_V"])
    phase1_wl = float(manifest["phase1_vcc_wl_V"])
    conditions = [
        {
            "phase": "pwm_exhaustion",
            "split_step": index,
            "pwm_code": pwm,
            "vcc_set_V": phase1_set,
            "vcc_wl_V": phase1_wl,
        }
        for index, pwm in enumerate(PWM_CODES, 1)
    ]
    arm = str(cell["split_arm"])
    arm_plan = manifest["phase2_split_arms"][arm]
    if arm == "set_only":
        pairs = [
            (float(vcc_set), float(arm_plan["vcc_wl_V"]))
            for vcc_set in arm_plan["vcc_set_ladder_V"]
        ]
    elif arm == "wl_only":
        pairs = [
            (float(arm_plan["vcc_set_V"]), float(vcc_wl))
            for vcc_wl in arm_plan["vcc_wl_ladder_V"]
        ]
    elif arm == "coupled":
        pairs = [tuple(map(float, pair)) for pair in arm_plan["rail_pairs_V"]]
    else:
        raise RuntimeError(f"unsupported split arm: {arm!r}")
    for step, (vcc_set, vcc_wl) in enumerate(pairs, 1):
        if not (0.0 <= vcc_set <= 3.0 and 0.0 <= vcc_wl <= 3.0):
            raise RuntimeError(f"split rail exceeds WB-PWM 0..3.0 V envelope: {(vcc_set, vcc_wl)!r}")
        conditions.append(
            {
                "phase": f"split_{arm}",
                "split_step": step,
                "pwm_code": int(arm_plan["pwm_code"]),
                "vcc_set_V": vcc_set,
                "vcc_wl_V": vcc_wl,
            }
        )
    return conditions


def initialize(
    output_dir: Path,
    prescreen_csv: Path,
    threshold_uS: float,
    voltage_V: float,
    seed: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    population_path = output_dir / "population.csv"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["prescreen_csv_sha256"] != sha256(prescreen_csv):
            raise RuntimeError("prescreen changed since this run was initialized")
        if float(manifest["forming_threshold_uS"]) != threshold_uS:
            raise RuntimeError("forming threshold does not match the initialized run")
        if float(manifest["phase1_set_voltage_V"]) != voltage_V:
            raise RuntimeError("phase-1 voltage does not match the initialized run")
        with population_path.open("r", encoding="utf-8", newline="") as handle:
            cells = [dict(item) for item in csv.DictReader(handle)]
        for item in cells:
            for key in ("sequence", "row", "column", "baseline_bin", "spatial_quadrant"):
                item[key] = int(item[key])
            item["baseline_uS"] = float(item["baseline_uS"])
        return manifest, cells

    if any(output_dir.iterdir()):
        raise RuntimeError(f"new output directory must be empty: {output_dir}")
    cells = build_population(prescreen_csv, threshold_uS, seed)
    run_id = "pwmfirst_" + hashlib.sha256(
        f"{sha256(prescreen_csv)}|{threshold_uS}|{voltage_V}|{seed}".encode()
    ).hexdigest()[:20]
    manifest = {
        "schema": "pwm-then-split-forming-population-v2",
        "run_id": run_id,
        "created_utc": utc_now(),
        "prescreen_csv": str(prescreen_csv),
        "prescreen_csv_sha256": sha256(prescreen_csv),
        "forming_threshold_uS": threshold_uS,
        "phase1_set_voltage_V": voltage_V,
        "phase1_vcc_set_V": voltage_V,
        "phase1_vcc_wl_V": voltage_V,
        "phase1_pwm_codes": list(PWM_CODES),
        "population_count": len(cells),
        "population_order": "deterministic SHA-256 dispersion across the full array",
        "split_assignment": "preassigned within baseline-conductance quartile x spatial quadrant strata",
        "phase2_split_arms": {
            "set_only": {
                "vcc_wl_V": 1.65,
                "vcc_set_ladder_V": [1.85, 2.0, 2.15, 2.3, 2.5, 2.7, 3.0],
                "pwm_code": 32,
            },
            "wl_only": {
                "vcc_set_V": 1.65,
                "vcc_wl_ladder_V": [1.7, 1.8, 1.9, 2.0],
                "pwm_code": 32,
            },
            "coupled": {
                "rail_pairs_V": [[1.85, 1.7], [2.0, 1.8], [2.2, 1.9], [2.5, 2.0], [2.7, 2.0], [3.0, 2.0]],
                "pwm_code": 32,
            },
        },
        "wb_pwm_voltage_ceiling_V": 3.0,
        "scan_debug_forming": "disabled_for_this_campaign",
        "controller": str(CONTROL_SCRIPT.parent / "control.tcl"),
        "controller_sha256": sha256(CONTROL_SCRIPT.parent / "control.tcl"),
    }
    atomic_json(manifest_path, manifest)
    write_population(population_path, run_id, cells)
    atomic_json(output_dir / "control.json", {"action": "run", "updated_utc": utc_now()})
    atomic_json(
        output_dir / "checkpoint.json",
        {"run_id": run_id, "completed_cells": [], "status": "initialized", "updated_utc": utc_now()},
    )
    return manifest, cells


def run_cell_batch(
    output_dir: Path,
    manifest: dict[str, Any],
    cell: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, Path, Path]:
    run_id = str(manifest["run_id"])
    row = int(cell["row"])
    column = int(cell["column"])
    threshold_uS = float(manifest["forming_threshold_uS"])
    conditions = conditions_for_cell(manifest, cell)
    job_id = hashlib.sha256(f"{run_id}|pwm_then_split|r{row}|c{column}".encode()).hexdigest()[:32]
    cell_dir = output_dir / "cells" / f"r{row:02d}_c{column:02d}"
    raw_dir = cell_dir / "raw" / "vivado"
    raw_dir.mkdir(parents=True, exist_ok=True)
    if list(raw_dir.glob(f"{job_id}.*")):
        raise RuntimeError(
            f"ambiguous existing raw evidence for incomplete cell r{row}/c{column}; automatic replay prohibited"
        )

    arm = f"form{threshold_uS:g}"
    plan_lines = [
        (
            f"{row} {column} set {condition['pwm_code']} "
            f"{condition['vcc_set_V']:.6f} {condition['vcc_wl_V']:.6f} "
            f"{POST_WRITE_READS} {arm}"
        )
        for condition in conditions
    ]
    plan_text = "\n".join(plan_lines) + "\n"
    runtime_plan = CONTROL_SCRIPT.parent / "runs" / f"{job_id}.pwm.txt"
    runtime_plan.parent.mkdir(parents=True, exist_ok=True)
    with runtime_plan.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(plan_text)
        handle.flush()
        os.fsync(handle.fileno())
    evidence_plan = cell_dir / f"{job_id}.pwm.txt"
    shutil.copy2(runtime_plan, evidence_plan)

    accumulators = [
        AttemptAccumulator(row, column, "set", expected_post_reads=POST_WRITE_READS)
        for _ in conditions
    ]
    started: list[int] = []
    current_index: int | None = None
    stop_marker = ""

    def on_line(text: str, timestamp_utc: str, line_number: int) -> None:
        nonlocal current_index, stop_marker
        if text.startswith("PWM_BATCH_CELL_START "):
            fields = dict(token.split("=", 1) for token in text.split()[1:] if "=" in token)
            index = int(fields["index"]) - 1
            if index != len(started) or not 0 <= index < len(conditions):
                raise RuntimeError(f"non-sequential PWM batch index {index + 1}")
            current_index = index
            started.append(index)
        if text.startswith("PWM_BATCH_STOP_"):
            stop_marker = text
        if text == "JOB_COMPLETE pwm_doe_batch":
            for index in started:
                accumulators[index].feed(text, timestamp_utc, line_number)
        elif current_index is not None:
            accumulators[current_index].feed(text, timestamp_utc, line_number)

    arguments = [
        "pwm_doe_batch",
        str(row),
        str(column),
        str(conditions[0]["vcc_set_V"]),
        str(conditions[0]["pwm_code"]),
        str(RESET_VOLTAGE_V),
        str(RESET_PWM),
        str(READ_HOLD_CLOCKS),
        str(POST_WRITE_READS),
        job_id,
    ]
    try:
        code, vivado_log, console_log, _ = VivadoRunner(None).run(
            job_id, arguments, raw_dir, on_line
        )
    finally:
        if runtime_plan.is_file():
            runtime_plan.unlink()
    if code != 0 or not started:
        raise RuntimeError(
            f"PWM batch {job_id} failed or issued no attempts; automatic replay prohibited"
        )

    decoded = [accumulators[index].finalize(code) for index in started]
    for result in decoded:
        if not result.get("job_complete") or not result.get("profile_restored"):
            raise RuntimeError(
                f"PWM batch {job_id} has uncertain completion or rail restoration; automatic replay prohibited"
            )
    return decoded, conditions[: len(decoded)], stop_marker, vivado_log, console_log


def attempt_rows(
    manifest: dict[str, Any],
    cell: dict[str, Any],
    decoded: list[dict[str, Any]],
    executed_conditions: list[dict[str, Any]],
    stop_marker: str,
    vivado_log: Path,
    console_log: Path,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, (result, condition) in enumerate(zip(decoded, executed_conditions, strict=True)):
        rows.append(
            {
                "run_id": manifest["run_id"],
                "job_id": vivado_log.stem.replace(".vivado", ""),
                "sequence": cell["sequence"],
                "row": cell["row"],
                "column": cell["column"],
                "baseline_uS": cell["baseline_uS"],
                "split_arm": cell["split_arm"],
                "phase": condition["phase"],
                "split_step": condition["split_step"],
                "pwm_code": condition["pwm_code"],
                "pwm_hex": f"0x{int(condition['pwm_code']):02X}",
                "vcc_set_V": condition["vcc_set_V"],
                "vcc_wl_V": condition["vcc_wl_V"],
                "pre_median_uS": result.get("pre_median_uS"),
                "post_median_uS": result.get("post_median_uS"),
                "post_min_uS": result.get("post_min_uS"),
                "post_max_uS": result.get("post_max_uS"),
                "accepted": int(bool(result.get("accepted"))),
                "wb_ack": int(bool(result.get("wb_ack"))),
                "tdc_pass": int(bool(result.get("tdc_pass"))),
                "analog_confirmed": int(bool(result.get("analog_confirmed"))),
                "clip": int(bool(result.get("clip"))),
                "sentinel_pass": int(bool(result.get("sentinel_pass"))),
                "sentinel_max_abs_drift_uA": result.get("sentinel_max_abs_drift_uA"),
                "profile_restored": int(bool(result.get("profile_restored"))),
                "job_complete": int(bool(result.get("job_complete"))),
                "result_class": result.get("result_class"),
                "validation_errors": ";".join(result.get("validation_errors", [])),
                "stop_marker": stop_marker if index == len(decoded) - 1 else "",
                "vivado_log": str(vivado_log.relative_to(Path(manifest["output_dir"])))
                if "output_dir" in manifest
                else str(vivado_log),
                "console_log": str(console_log.relative_to(Path(manifest["output_dir"])))
                if "output_dir" in manifest
                else str(console_log),
            }
        )
    return rows


def classify(
    decoded: list[dict[str, Any]],
    executed_conditions: list[dict[str, Any]],
    stop_marker: str,
    threshold_uS: float,
) -> tuple[str, dict[str, Any] | None, float | None, float | None, bool]:
    if not decoded:
        return "invalid_no_attempt", None, None, None, True
    last = decoded[-1]
    median = last.get("post_median_uS")
    maximum = last.get("post_max_uS")
    validation_errors = {
        str(error)
        for item in decoded
        for error in item.get("validation_errors", [])
    }
    safety = "SAFETY" in stop_marker
    hard_gate_failure = any(
        bool(item.get("clip"))
        or not bool(item.get("sentinel_pass"))
        or not bool(item.get("wb_ack"))
        or not bool(item.get("tdc_pass"))
        or not bool(item.get("profile_restored"))
        or not bool(item.get("job_complete"))
        for item in decoded
    )
    if safety or hard_gate_failure or bool(validation_errors - {"analog_confirmed"}):
        return "safety_or_gate_pause", None, median, maximum, True
    if isinstance(median, (int, float)) and median >= threshold_uS:
        success = executed_conditions[-1]
        status = "formed_pwm" if success["phase"] == "pwm_exhaustion" else "formed_split"
        return status, success, median, maximum, False
    if isinstance(maximum, (int, float)) and maximum >= threshold_uS:
        return "threshold_transient", executed_conditions[-1], median, maximum, False
    if validation_errors == {"analog_confirmed"}:
        return (
            "all_conditions_exhausted_analog_unconfirmed"
            if not stop_marker
            else "condition_skipped_analog_unconfirmed",
            None,
            median,
            maximum,
            False,
        )
    if not stop_marker:
        return "all_conditions_exhausted", None, median, maximum, False
    return "unexpected_early_stop", None, median, maximum, True


def summarize(output_dir: Path, manifest: dict[str, Any], total: int) -> dict[str, Any]:
    results_path = output_dir / "cell_results.csv"
    rows: list[dict[str, str]] = []
    if results_path.is_file():
        with results_path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    statuses = Counter(row["status"] for row in rows)
    pwm_success = Counter(row["success_pwm_hex"] for row in rows if row["status"] == "formed_pwm")
    split_entered = Counter(row["split_arm"] for row in rows if row["phase2_attempted"] == "1")
    split_success = Counter(row["split_arm"] for row in rows if row["status"] == "formed_split")
    split_success_rate = {
        arm: (split_success[arm] / split_entered[arm] if split_entered[arm] else None)
        for arm in SPLIT_ARMS
    }
    summary = {
        "schema": "pwm-then-split-forming-summary-v2",
        "run_id": manifest["run_id"],
        "updated_utc": utc_now(),
        "population_count": total,
        "completed_cell_count": len(rows),
        "remaining_cell_count": total - len(rows),
        "status_counts": dict(statuses),
        "formed_by_pwm_code": dict(sorted(pwm_success.items())),
        "phase2_entered_by_split": dict(split_entered),
        "formed_by_split": dict(split_success),
        "split_success_rate": split_success_rate,
    }
    atomic_json(output_dir / "summary.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run PWM exhaustion, then stratified WB-PWM rail splits, on fresh full-array prescreen failures."
    )
    parser.add_argument("--prescreen-csv", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--forming-threshold-us", type=float, default=20.0)
    parser.add_argument("--set-voltage-v", type=float, default=1.65)
    parser.add_argument("--seed", default="pwm-then-split-v2")
    args = parser.parse_args()

    if not 0.5 <= args.set_voltage_v <= 2.5:
        raise RuntimeError("phase-1 SET voltage must be within 0.5..2.5 V")
    prescreen_csv = args.prescreen_csv.resolve()
    output_dir = args.output_dir.resolve()
    manifest, cells = initialize(
        output_dir,
        prescreen_csv,
        float(args.forming_threshold_us),
        float(args.set_voltage_v),
        str(args.seed),
    )
    manifest["output_dir"] = str(output_dir)
    checkpoint_path = output_dir / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    completed = set(checkpoint.get("completed_cells", []))
    control_path = output_dir / "control.json"

    if not cells:
        checkpoint.update(status="complete", updated_utc=utc_now())
        atomic_json(checkpoint_path, checkpoint)
        print(json.dumps(summarize(output_dir, manifest, 0), indent=2, sort_keys=True))
        return

    checkpoint.update(status="running", updated_utc=utc_now())
    atomic_json(checkpoint_path, checkpoint)
    try:
        with HardwareLock(GLOBAL_HARDWARE_LOCK, str(manifest["run_id"])):
            for cell in cells:
                cell_key = f"r{int(cell['row']):02d}_c{int(cell['column']):02d}"
                if cell_key in completed:
                    continue
                action = read_control(control_path)
                if action in {"pause", "stop"}:
                    checkpoint.update(status="paused" if action == "pause" else "stopped", updated_utc=utc_now())
                    atomic_json(checkpoint_path, checkpoint)
                    break

                decoded, executed_conditions, stop_marker, vivado_log, console_log = run_cell_batch(
                    output_dir, manifest, cell
                )
                rows = attempt_rows(
                    manifest, cell, decoded, executed_conditions, stop_marker, vivado_log, console_log
                )
                append_csv(output_dir / "attempts.csv", ATTEMPT_FIELDS, rows)
                status, success_condition, final_median, final_max, global_pause = classify(
                    decoded, executed_conditions, stop_marker, float(args.forming_threshold_us)
                )
                phase2_attempted = any(
                    condition["phase"] != "pwm_exhaustion"
                    for condition in executed_conditions
                )
                cell_row = {
                    "run_id": manifest["run_id"],
                    "sequence": cell["sequence"],
                    "row": cell["row"],
                    "column": cell["column"],
                    "baseline_uS": cell["baseline_uS"],
                    "baseline_bin": cell["baseline_bin"],
                    "spatial_quadrant": cell["spatial_quadrant"],
                    "split_arm": cell["split_arm"],
                    "status": status,
                    "attempts_executed": len(decoded),
                    "phase2_attempted": int(phase2_attempted),
                    "success_phase": success_condition["phase"] if success_condition else "",
                    "success_split_step": success_condition["split_step"] if success_condition else "",
                    "success_pwm_code": success_condition["pwm_code"] if success_condition else "",
                    "success_pwm_hex": f"0x{int(success_condition['pwm_code']):02X}" if success_condition else "",
                    "success_vcc_set_V": success_condition["vcc_set_V"] if success_condition else "",
                    "success_vcc_wl_V": success_condition["vcc_wl_V"] if success_condition else "",
                    "final_median_uS": final_median,
                    "final_max_uS": final_max,
                    "stop_marker": stop_marker,
                    "completed_utc": utc_now(),
                }
                append_csv(output_dir / "cell_results.csv", CELL_FIELDS, [cell_row])
                completed.add(cell_key)
                checkpoint["completed_cells"] = sorted(completed)
                checkpoint["last_cell"] = cell_key
                checkpoint["last_cell_status"] = status
                checkpoint["updated_utc"] = utc_now()
                if global_pause:
                    checkpoint["status"] = "paused_safety_review"
                    atomic_json(control_path, {"action": "pause", "updated_utc": utc_now(), "reason": status})
                atomic_json(checkpoint_path, checkpoint)
                summary = summarize(output_dir, manifest, len(cells))
                print(
                    f"{cell_key} status={status} attempts={len(decoded)} "
                    f"completed={summary['completed_cell_count']}/{len(cells)}",
                    flush=True,
                )
                if global_pause:
                    break
            else:
                checkpoint.update(status="complete", updated_utc=utc_now())
                atomic_json(checkpoint_path, checkpoint)
    except Exception as exc:
        checkpoint.update(status="blocked", error=f"{type(exc).__name__}: {exc}", updated_utc=utc_now())
        atomic_json(checkpoint_path, checkpoint)
        summarize(output_dir, manifest, len(cells))
        raise

    print(json.dumps(summarize(output_dir, manifest, len(cells)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
