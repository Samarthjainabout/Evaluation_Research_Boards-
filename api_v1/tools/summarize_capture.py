#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from pathlib import Path


SHUNT_OHMS = 470.0


def transitions_from_csv(path: Path, channel: int):
    field = f"Channel {channel}"
    transitions = []
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        previous = None
        for row in reader:
            t_s = float(row["Time [s]"])
            value = int(row[field])
            if previous is None or value != previous:
                transitions.append((t_s, value))
                previous = value
    return transitions


def deglitch(transitions, min_width_s=80e-9):
    if len(transitions) <= 2:
        return transitions
    changed = True
    items = list(transitions)
    while changed and len(items) > 2:
        changed = False
        cleaned = [items[0]]
        i = 1
        while i < len(items) - 1:
            prev_v = cleaned[-1][1]
            t_s, _value = items[i]
            next_t_s, next_v = items[i + 1]
            if next_t_s - t_s < min_width_s and prev_v == next_v:
                i += 2
                changed = True
                continue
            cleaned.append(items[i])
            i += 1
        while i < len(items):
            cleaned.append(items[i])
            i += 1
        items = cleaned
    return items


def value_at(transitions, t_s):
    value = transitions[0][1]
    for edge_t_s, edge_v in transitions[1:]:
        if edge_t_s > t_s:
            break
        value = edge_v
    return value


def first_transition(transitions, from_v, to_v, after=None):
    prior_t, prior_v = transitions[0]
    for t_s, value in transitions[1:]:
        if after is not None and t_s <= after:
            prior_t, prior_v = t_s, value
            continue
        if prior_v == from_v and value == to_v:
            return t_s
        prior_t, prior_v = t_s, value
    return None


def decode_packet(digital_csv: Path):
    clk = transitions_from_csv(digital_csv, 8)
    tm = transitions_from_csv(digital_csv, 9)
    dl = deglitch(transitions_from_csv(digital_csv, 10))
    dr = deglitch(transitions_from_csv(digital_csv, 11))
    tm_rise = first_transition(tm, 0, 1)
    tm_fall = first_transition(tm, 1, 0, after=tm_rise)
    dr_fall = first_transition(dr, 1, 0, after=tm_rise)
    dr_rise = first_transition(dr, 0, 1, after=dr_fall)

    low_samples = []
    clock_rises = []
    prior_t, prior_v = clk[0]
    for t_s, value in clk[1:]:
        if prior_v == 0 and value == 1:
            clock_rises.append(t_s)
            if tm_rise is not None and t_s > tm_rise and value_at(tm, t_s) == 1 and value_at(dr, t_s) == 0:
                low_samples.append(value_at(dl, t_s))
        prior_t, prior_v = t_s, value

    decoded = None
    if len(low_samples) >= 17:
        decoded = 0
        for bit in range(16):
            decoded |= (low_samples[1 + bit] & 1) << bit

    periods = [b - a for a, b in zip(clock_rises, clock_rises[1:])]
    return {
        "decoded_packet": decoded,
        "decoded_packet_hex": f"0x{decoded:04x}" if decoded is not None else "",
        "low_sample_count": len(low_samples),
        "low_samples": "".join(str(v) for v in low_samples[:18]),
        "tm_rise_s": tm_rise,
        "tm_fall_s": tm_fall,
        "dr_fall_s": dr_fall,
        "dr_rise_s": dr_rise,
        "dr_low_width_s": dr_rise - dr_fall if dr_fall is not None and dr_rise is not None else None,
        "clock_period_s_median": statistics.median(periods) if periods else None,
    }


def mean(values):
    return statistics.fmean(values) if values else math.nan


def current_window(analog_csv: Path, start_s, stop_s):
    set_values = []
    reset_values = []
    if start_s is None or stop_s is None:
        return math.nan, math.nan, 0
    with analog_csv.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            t_s = float(row["Time [s]"])
            if start_s <= t_s <= stop_s:
                set_values.append((float(row["Channel 12"]) - float(row["Channel 13"])) / SHUNT_OHMS * 1e6)
                reset_values.append((float(row["Channel 14"]) - float(row["Channel 15"])) / SHUNT_OHMS * 1e6)
    return mean(set_values), mean(reset_values), len(set_values)


def validate_measurement_window(decoded, set_mean_uA, reset_mean_uA, samples):
    start, stop = decoded.get("dr_rise_s"), decoded.get("tm_fall_s")
    if start is None or stop is None or not math.isfinite(start) or not math.isfinite(stop) or stop <= start:
        raise ValueError("Invalid measurement window: TM fall must follow ScanInDR rise")
    if samples < 1 or not math.isfinite(set_mean_uA) or not math.isfinite(reset_mean_uA):
        raise ValueError("Invalid measurement window: missing or non-finite analog samples")


def latest_adc(adc_csv: Path):
    if not adc_csv.exists():
        return {}
    latest = {}
    with adc_csv.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if row.get("check") == "PASS":
                latest = row
    if not latest:
        return {}
    return {
        "adc_read_uA": float(latest["adc_A2_A3_read_uA"]),
        "adc_set_uA": float(latest["adc_A0_A1_set_uA"]),
        "adc_reset_uA": float(latest["adc_A4_A5_reset_uA"]),
    }


def append_row(manifest: Path, row: dict):
    write_header = not manifest.exists() or manifest.stat().st_size == 0
    with manifest.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "index", "phase", "vcc_set_V", "vcc_wl_set_V", "packet", "bits_lsb_first",
            "remote_output_dir", "local_output_dir", "ok", "decoded_packet",
            "la_set_window_mean_uA", "la_reset_window_mean_uA",
            "adc_read_uA", "adc_set_uA", "adc_reset_uA", "error", "capture_device_id", "capture_analog_sample_rate",
        ])
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--index", required=True, type=int)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--packet", required=True)
    parser.add_argument("--bits", required=True)
    parser.add_argument("--vcc-set-v", required=True, type=float)
    parser.add_argument("--vcc-wl-set-v", required=True, type=float)
    parser.add_argument("--remote-output-dir", required=True)
    parser.add_argument("--local-output-dir", required=True)
    parser.add_argument("--recorded-local-output-dir")
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()

    local_dir = Path(args.local_output_dir)
    expected_packet = int(args.packet, 16)
    error = ""
    try:
        decoded = decode_packet(local_dir / "digital.csv")
        set_mean_uA, reset_mean_uA, samples = current_window(
            local_dir / "analog.csv",
            decoded["dr_rise_s"],
            decoded["tm_fall_s"],
        )
        validate_measurement_window(decoded, set_mean_uA, reset_mean_uA, samples)
        adc = latest_adc(local_dir / "adc_monitor.csv")
        ok = decoded["decoded_packet"] == expected_packet
        if not ok:
            error = f"decoded {decoded['decoded_packet_hex']} != expected {args.packet}"
    except Exception as exc:
        decoded = {"decoded_packet_hex": "", "dr_rise_s": None, "tm_fall_s": None}
        set_mean_uA = math.nan
        reset_mean_uA = math.nan
        samples = 0
        adc = {}
        ok = False
        error = f"{type(exc).__name__}: {exc}"

    metadata_path = local_dir / "analysis.json"
    metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
    summary = {
        "index": args.index,
        "phase": args.phase,
        "vcc_set_V": args.vcc_set_v,
        "vcc_wl_set_V": args.vcc_wl_set_v,
        "packet": args.packet,
        "bits_lsb_first": args.bits,
        "remote_output_dir": args.remote_output_dir,
        "local_output_dir": args.recorded_local_output_dir or str(local_dir),
        "ok": ok,
        "decoded_packet": decoded["decoded_packet_hex"],
        "la_set_window_mean_uA": set_mean_uA,
        "la_reset_window_mean_uA": reset_mean_uA,
        "adc_read_uA": adc.get("adc_read_uA", ""),
        "adc_set_uA": adc.get("adc_set_uA", ""),
        "adc_reset_uA": adc.get("adc_reset_uA", ""),
        "error": error,
        "capture_device_id": metadata.get("device_id", ""),
        "capture_analog_sample_rate": metadata.get("analog_sample_rate", ""),
    }
    append_row(args.manifest, summary)
    (local_dir / "capture_summary.json").write_text(json.dumps({**summary, "decoded": decoded, "samples_in_window": samples}, indent=2))
    print(
        f"SUMMARY index={args.index} phase={args.phase} ok={ok} decoded={decoded['decoded_packet_hex']} "
        f"set_window={set_mean_uA:.3f}uA reset_window={reset_mean_uA:.3f}uA samples={samples}"
    )
    if not ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
