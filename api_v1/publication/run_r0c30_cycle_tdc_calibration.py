"""Acquire empirical TDC calibration along the working GUI r0c30 scan ramp.

Keep c31 unprogrammed, but measure it before/after every WB group. Log actual
achieved states, not requested targets. Stop after one finite SET/RESET sweep.
The raw state log is authoritative; fits are provisional and state-held-out.
"""
from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import statistics as stats
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cell_api import (CellAddress, RailVoltages, ScanDebugCellAPI, SweepConfig,
                      ScanDebugConfig, DEFAULT_READ_CALIBRATION,
                      FPGA_BITSTREAM_DIR, FPGA_RUNTIME_BITSTREAM)
from run_fine_count_calibration_20260928 import BenchTransport, scan_once, wb_read

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "runs/gui_20260930_114523_r00c30_cycle"


def save_json(path, value):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")
    temp.replace(path)


def append(path, value):
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, allow_nan=False) + "\n")


def rails_for(settings, operation):
    return [RailVoltages(v, wl) for v in settings[f"{operation}_vcc_set_V"]
            for wl in settings[f"{operation}_wl_V"]]


def make_api(run, source):
    config = ScanDebugConfig(
        run_dir=run, attempts=1, read_feedback_attempts=3,
        summarizer=Path(__file__).with_name("summarize_scan_2mhz.py"),
        read_calibration_path=source / "read_calibration_profile.json",
        defer_capture_copy=False, hardware_queue_timeout_seconds=20,
        wishbone_persistent_uart=True, wishbone_skip_passive_snapshot=True,
        wishbone_uart_timeout_seconds=40,
    )
    api = ScanDebugCellAPI(config)
    api.runner = BenchTransport()
    return api


def scan(api, col, measurement=None, *, row=0):
    if measurement is None:
        result = scan_once(api, row, col)
    else:
        # Reuse the public programming API's verification capture without
        # triggering another hardware read just to extract measured rails.
        result = dict(measurement)
        capture = Path(result["local_output_dir"])
        summary = json.loads((capture / "capture_summary.json").read_text())
        timing = summary["decoded"]
        with (capture / "analog.csv").open(newline="") as handle:
            window = [r for r in csv.DictReader(handle)
                      if timing["dr_rise_s"] <= float(r["Time [s]"]) <= timing["tm_fall_s"]]
        voltage = stats.mean(float(r["Channel 0"]) for r in window)
        result.update(read_V=voltage, wl_V=stats.mean(float(r["Channel 1"]) for r in window),
                      G_uS=result["current_uA"] / voltage,
                      hold_us=(timing["tm_fall_s"]-timing["dr_rise_s"])*1e6)
    if not result["ok"] or not math.isfinite(result["G_uS"]):
        raise RuntimeError(f"Invalid scan r{row}c{col}")
    if not 0.40 <= result["read_V"] <= 0.60 or not 2.25 <= result["wl_V"] <= 2.75:
        raise RuntimeError(f"READ rails outside tolerance: {result['read_V']}, {result['wl_V']}")
    if not 45 <= result["hold_us"] <= 55:
        raise RuntimeError(f"Scan hold differs from working cycle: {result['hold_us']} us")
    # Preserve the GUI's nominal-voltage estimate as well as measured-V estimate.
    result["G_nominal_uS"] = result["current_uA"] / 0.5
    return result


def program_step(api, operation, rail):
    """Delegate pulse, feedback, threshold confirmation and resume to the API."""
    sweep = SweepConfig.from_ranges(
        vcc_set_v=(rail.vcc_set_v,), vcc_wl_set_v=(rail.vcc_wl_set_v,),
        threshold_uA=35 if operation == "set" else 15,
        direction="above" if operation == "set" else "below", confirm_reads=2)
    setattr(api.config, operation + "_sweep", sweep)
    return getattr(api, operation + "_cell")(0, 30)


def completed_rails(run, operation):
    manifest = run / "manifest.csv"
    if not manifest.exists():
        return set()
    with manifest.open(newline="") as handle:
        return {(round(float(r["vcc_set_V"]), 6), round(float(r["vcc_wl_set_V"]), 6))
                for r in csv.DictReader(handle)
                if r["cell"] == "0_30" and r["operation"] == operation and r["ok"].lower() == "true"}


def pair(api, reverse=False, *, row=0):
    order = (31, 30) if reverse else (30, 31)
    values = {str(col): scan(api, col, row=row) for col in order}
    return {"time": datetime.now().isoformat(), "order": list(order),
            "reads": values, "G30_uS": values["30"]["G_uS"],
            "G31_uS": values["31"]["G_uS"],
            "G31_minus_G30_uS": values["31"]["G_uS"] - values["30"]["G_uS"]}


def summarize_state(before, after, entries, drift_limit):
    selected = [entry["selected_matching"] for entry in entries]
    coarse = [entry["coarse_cnt"] for entry in selected]
    modal = stats.multimode(coarse)[0]
    drift = after["G31_minus_G30_uS"] - before["G31_minus_G30_uS"]
    reference_drift = after["G31_uS"] - before["G31_uS"]
    # Preserve C,F pairs across wrap. Never combine independently averaged C,F.
    ticks = [100 * item["coarse_cnt"] + 99 - item["fine_cnt"] for item in selected]
    delta = (before["G31_minus_G30_uS"] + after["G31_minus_G30_uS"]) / 2
    reasons = []
    if abs(drift) > drift_limit:
        reasons.append("differential_drift")
    if abs(reference_drift) > drift_limit:
        reasons.append("reference_drift")
    if any(item["fine_cnt"] > 100 for item in selected):
        reasons.append("fine_outside_observed_range")
    if any(item["coarse_cnt"] >= 51 for item in selected):
        reasons.append("possible_coarse_timeout")
    return {"G30_uS": (before["G30_uS"] + after["G30_uS"]) / 2,
            "G31_uS": (before["G31_uS"] + after["G31_uS"]) / 2,
            "G31_minus_G30_uS": delta, "G30_minus_G31_uS": -delta,
            "delta_drift_uS": drift, "reference_drift_uS": reference_drift,
            "modal_coarse": modal,
            "median_fine_at_modal_coarse": stats.median(
                x["fine_cnt"] for x in selected if x["coarse_cnt"] == modal),
            "T100_median": stats.median(ticks),
            "fit_eligible": not reasons, "quality_flags": reasons}


def fit_states(states):
    import numpy as np
    good = [s for s in states if s["summary"]["fit_eligible"]]
    y = np.array([s["summary"]["G31_minus_G30_uS"] for s in good])
    if len(good) < 6 or np.ptp(y) < 25:
        return {"status": "insufficient_stable_states_or_range", "stable_states": len(good),
                "required_states": 6, "required_span_uS": 25,
                "span_uS": float(np.ptp(y)) if len(y) else 0}
    means = []
    for state in good:
        frames = [w["selected_matching"] for w in state["wb"]]
        means.append((stats.mean(w["coarse_cnt"] for w in frames),
                      stats.mean(w["fine_cnt"] for w in frames)))
    c, f = np.asarray(means).T
    features = {"coarse_only": np.column_stack((np.ones(len(c)), c)),
                "coarse_fine": np.column_stack((np.ones(len(c)), c, f)),
                "wrap100_minus_fine": np.column_stack((np.ones(len(c)), 100*c+99-f))}
    models = {}
    for name, x in features.items():
        if np.linalg.matrix_rank(x) < x.shape[1]:
            continue
        beta = np.linalg.lstsq(x, y, rcond=None)[0]
        errors = []
        for i in range(len(y)):
            keep = np.arange(len(y)) != i
            if np.linalg.matrix_rank(x[keep]) < x.shape[1]:
                break
            b = np.linalg.lstsq(x[keep], y[keep], rcond=None)[0]
            errors.append(float(x[i] @ b - y[i]))
        models[name] = {"coefficients": beta.tolist(),
                        "train_rmse_uS": float(np.sqrt(np.mean((x @ beta-y)**2))),
                        "leave_state_out_rmse_uS": float(np.sqrt(np.mean(np.square(errors))))
                        if len(errors) == len(y) else None}
    return {"status": "provisional_not_cross_cell_validated", "stable_states": len(good),
            "target": "G31-G30 in uS", "span_uS": float(np.ptp(y)), "models": models,
            "fine_wrap_period": "100 is a diagnostic hypothesis, not established calibration"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--resume-run", type=Path)
    parser.add_argument("--source-run", type=Path, default=SOURCE)
    parser.add_argument("--wb-repeats", type=int, default=5)
    parser.add_argument("--cycles", type=int, default=1)
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    if args.wb_repeats < 3 or args.cycles != 1:
        parser.error("Use >=3 WB repeats and one finite cycle per run")
    settings = json.loads((args.source_run / "experiment_settings.jsonl").read_text().splitlines()[-1])
    plan = {"source_run": str(args.source_run), "row": 0, "program_col": 30,
            "reference_col": 31, "reference_programmed": False,
            "G30_endpoints_uS": {"set": 70, "reset": 30},
            "checkpoint_change_uS": 8, "checkpoint_max_pulse_gap": 16,
            "state_drift_limit_uS": 12, "wb_repeats": args.wb_repeats,
            "cycles": args.cycles, "programming_ramps": {
                op: [asdict(r) for r in rails_for(settings, op)] for op in ("set", "reset")},
            "sequence": "scan pair -> repeated WB column31 READ -> reverse scan pair",
            "read_profile": str(args.source_run / "read_calibration_profile.json"),
            "shunt_ohms": 470, "scan_hold_expected_us": 50,
            "wb_read_packet": "0x41F2AAFF", "scan_clock_MHz": 2, "wb_clock_MHz": 10,
            "biases_V": {"Iref": 1.0, "Vcomp": 0.9, "VBIAS": 1.6,
                         "Bias_comp2": 0.6, "dc_bias": 1.5},
            "sign": "Fit G31-G30; also store its negative G30-G31"}
    if args.plan_only:
        print(json.dumps(plan, indent=2))
        return 0
    run = args.resume_run or args.run_dir or ROOT / "runs" / ("tdc_r0c30_cycle_" + datetime.now().strftime("%Y%m%d_%H%M%S"))
    if args.resume_run:
        report = json.loads((run / "report.json").read_text())
        if report.get("complete"):
            raise RuntimeError("Completed experiment cannot be resumed")
        report.setdefault("resumes", []).append({"time": datetime.now().isoformat(),
            "prior_error": report.pop("error", None), "decoder": "summarize_scan_2mhz.py",
            "read_retries": 3, "program_api": "set_cell/reset_cell"})
        plan = json.loads((run / "plan.json").read_text())
        args.wb_repeats = plan["wb_repeats"]
        save_json(run / "report.json", report)
    else:
        run.mkdir(parents=True, exist_ok=False)
        report = {"run_dir": str(run), "complete": False, "states": [], "phases": []}
    plan["bitstream_sha256"] = hashlib.sha256((FPGA_BITSTREAM_DIR / FPGA_RUNTIME_BITSTREAM).read_bytes()).hexdigest()
    if not args.resume_run:
        save_json(run / "plan.json", plan)
    api = make_api(run, args.source_run)
    print("RUN_DIR=" + str(run), flush=True)

    def check_stop():
        if (run / "STOP").exists():
            raise RuntimeError("Requested safe stop")

    def checkpoint(phase, pulse_index, rail=None):
        check_stop()
        index = len(report["states"])
        acquisition_id = uuid.uuid4().hex
        api._append_progress("tdc-cycle-calibration", f"State {index}: {phase}, scan pair before WB", row=0, col=30)
        before = pair(api, reverse=bool(index % 2))
        append(run / "checkpoint_attempts.jsonl", {"index": index, "acquisition_id": acquisition_id,
               "phase": phase, "pulse_index": pulse_index, "before": before})
        entries = []
        for repeat in range(args.wb_repeats):
            check_stop()
            entry = wb_read(api, 0)
            append(run / "tdc_raw.jsonl", {"state": index, "acquisition_id": acquisition_id,
                   "repeat": repeat, "wb": entry})
            entries.append(entry)
        after = pair(api, reverse=not bool(index % 2))
        state = {"index": index, "acquisition_id": acquisition_id, "phase": phase, "pulse_index": pulse_index,
                 "rail": asdict(rail) if rail else None, "before": before, "after": after,
                 "wb": entries, "summary": summarize_state(before, after, entries, 12)}
        append(run / "states.jsonl", state)
        report["states"].append(state)
        report["fit"] = fit_states(report["states"])
        save_json(run / "report.json", report)
        print("STATE=" + json.dumps({"index": index, "phase": phase, **state["summary"]}), flush=True)
        api._append_progress("tdc-cycle-calibration", f"State {index} measured", row=0, col=30, **state["summary"])
        return after["G30_uS"]

    try:
        with api.hardware_queue("tdc-cycle-calibration-r0c30"):
            api._ensure_runtime_bitstream()
            api._ensure_runtime_vio_daemon()
            # WB reapplies its nominal bias profile; explicitly match it in scan.
            for bias, value in (("iref", 1), ("vcomp", .9), ("vbias", 1.6),
                                ("bias_comp2", .6), ("dc_bias", 1.5)):
                api.set_bias_voltage(bias, value)
            current = checkpoint("resume_read_only" if args.resume_run else "baseline", 0)
            for cycle in range(args.cycles):
                for operation in ("set", "reset"):
                    phase = f"cycle{cycle+1}_{operation}"
                    if any(p["phase"] == phase for p in report["phases"]):
                        continue
                    done = completed_rails(run, operation)
                    reached = (lambda g: g >= 70) if operation == "set" else (lambda g: g <= 30)
                    last_checkpoint, last_checkpoint_pulse = current, 0
                    used, qualified = 0, False
                    for i, rail in enumerate(rails_for(settings, operation), 1):
                        check_stop()
                        if (round(rail.vcc_set_v, 6), round(rail.vcc_wl_set_v, 6)) in done:
                            used = i
                            continue
                        if reached(current):
                            confirm = scan(api, 30)
                            append(run / "pulse_reads.jsonl", {"phase": phase, "kind": "confirmation", "read": confirm})
                            current = confirm["G_uS"]
                            if reached(current):
                                qualified = True
                                break
                        outcome = program_step(api, operation, rail)
                        if not outcome["steps"]:
                            continue
                        step = outcome["steps"][-1]
                        measurement = (step.get("confirm_reads") or [step["verify"]])[-1]
                        read = scan(api, 30, measurement)
                        current, used = read["G_uS"], i
                        append(run / "pulse_reads.jsonl", {"phase": phase, "pulse_index": i,
                              "pulse": step["pulse"], "read": read, "api_target_hit": outcome["target_hit"]})
                        api._append_progress("tdc-cycle-calibration", f"{phase} pulse {i}: G30={current:.2f} uS",
                                             row=0, col=30, rails=asdict(rail))
                        if abs(current-last_checkpoint) >= 8 or i-last_checkpoint_pulse >= 16:
                            current = checkpoint(phase, i, rail)
                            last_checkpoint, last_checkpoint_pulse = current, i
                        if outcome["target_hit"]:
                            qualified = True
                            break
                    current = checkpoint(phase + "_end", used)
                    # Independent no-program repeat checks read repeatability.
                    current = checkpoint(phase + "_hold", used)
                    report["phases"].append({"phase": phase, "pulses": used,
                          "endpoint_confirmed_in_ramp": qualified,
                          "endpoint_held": reached(current), "final_G30_uS": current})
                    save_json(run / "report.json", report)
            report["complete"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        print("STOP=" + report["error"], flush=True)
    finally:
        report["fit"] = fit_states(report["states"])
        save_json(run / "report.json", report)
    print("RESULT=" + str(run / "report.json"), flush=True)
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
