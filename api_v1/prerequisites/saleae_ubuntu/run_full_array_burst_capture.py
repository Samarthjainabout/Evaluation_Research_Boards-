#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import os
import statistics
import sys
import threading
import time
from bisect import bisect_left, bisect_right
from datetime import datetime
from pathlib import Path

from saleae import automation

import run_fpga_scan0000_la12_15_capture as base


DIGITAL_CHANNELS = [6, 7, 8, 9, 10, 11]
ANALOG_CHANNELS = [12, 13, 14, 15]
DIGITAL_SAMPLE_RATE = int(os.environ.get("DIGITAL_SAMPLE_RATE", "50000000"))
ANALOG_SAMPLE_RATE = int(os.environ.get("ANALOG_SAMPLE_RATE", "3125000"))
DIGITAL_THRESHOLD_VOLTS = float(os.environ.get("DIGITAL_THRESHOLD_VOLTS", "1.2"))
AFTER_TRIGGER_SECONDS = float(os.environ.get("AFTER_TRIGGER_SECONDS", "0.000028"))
TRIM_DATA_SECONDS = float(os.environ.get("TRIM_DATA_SECONDS", "0.000003"))
MAX_CELLS = int(os.environ.get("MAX_CELLS", "1024"))
SHUNT_OHMS = float(os.environ.get("SHUNT_OHMS", "470.0"))
RAIL_COMMAND = os.environ.get("RAIL_COMMAND", "SCAN_SET_RAILS").strip()
SKIP_SET_RAILS = os.environ.get("SKIP_SET_RAILS", "0") == "1"
ENABLE_ADC_MONITOR = os.environ.get("ENABLE_ADC_MONITOR", "1") != "0"
ADC_START_DELAY_SECONDS = float(os.environ.get("ADC_START_DELAY_SECONDS", "0.0"))
STOP_ON_MISMATCH = os.environ.get("STOP_ON_MISMATCH", "1") != "0"
START_ROW = int(os.environ.get("START_ROW", "0"))
START_COL = int(os.environ.get("START_COL", "0"))
TRIGGER_CHANNEL_INDEX = int(os.environ.get("TRIGGER_CHANNEL_INDEX", "9"))
TRIGGER_TYPE = os.environ.get("TRIGGER_TYPE", "RISING").strip().upper()
CAPTURE_STRATEGY = os.environ.get("CAPTURE_STRATEGY", "single").strip().lower()
MEASURE_SKIP_END_CYCLES = float(os.environ.get("MEASURE_SKIP_END_CYCLES", "3"))
FULL_ARRAY_DETERMINISTIC_TIMING = os.environ.get("FULL_ARRAY_DETERMINISTIC_TIMING", "0") == "1"
FPGA_RESET_ASSERT_CYCLES = int(os.environ.get("FPGA_RESET_ASSERT_CYCLES", "24000"))
RESET_RELEASE_FALLBACK_CYCLES = int(os.environ.get("RESET_RELEASE_FALLBACK_CYCLES", "2000"))
POST_RESET_WAIT_CYCLES = int(os.environ.get("POST_RESET_WAIT_CYCLES", "128"))
POST_DR_TM_HOLD_CYCLES = int(os.environ.get("POST_DR_TM_HOLD_CYCLES", "100"))
REPEAT_AFTER_DONE_CYCLES = int(os.environ.get("REPEAT_AFTER_DONE_CYCLES", "1"))
WB_CLK_PERIOD_SECONDS = float(os.environ.get("WB_CLK_PERIOD_SECONDS", "0.0000005"))
FULL_ARRAY_PACKET_PERIOD_SECONDS = float(os.environ.get("FULL_ARRAY_PACKET_PERIOD_SECONDS", "0.01312428"))


def sweep_cells():
    cells = []
    for row in range(32):
        cells.append((row, 0))
    for col in range(1, 32):
        for row in range(32):
            cells.append((row, col))
    try:
        start_index = cells.index((START_ROW, START_COL))
    except ValueError as exc:
        raise ValueError(f"START_ROW/START_COL ({START_ROW},{START_COL}) is not in the sweep order") from exc
    return cells[start_index:start_index + MAX_CELLS]


def packet_for(row: int, col: int) -> int:
    return (row << 10) | (col << 5) | row


def lsb_bits(packet: int) -> str:
    return "".join(str((packet >> bit) & 1) for bit in range(16))


def stats(values):
    values = list(values)
    if not values:
        return {"n": 0, "mean": None, "std": None, "min": None, "max": None, "span": None}
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 9),
        "std": round(statistics.pstdev(values), 9) if len(values) > 1 else 0.0,
        "min": round(min(values), 9),
        "max": round(max(values), 9),
        "span": round(max(values) - min(values), 9),
    }


def transitions_from_csv(path: Path, channel: int):
    field = f"Channel {channel}"
    transitions = []
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        previous = None
        for row in reader:
            t = float(row["Time [s]"])
            value = int(row[field])
            if previous is None or value != previous:
                transitions.append((t, value))
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
            prev_t, prev_v = cleaned[-1]
            t, v = items[i]
            next_t, next_v = items[i + 1]
            if next_t - t < min_width_s and prev_v == next_v:
                i += 2
                changed = True
                continue
            cleaned.append((t, v))
            i += 1
        while i < len(items):
            cleaned.append(items[i])
            i += 1
        items = cleaned
    return items


def value_at(transitions, t):
    value = transitions[0][1]
    for edge_t, edge_v in transitions[1:]:
        if edge_t > t:
            break
        value = edge_v
    return value


def first_transition(transitions, from_v, to_v, after=None):
    prior_t, prior_v = transitions[0]
    for t, v in transitions[1:]:
        if after is not None and t <= after:
            prior_t, prior_v = t, v
            continue
        if prior_v == from_v and v == to_v:
            return t
        prior_t, prior_v = t, v
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
    prev_t, prev_v = clk[0]
    for t, v in clk[1:]:
        if prev_v == 0 and v == 1:
            clock_rises.append(t)
            if tm_rise is not None and t > tm_rise and value_at(tm, t) == 1 and value_at(dr, t) == 0:
                low_samples.append(value_at(dl, t))
        prev_t, prev_v = t, v

    decoded = 0
    if len(low_samples) >= 17:
        for bit in range(16):
            decoded |= (low_samples[1 + bit] & 1) << bit

    periods = [b - a for a, b in zip(clock_rises, clock_rises[1:])]
    return {
        "decoded_packet": decoded if len(low_samples) >= 17 else None,
        "low_sample_count": len(low_samples),
        "low_samples": "".join(str(v) for v in low_samples[:18]),
        "tm_rise_s": tm_rise,
        "tm_fall_s": tm_fall,
        "dr_fall_s": dr_fall,
        "dr_rise_s": dr_rise,
        "dr_low_width_s": (dr_rise - dr_fall) if dr_fall is not None and dr_rise is not None else None,
        "clock_period_s_median": round(statistics.median(periods), 12) if periods else None,
    }


def latest_adc_sample(adc_csv: Path):
    if not adc_csv.exists():
        return None
    latest = None
    with adc_csv.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if row.get("check") == "PASS":
                latest = row
    if latest is None:
        return None
    return {
        "t_s": float(latest["t_s"]),
        "read_uA": float(latest["adc_A2_A3_read_uA"]),
        "set_uA": float(latest["adc_A0_A1_set_uA"]),
        "reset_uA": float(latest["adc_A4_A5_reset_uA"]),
    }


def compact_analog_summary(analog_csv: Path):
    set_values = []
    reset_values = []
    with analog_csv.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            set_values.append((float(row["Channel 12"]) - float(row["Channel 13"])) / SHUNT_OHMS * 1e6)
            reset_values.append((float(row["Channel 14"]) - float(row["Channel 15"])) / SHUNT_OHMS * 1e6)
    return {
        "set_A12_minus_A13_uA": {**stats(set_values), "units": "uA"},
        "reset_A14_minus_A15_uA": {**stats(reset_values), "units": "uA"},
    }


def window_analog_summary(analog_csv: Path, start_s, stop_s):
    if start_s is None or stop_s is None or stop_s <= start_s:
        return compact_analog_summary(analog_csv)
    set_values = []
    reset_values = []
    with analog_csv.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            t_s = float(row["Time [s]"])
            if start_s <= t_s <= stop_s:
                set_values.append((float(row["Channel 12"]) - float(row["Channel 13"])) / SHUNT_OHMS * 1e6)
                reset_values.append((float(row["Channel 14"]) - float(row["Channel 15"])) / SHUNT_OHMS * 1e6)
    return {
        "set_A12_minus_A13_uA": {**stats(set_values), "units": "uA"},
        "reset_A14_minus_A15_uA": {**stats(reset_values), "units": "uA"},
    }


def configure_base_globals():
    base.DIGITAL_CHANNELS = DIGITAL_CHANNELS
    base.ANALOG_CHANNELS = ANALOG_CHANNELS
    base.ANALOG_SAMPLE_RATE = ANALOG_SAMPLE_RATE
    base.DIGITAL_THRESHOLD_VOLTS = DIGITAL_THRESHOLD_VOLTS
    base.SHUNT_OHMS = SHUNT_OHMS
    base.SCAN_RAIL_COMMAND = RAIL_COMMAND


def digital_trigger_type():
    if TRIGGER_TYPE == "FALLING":
        return automation.DigitalTriggerType.FALLING
    if TRIGGER_TYPE == "RISING":
        return automation.DigitalTriggerType.RISING
    raise ValueError(f"Unsupported TRIGGER_TYPE={TRIGGER_TYPE!r}; use RISING or FALLING")


def transitions_for_channels(path: Path, channels):
    fields = {channel: f"Channel {channel}" for channel in channels}
    transitions = {channel: [] for channel in channels}
    previous = {}
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            t_s = float(row["Time [s]"])
            for channel, field in fields.items():
                if field not in row:
                    continue
                value = int(row[field])
                if channel not in previous or value != previous[channel]:
                    transitions[channel].append((t_s, value))
                    previous[channel] = value
    return transitions


def transition_times(transitions, from_v, to_v, after=None, before=None):
    times = []
    if len(transitions) < 2:
        return times
    prior_t, prior_v = transitions[0]
    for t_s, value in transitions[1:]:
        if after is not None and t_s <= after:
            prior_t, prior_v = t_s, value
            continue
        if before is not None and t_s >= before:
            break
        if prior_v == from_v and value == to_v:
            times.append(t_s)
        prior_t, prior_v = t_s, value
    return times


def last_transition(transitions, from_v, to_v, before):
    found = None
    if len(transitions) < 2:
        return found
    prior_t, prior_v = transitions[0]
    for t_s, value in transitions[1:]:
        if t_s >= before:
            break
        if prior_v == from_v and value == to_v:
            found = t_s
        prior_t, prior_v = t_s, value
    return found


def clock_rises(transitions):
    return transition_times(transitions, 0, 1)


def decode_packet_windows(digital_csv: Path, max_windows: int):
    transitions = transitions_for_channels(digital_csv, [8, 9, 10, 11])
    clk = transitions[8]
    tm = deglitch(transitions[9])
    dl = deglitch(transitions[10])
    dr = deglitch(transitions[11])
    clk_rises = clock_rises(clk)
    dr_falls = transition_times(dr, 1, 0)
    windows = []

    for dr_fall in dr_falls:
        if len(windows) >= max_windows:
            break
        dr_rise = first_transition(dr, 0, 1, after=dr_fall)
        if dr_rise is None:
            continue
        tm_fall = first_transition(tm, 1, 0, after=dr_rise)
        tm_rise = last_transition(tm, 0, 1, before=dr_fall)
        low_start = bisect_left(clk_rises, dr_fall)
        low_stop = bisect_right(clk_rises, dr_rise)
        low_samples = [
            value_at(dl, t_s)
            for t_s in clk_rises[low_start:low_stop]
            if dr_fall < t_s < dr_rise and value_at(tm, t_s) == 1
        ]
        decoded = None
        if len(low_samples) >= 17:
            decoded = 0
            for bit in range(16):
                decoded |= (low_samples[1 + bit] & 1) << bit
        near_start = bisect_left(clk_rises, dr_fall - 10e-6)
        near_stop = bisect_right(clk_rises, dr_rise + 10e-6)
        near_clocks = clk_rises[near_start:near_stop]
        periods = [b - a for a, b in zip(near_clocks, near_clocks[1:])]
        clock_period = statistics.median(periods) if periods else None
        measure_start = dr_rise
        measure_stop = tm_fall
        if measure_stop is not None and clock_period is not None:
            measure_stop = max(measure_start, measure_stop - (MEASURE_SKIP_END_CYCLES * clock_period))
        windows.append(
            {
                "decoded_packet": decoded,
                "decoded_packet_hex": f"0x{decoded:04x}" if decoded is not None else "",
                "low_sample_count": len(low_samples),
                "low_samples": "".join(str(value) for value in low_samples[:18]),
                "tm_rise_s": tm_rise,
                "tm_fall_s": tm_fall,
                "dr_fall_s": dr_fall,
                "dr_rise_s": dr_rise,
                "dr_low_width_s": dr_rise - dr_fall,
                "clock_period_s_median": round(clock_period, 12) if clock_period is not None else None,
                "measurement_start_s": measure_start,
                "measurement_stop_s": measure_stop,
            }
        )
    return windows


def deterministic_packet_windows(cells):
    fallback_cycles_per_cell = (
        (FPGA_RESET_ASSERT_CYCLES + 1)
        + (RESET_RELEASE_FALLBACK_CYCLES + 1)
        + (POST_RESET_WAIT_CYCLES + 1)
        + 1
        + 18
        + POST_DR_TM_HOLD_CYCLES
        + (REPEAT_AFTER_DONE_CYCLES + 1)
    )
    packet_period = FULL_ARRAY_PACKET_PERIOD_SECONDS or (fallback_cycles_per_cell * WB_CLK_PERIOD_SECONDS)
    windows = []
    for index, (row, col) in enumerate(cells):
        packet = packet_for(row, col)
        dr_fall = index * packet_period
        dr_rise = dr_fall + (18 * WB_CLK_PERIOD_SECONDS)
        tm_rise = dr_fall - WB_CLK_PERIOD_SECONDS
        tm_fall = dr_rise + (POST_DR_TM_HOLD_CYCLES * WB_CLK_PERIOD_SECONDS)
        measure_stop = max(dr_rise, tm_fall - (MEASURE_SKIP_END_CYCLES * WB_CLK_PERIOD_SECONDS))
        windows.append(
            {
                "decoded_packet": packet,
                "decoded_packet_hex": f"0x{packet:04x}",
                "low_sample_count": "deterministic",
                "low_samples": lsb_bits(packet),
                "tm_rise_s": tm_rise,
                "tm_fall_s": tm_fall,
                "dr_fall_s": dr_fall,
                "dr_rise_s": dr_rise,
                "dr_low_width_s": dr_rise - dr_fall,
                "clock_period_s_median": round(WB_CLK_PERIOD_SECONDS, 12),
                "measurement_start_s": dr_rise,
                "measurement_stop_s": measure_stop,
                "timing_source": "measured_packet_period" if FULL_ARRAY_PACKET_PERIOD_SECONDS else "fpga_sequence_parameters",
                "packet_period_s": packet_period,
                "fallback_cycles_per_cell": fallback_cycles_per_cell,
            }
        )
    return windows


def window_analog_summaries(analog_csv: Path, windows):
    accum = [
        {
            "set": [],
            "reset": [],
        }
        for _ in windows
    ]
    if not analog_csv.exists() or not windows:
        return [
            {
                "set_A12_minus_A13_uA": {**stats([]), "units": "uA"},
                "reset_A14_minus_A15_uA": {**stats([]), "units": "uA"},
            }
            for _ in windows
        ]

    ordered = [
        (index, window.get("measurement_start_s"), window.get("measurement_stop_s"))
        for index, window in enumerate(windows)
        if window.get("measurement_start_s") is not None and window.get("measurement_stop_s") is not None
    ]
    ordered.sort(key=lambda item: item[1])
    window_pos = 0
    with analog_csv.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if window_pos >= len(ordered):
                break
            t_s = float(row["Time [s]"])
            while window_pos < len(ordered) and t_s > ordered[window_pos][2]:
                window_pos += 1
            if window_pos >= len(ordered):
                break
            index, start_s, stop_s = ordered[window_pos]
            if start_s <= t_s <= stop_s:
                try:
                    accum[index]["set"].append((float(row["Channel 12"]) - float(row["Channel 13"])) / SHUNT_OHMS * 1e6)
                except (KeyError, ValueError):
                    pass
                try:
                    accum[index]["reset"].append((float(row["Channel 14"]) - float(row["Channel 15"])) / SHUNT_OHMS * 1e6)
                except (KeyError, ValueError):
                    pass

    return [
        {
            "set_A12_minus_A13_uA": {**stats(item["set"]), "units": "uA"},
            "reset_A14_minus_A15_uA": {**stats(item["reset"]), "units": "uA"},
        }
        for item in accum
    ]


def run_single_capture(root: Path, cells, rails, adc_csv: Path, manifest_csv: Path, manifest_json: Path, run_log: Path):
    trace_dir = root / "column_capture"
    trace_dir.mkdir()
    stop_event = threading.Event()
    starts = {}
    adc_state = {"enabled": ENABLE_ADC_MONITOR}
    adc_thread = None
    if ENABLE_ADC_MONITOR:
        adc_thread = threading.Thread(target=base.adc_monitor, args=(stop_event, root, starts, adc_state), daemon=True)
        adc_thread.start()
        if ADC_START_DELAY_SECONDS > 0:
            time.sleep(ADC_START_DELAY_SECONDS)

    fieldnames = [
        "index", "row", "col", "packet", "bits_lsb_first", "output_dir", "ok",
        "decoded_packet", "low_sample_count", "tm_rise_s", "dr_low_width_s",
        "clock_period_s_median", "la_set_mean_uA", "la_set_min_uA", "la_set_max_uA",
        "la_reset_mean_uA", "la_reset_min_uA", "la_reset_max_uA",
        "adc_read_uA", "adc_set_uA", "adc_reset_uA", "error",
    ]
    manifest = []
    capture_started = None
    capture_done = None

    try:
        with automation.Manager.connect(port=base.SALEAE_PORT, connect_timeout_seconds=5) as manager:
            device = base.find_logic_device(manager)
            app_info = manager.get_app_info()
            device_config = automation.LogicDeviceConfiguration(
                enabled_digital_channels=DIGITAL_CHANNELS,
                enabled_analog_channels=ANALOG_CHANNELS,
                digital_sample_rate=DIGITAL_SAMPLE_RATE,
                analog_sample_rate=ANALOG_SAMPLE_RATE,
                digital_threshold_volts=DIGITAL_THRESHOLD_VOLTS,
            )
            capture_config = automation.CaptureConfiguration(
                capture_mode=automation.DigitalTriggerCaptureMode(
                    trigger_channel_index=TRIGGER_CHANNEL_INDEX,
                    trigger_type=digital_trigger_type(),
                    after_trigger_seconds=AFTER_TRIGGER_SECONDS,
                    trim_data_seconds=TRIM_DATA_SECONDS,
                )
            )
            detail = (
                f"SINGLE_CAPTURE cells={len(cells)} start=({START_ROW},{START_COL}) "
                f"trigger=D{TRIGGER_CHANNEL_INDEX} {TRIGGER_TYPE} after={AFTER_TRIGGER_SECONDS}s "
                f"digital_rate={DIGITAL_SAMPLE_RATE} analog_rate={ANALOG_SAMPLE_RATE} "
                f"deterministic_timing={int(FULL_ARRAY_DETERMINISTIC_TIMING)}"
            )
            print(detail, flush=True)
            run_log.write_text(detail + "\n")
            capture_started = time.monotonic()
            with manager.start_capture(
                device_id=device.device_id,
                device_configuration=device_config,
                capture_configuration=capture_config,
            ) as capture:
                print("SINGLE_CAPTURE_ARMED", flush=True)
                capture.wait()
                print("BURST_STAGE Capture complete; exporting waveform data", flush=True)
                export_digital_channels = [] if FULL_ARRAY_DETERMINISTIC_TIMING else DIGITAL_CHANNELS
                capture.export_raw_data_csv(
                    directory=str(trace_dir),
                    digital_channels=export_digital_channels,
                    analog_channels=ANALOG_CHANNELS,
                    analog_downsample_ratio=1,
                )
            capture_done = time.monotonic()
            print("BURST_STAGE Waveform export complete; calculating cell readings", flush=True)

        if FULL_ARRAY_DETERMINISTIC_TIMING:
            decoded_windows = deterministic_packet_windows(cells)
        else:
            decoded_windows = decode_packet_windows(trace_dir / "digital.csv", len(cells))
        analog_windows = window_analog_summaries(trace_dir / "analog.csv", decoded_windows)
        with manifest_csv.open("w", newline="") as manifest_handle:
            writer = csv.DictWriter(manifest_handle, fieldnames=fieldnames)
            writer.writeheader()
            for index, (row, col) in enumerate(cells):
                packet = packet_for(row, col)
                decoded = decoded_windows[index] if index < len(decoded_windows) else {}
                analog = analog_windows[index] if index < len(analog_windows) else {
                    "set_A12_minus_A13_uA": {**stats([]), "units": "uA"},
                    "reset_A14_minus_A15_uA": {**stats([]), "units": "uA"},
                }
                decoded_packet = decoded.get("decoded_packet")
                ok = decoded_packet == packet
                error = ""
                if not decoded:
                    error = "missing decoded packet window"
                elif not ok:
                    error = f"decoded {decoded.get('decoded_packet_hex') or '<none>'} != expected 0x{packet:04x}"
                row_out = {
                    "index": index,
                    "row": row,
                    "col": col,
                    "packet": f"0x{packet:04x}",
                    "bits_lsb_first": lsb_bits(packet),
                    "output_dir": str(trace_dir),
                    "ok": ok,
                    "decoded_packet": decoded.get("decoded_packet_hex", ""),
                    "low_sample_count": decoded.get("low_sample_count", ""),
                    "tm_rise_s": decoded.get("tm_rise_s", ""),
                    "dr_low_width_s": decoded.get("dr_low_width_s", ""),
                    "clock_period_s_median": decoded.get("clock_period_s_median", ""),
                    "la_set_mean_uA": analog["set_A12_minus_A13_uA"]["mean"],
                    "la_set_min_uA": analog["set_A12_minus_A13_uA"]["min"],
                    "la_set_max_uA": analog["set_A12_minus_A13_uA"]["max"],
                    "la_reset_mean_uA": analog["reset_A14_minus_A15_uA"]["mean"],
                    "la_reset_min_uA": analog["reset_A14_minus_A15_uA"]["min"],
                    "la_reset_max_uA": analog["reset_A14_minus_A15_uA"]["max"],
                    "adc_read_uA": "",
                    "adc_set_uA": "",
                    "adc_reset_uA": "",
                    "error": error,
                }
                writer.writerow(row_out)
                manifest.append(row_out)
                if ok:
                    set_mean = row_out["la_set_mean_uA"]
                    reset_mean = row_out["la_reset_mean_uA"]
                    set_text = f"{set_mean:.3f}uA" if set_mean is not None else "n/a"
                    reset_text = f"{reset_mean:.3f}uA" if reset_mean is not None else "n/a"
                    print(
                        f"RESULT index={index:04d} ok=True decoded={row_out['decoded_packet']} "
                        f"la_set_mean={set_text} "
                        f"la_reset_mean={reset_text}",
                        flush=True,
                    )
                else:
                    print(f"ERROR index={index:04d} {error}", flush=True)
                if STOP_ON_MISMATCH and not ok:
                    break

        (trace_dir / "decoded_windows.json").write_text(json.dumps(decoded_windows, indent=2))
        (trace_dir / "analog_windows.json").write_text(json.dumps(analog_windows, indent=2))
        manifest_json.write_text(json.dumps({
            "output_root": str(root),
            "capture_strategy": CAPTURE_STRATEGY,
            "capture_dir": str(trace_dir),
            "rails": rails,
            "start_row": START_ROW,
            "start_col": START_COL,
            "cells_requested": len(cells),
            "cells_captured": len(manifest),
            "measurement_skip_end_cycles": MEASURE_SKIP_END_CYCLES,
            "full_array_deterministic_timing": FULL_ARRAY_DETERMINISTIC_TIMING,
            "fpga_timing": {
                "fpga_reset_assert_cycles": FPGA_RESET_ASSERT_CYCLES,
                "reset_release_fallback_cycles": RESET_RELEASE_FALLBACK_CYCLES,
                "post_reset_wait_cycles": POST_RESET_WAIT_CYCLES,
                "post_dr_tm_hold_cycles": POST_DR_TM_HOLD_CYCLES,
                "repeat_after_done_cycles": REPEAT_AFTER_DONE_CYCLES,
                "wb_clk_period_seconds": WB_CLK_PERIOD_SECONDS,
            },
            "capture_started_monotonic": capture_started,
            "capture_done_monotonic": capture_done,
            "adc_state": adc_state,
            "saleae": {
                "device_id": device.device_id,
                "logic_app_version": app_info.app_version,
                "digital_sample_rate": DIGITAL_SAMPLE_RATE,
                "analog_sample_rate": ANALOG_SAMPLE_RATE,
                "trigger_channel_index": TRIGGER_CHANNEL_INDEX,
                "trigger_type": TRIGGER_TYPE,
                "after_trigger_seconds": AFTER_TRIGGER_SECONDS,
                "trim_data_seconds": TRIM_DATA_SECONDS,
            },
            "manifest": manifest,
        }, indent=2))
    finally:
        stop_event.set()
        if adc_thread is not None:
            adc_thread.join(timeout=2.0)


def main():
    configure_base_globals()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    root = Path.home() / "saleae-api" / "captures" / f"fpga-full-cell-sweep-{stamp}"
    root.mkdir(parents=True, exist_ok=True)
    adc_csv = root / "adc_monitor.csv"
    manifest_csv = root / "manifest.csv"
    manifest_json = root / "manifest.json"
    run_log = root / "run.log"

    cells = sweep_cells()
    rails = {
        "command": "SCAN_SET_RAILS",
        "response": "SKIPPED_BY_ENV",
        "parsed_volts": {},
    } if SKIP_SET_RAILS else base.set_scan_set_rails()
    print(f"OUTPUT_ROOT={root}", flush=True)
    print(f"RAILS {rails['response']}", flush=True)

    if CAPTURE_STRATEGY == "single":
        run_single_capture(root, cells, rails, adc_csv, manifest_csv, manifest_json, run_log)
        print(f"DONE output_root={root} cells={len(cells)}", flush=True)
        return
    if CAPTURE_STRATEGY not in ("per-cell", "per_cell"):
        raise ValueError(f"Unsupported CAPTURE_STRATEGY={CAPTURE_STRATEGY!r}; use single or per-cell")

    stop_event = threading.Event()
    starts = {}
    adc_state = {"enabled": ENABLE_ADC_MONITOR}
    adc_thread = None
    if ENABLE_ADC_MONITOR:
        adc_thread = threading.Thread(target=base.adc_monitor, args=(stop_event, root, starts, adc_state), daemon=True)
        adc_thread.start()
        if ADC_START_DELAY_SECONDS > 0:
            time.sleep(ADC_START_DELAY_SECONDS)

    manifest = []
    fieldnames = [
        "index", "row", "col", "packet", "bits_lsb_first", "output_dir", "ok",
        "decoded_packet", "low_sample_count", "tm_rise_s", "dr_low_width_s",
        "clock_period_s_median", "la_set_mean_uA", "la_set_min_uA", "la_set_max_uA",
        "la_reset_mean_uA", "la_reset_min_uA", "la_reset_max_uA",
        "adc_read_uA", "adc_set_uA", "adc_reset_uA", "error",
    ]

    try:
        with automation.Manager.connect(port=base.SALEAE_PORT, connect_timeout_seconds=5) as manager, \
                manifest_csv.open("w", newline="") as manifest_handle, \
                run_log.open("w") as log_handle:
            device = base.find_logic_device(manager)
            app_info = manager.get_app_info()
            writer = csv.DictWriter(manifest_handle, fieldnames=fieldnames)
            writer.writeheader()
            device_config = automation.LogicDeviceConfiguration(
                enabled_digital_channels=DIGITAL_CHANNELS,
                enabled_analog_channels=ANALOG_CHANNELS,
                digital_sample_rate=DIGITAL_SAMPLE_RATE,
                analog_sample_rate=ANALOG_SAMPLE_RATE,
                digital_threshold_volts=DIGITAL_THRESHOLD_VOLTS,
            )
            capture_config = automation.CaptureConfiguration(
                capture_mode=automation.DigitalTriggerCaptureMode(
                    trigger_channel_index=TRIGGER_CHANNEL_INDEX,
                    trigger_type=digital_trigger_type(),
                    after_trigger_seconds=AFTER_TRIGGER_SECONDS,
                    trim_data_seconds=TRIM_DATA_SECONDS,
                )
            )

            for index, (row, col) in enumerate(cells):
                packet = packet_for(row, col)
                cell_dir = root / f"{index:04d}_r{row:02d}_c{col:02d}_pkt{packet:04x}"
                cell_dir.mkdir()
                detail = (
                    f"MEASURE index={index:04d} row={row:02d} col={col:02d} "
                    f"packet=0x{packet:04x} bits_lsb_first={lsb_bits(packet)}"
                )
                print(detail, flush=True)
                log_handle.write(detail + "\n")
                log_handle.flush()

                try:
                    capture_started = time.monotonic()
                    print(f"ARMING index={index:04d} packet=0x{packet:04x}", flush=True)
                    with manager.start_capture(
                        device_id=device.device_id,
                        device_configuration=device_config,
                        capture_configuration=capture_config,
                    ) as capture:
                        print(f"ARMED index={index:04d} packet=0x{packet:04x}", flush=True)
                        capture.wait()
                        capture.export_raw_data_csv(
                            directory=str(cell_dir),
                            digital_channels=DIGITAL_CHANNELS,
                            analog_channels=ANALOG_CHANNELS,
                            analog_downsample_ratio=1,
                        )
                    capture_done = time.monotonic()

                    digital = base.parse_digital(cell_dir / "digital.csv")
                    events = base.summarize_events(digital, {"reset_skipped": True}, capture_started)
                    decoded = decode_packet(cell_dir / "digital.csv")
                    analog = window_analog_summary(cell_dir / "analog.csv", decoded["dr_rise_s"], decoded["tm_fall_s"])
                    adc_latest = latest_adc_sample(adc_csv) or {}
                    ok = decoded["decoded_packet"] == packet

                    analysis = {
                        "index": index,
                        "row": row,
                        "col": col,
                        "packet": f"0x{packet:04x}",
                        "bits_lsb_first": lsb_bits(packet),
                        "ok": ok,
                        "decoded": {
                            **decoded,
                            "decoded_packet_hex": f"0x{decoded['decoded_packet']:04x}" if decoded["decoded_packet"] is not None else None,
                        },
                        "events": events,
                        "analog_currents": analog,
                        "adc_latest": adc_latest,
                        "capture_started_monotonic": capture_started,
                        "capture_done_monotonic": capture_done,
                        "rails": rails,
                        "saleae": {
                            "logic_app_version": app_info.app_version,
                            "digital_sample_rate": DIGITAL_SAMPLE_RATE,
                            "analog_sample_rate": ANALOG_SAMPLE_RATE,
                            "trigger_channel_index": TRIGGER_CHANNEL_INDEX,
                            "trigger_type": TRIGGER_TYPE,
                            "after_trigger_seconds": AFTER_TRIGGER_SECONDS,
                            "trim_data_seconds": TRIM_DATA_SECONDS,
                        },
                    }
                    (cell_dir / "analysis.json").write_text(json.dumps(analysis, indent=2))

                    row_out = {
                        "index": index,
                        "row": row,
                        "col": col,
                        "packet": f"0x{packet:04x}",
                        "bits_lsb_first": lsb_bits(packet),
                        "output_dir": str(cell_dir),
                        "ok": ok,
                        "decoded_packet": analysis["decoded"]["decoded_packet_hex"],
                        "low_sample_count": decoded["low_sample_count"],
                        "tm_rise_s": decoded["tm_rise_s"],
                        "dr_low_width_s": decoded["dr_low_width_s"],
                        "clock_period_s_median": decoded["clock_period_s_median"],
                        "la_set_mean_uA": analog["set_A12_minus_A13_uA"]["mean"],
                        "la_set_min_uA": analog["set_A12_minus_A13_uA"]["min"],
                        "la_set_max_uA": analog["set_A12_minus_A13_uA"]["max"],
                        "la_reset_mean_uA": analog["reset_A14_minus_A15_uA"]["mean"],
                        "la_reset_min_uA": analog["reset_A14_minus_A15_uA"]["min"],
                        "la_reset_max_uA": analog["reset_A14_minus_A15_uA"]["max"],
                        "adc_read_uA": adc_latest.get("read_uA"),
                        "adc_set_uA": adc_latest.get("set_uA"),
                        "adc_reset_uA": adc_latest.get("reset_uA"),
                        "error": "",
                    }
                except Exception as exc:
                    row_out = {
                        "index": index,
                        "row": row,
                        "col": col,
                        "packet": f"0x{packet:04x}",
                        "bits_lsb_first": lsb_bits(packet),
                        "output_dir": str(cell_dir),
                        "ok": False,
                        "decoded_packet": "",
                        "low_sample_count": "",
                        "tm_rise_s": "",
                        "dr_low_width_s": "",
                        "clock_period_s_median": "",
                        "la_set_mean_uA": "",
                        "la_set_min_uA": "",
                        "la_set_max_uA": "",
                        "la_reset_mean_uA": "",
                        "la_reset_min_uA": "",
                        "la_reset_max_uA": "",
                        "adc_read_uA": "",
                        "adc_set_uA": "",
                        "adc_reset_uA": "",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                    (cell_dir / "analysis_error.json").write_text(json.dumps(row_out, indent=2))
                    print(f"ERROR index={index:04d} {row_out['error']}", flush=True)
                writer.writerow(row_out)
                manifest_handle.flush()
                manifest.append(row_out)
                if row_out["ok"]:
                    print(
                        f"RESULT index={index:04d} ok=True decoded={row_out['decoded_packet']} "
                        f"la_set_mean={row_out['la_set_mean_uA']:.3f}uA "
                        f"la_reset_mean={row_out['la_reset_mean_uA']:.3f}uA",
                        flush=True,
                    )
                if STOP_ON_MISMATCH and not row_out["ok"]:
                    break
    finally:
        stop_event.set()
        if adc_thread is not None:
            adc_thread.join(timeout=2.0)

    manifest_json.write_text(json.dumps({
        "output_root": str(root),
        "rails": rails,
        "start_row": START_ROW,
        "start_col": START_COL,
        "cells_requested": len(cells),
        "cells_captured": len(manifest),
        "adc_state": adc_state,
        "manifest": manifest,
        "saleae": {"device_id": device.device_id, "analog_sample_rate": ANALOG_SAMPLE_RATE},
    }, indent=2))
    print(f"DONE output_root={root} cells={len(manifest)}", flush=True)


if __name__ == "__main__":
    main()
