#!/usr/bin/env python3
import csv
import json
import os
import re
import site
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

for package_dir in (
    "/home/ubuntu-24-04/caravel_venv/lib/python3.14/site-packages",
    "/usr/lib/python3/dist-packages",
):
    site.addsitedir(package_dir)

import serial
from saleae import automation


ADC_DAC_PORT = os.environ.get(
    "ADC_DAC_PORT",
    "/dev/serial/by-id/usb-Teensyduino_USB_Serial_8829000-if00",
)
CARAVEL_FTDI_URL = "ftdi://ftdi:232h/1"
CARAVEL_UTIL_DIR = "/home/ubuntu-24-04/caravel_board/firmware/chipignite/util"
SALEAE_PORT = 10430

DURATION_SECONDS = float(os.environ.get("CAPTURE_DURATION_SECONDS", "0.85"))
PRE_RESET_DELAY_SECONDS = float(os.environ.get("PRE_RESET_DELAY_SECONDS", "0.10"))
RESET_HOLD_SECONDS = float(os.environ.get("RESET_HOLD_SECONDS", "0.12"))
POST_RAIL_DELAY_SECONDS = float(os.environ.get("POST_RAIL_DELAY_SECONDS", "0"))
DIGITAL_CHANNELS = [
    int(value)
    for value in os.environ.get("DIGITAL_CHANNELS", "6,7,8,9,10,11").split(",")
    if value.strip()
]
ANALOG_CHANNELS = [
    int(value)
    for value in os.environ.get("ANALOG_CHANNELS", "0,1,2,3,4,5,12,13,14,15").split(",")
    if value.strip()
]
DIGITAL_SAMPLE_RATE_CANDIDATES = [
    int(value)
    for value in os.environ.get("DIGITAL_SAMPLE_RATES", "50000000,25000000,12500000,6250000").split(",")
    if value.strip()
]
ANALOG_SAMPLE_RATE = int(os.environ.get("ANALOG_SAMPLE_RATE", "31250"))
DIGITAL_THRESHOLD_VOLTS = float(os.environ.get("DIGITAL_THRESHOLD_VOLTS", "1.8"))
RESET_MODE = os.environ.get("RESET_MODE", "hk").strip().lower()
HK_SUDO_PASSWORD = os.environ.get("HK_SUDO_PASSWORD", os.environ.get("UBUNTU_SUDO_PASSWORD", ""))
TRIGGER_CHANNEL = int(os.environ.get("TRIGGER_CHANNEL", "-1"))
TRIGGER_EDGE = os.environ.get("TRIGGER_EDGE", "falling").strip().lower()
AFTER_TRIGGER_SECONDS = float(os.environ.get("AFTER_TRIGGER_SECONDS", "1.2"))
TRIM_DATA_SECONDS_ENV = os.environ.get("TRIM_DATA_SECONDS", "").strip()
TRIM_DATA_SECONDS = float(TRIM_DATA_SECONDS_ENV) if TRIM_DATA_SECONDS_ENV else None
ENABLE_ADC_MONITOR = os.environ.get("ENABLE_ADC_MONITOR", "1").strip().lower() not in ("0", "false", "no")
SKIP_SET_RAILS = os.environ.get("SKIP_SET_RAILS", "0") == "1"
SCAN_REQUEST = os.environ.get("SCAN_REQUEST", "0x0000").strip()
SCAN_RAIL_COMMAND = os.environ.get("SCAN_RAIL_COMMAND", "SCAN_SET_RAILS").strip()
SCAN_REQUEST_TAG = SCAN_REQUEST.lower().replace("0x", "")

SHUNT_OHMS = float(os.environ.get("SHUNT_OHMS", "470.0"))

RAILS_REQUESTED = {
    "Vcc_read_V": 0.0,
    "Vcc_set_V": float(os.environ.get("VCC_SET_V", "1.7")),
    "Vcc_reset_V": 0.0,
    "Vcc_wl_read_V": 0.0,
    "Vcc_wl_set_V": float(os.environ.get("VCC_WL_SET_V", "2.5")),
    "Vcc_wl_reset_V": 0.0,
}

LABELS = {
    "D6": "ready / Caravel GPIO1",
    "D7": "rst_b / Caravel resetb",
    "D8": "wb_clk_i / XCLK",
    "D9": "TM / Caravel GPIO36",
    "D10": "ScanInDL / Caravel GPIO22",
    "D11": "ScanInDR / Caravel GPIO21",
    "A0": "Vcc_set",
    "A1": "Vcc_wl_set",
    "A2": "Vcc_reset",
    "A3": "Vcc_wl_reset",
    "A4": "VDDa1",
    "A5": "VDDc2",
    "A12-A13": "set_shunt current, polarity A12-A13",
    "A14-A15": "reset_shunt current, polarity A14-A15",
}

RAIL_ANALOG_MAP = {
    "Vcc_set_V": 0,
    "Vcc_wl_set_V": 1,
    "Vcc_reset_V": 2,
    "Vcc_wl_reset_V": 3,
    "VDDa1_V": 4,
    "VDDc2_V": 5,
}

RAIL_DONE_RE = re.compile(
    r"(?P<name>vcc_[a-z_]+)_mV=(?P<mv>-?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
ADC_RE = re.compile(
    r"id=0x(?P<id>[0-9A-Fa-f]+).*?"
    r"check=(?P<check>PASS|FAIL).*?"
    r"read_uA=(?P<read>-?\d+(?:\.\d+)?).*?"
    r"set_uA=(?P<set>-?\d+(?:\.\d+)?).*?"
    r"reset_uA=(?P<reset>-?\d+(?:\.\d+)?)"
)


def find_logic_device(manager):
    devices = manager.get_devices(include_simulation_devices=False)
    for device in devices:
        if device.device_type == automation.DeviceType.LOGIC_PRO_16:
            return device
    if devices:
        return devices[0]
    raise RuntimeError("No Saleae device found")


def stats(values):
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


def send_adc_dac_command(command, read_delay=0.5):
    with serial.Serial(ADC_DAC_PORT, 115200, timeout=0.25, write_timeout=1.0) as port:
        time.sleep(0.2)
        port.reset_input_buffer()
        port.write((command + "\n").encode())
        port.flush()
        time.sleep(read_delay)
        return port.read(8192).decode("utf-8", errors="replace")


def set_scan_set_rails():
    text = send_adc_dac_command(SCAN_RAIL_COMMAND, read_delay=0.7)
    parsed = {}
    for match in RAIL_DONE_RE.finditer(text):
        parsed[f"{match.group('name')}_V"] = float(match.group("mv")) / 1000.0
    return {"command": SCAN_RAIL_COMMAND, "response": text.strip(), "parsed_volts": parsed}


def adc_monitor(stop_event, output_dir, starts, adc_state):
    csv_path = output_dir / "adc_monitor.csv"
    log_path = output_dir / "adc_monitor.log"
    fieldnames = [
        "i",
        "t_s",
        "id",
        "check",
        "adc_A2_A3_read_uA",
        "adc_A0_A1_set_uA",
        "adc_A4_A5_reset_uA",
        "raw",
    ]
    try:
        with serial.Serial(ADC_DAC_PORT, 115200, timeout=0.2, write_timeout=1.0) as ser, \
                csv_path.open("w", newline="") as csv_file, \
                log_path.open("w") as log_file:
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            writer.writeheader()
            starts["adc_start_monotonic"] = time.monotonic()
            ser.reset_input_buffer()
            next_query = 0.0
            i = 0
            while not stop_event.is_set():
                now = time.monotonic()
                if now >= next_query:
                    ser.write(b"ADC\n")
                    ser.flush()
                    next_query = now + 0.5
                raw = ser.readline().decode("utf-8", "replace").strip()
                if not raw:
                    continue
                log_file.write(raw + "\n")
                log_file.flush()
                match = ADC_RE.search(raw)
                if not match:
                    continue
                writer.writerow({
                    "i": i,
                    "t_s": f"{time.monotonic() - starts['adc_start_monotonic']:.6f}",
                    "id": match.group("id").upper(),
                    "check": match.group("check"),
                    "adc_A2_A3_read_uA": match.group("read"),
                    "adc_A0_A1_set_uA": match.group("set"),
                    "adc_A4_A5_reset_uA": match.group("reset"),
                    "raw": raw,
                })
                csv_file.flush()
                print(
                    f"ADC i={i} check={match.group('check')} "
                    f"A2_A3_read={float(match.group('read')):.6f}uA "
                    f"A0_A1_set={float(match.group('set')):.6f}uA "
                    f"A4_A5_reset={float(match.group('reset')):.6f}uA",
                    flush=True,
                )
                i += 1
    except Exception as exc:
        adc_state["ok"] = False
        adc_state["error_type"] = type(exc).__name__
        adc_state["error"] = str(exc)
        starts.setdefault("adc_start_monotonic", time.monotonic())
    else:
        adc_state["ok"] = True


def reset_caravel(markers):
    sys.path.insert(0, CARAVEL_UTIL_DIR)
    from caravel.hk import HKSpi

    with HKSpi(ftdi_device=CARAVEL_FTDI_URL, uart_enable_mode=HKSpi.UART_DISABLE) as hk:
        markers["reset_hold_monotonic"] = time.monotonic()
        hk.cpu_reset_hold()
        time.sleep(RESET_HOLD_SECONDS)
        markers["reset_release_monotonic"] = time.monotonic()
        hk.cpu_reset_release()


def reset_caravel_sudo_subprocess(markers):
    if not HK_SUDO_PASSWORD:
        raise RuntimeError("HK_SUDO_PASSWORD or UBUNTU_SUDO_PASSWORD is required for RESET_MODE=hk_sudo_subprocess")
    code = (
        "import sys,time; "
        "sys.path.insert(0, '/home/ubuntu-24-04/caravel_board/firmware/chipignite/util'); "
        "from caravel.hk import HKSpi; "
        "hk=HKSpi(ftdi_device='ftdi://ftdi:232h/1', uart_enable_mode=HKSpi.UART_DISABLE); "
        "hk.__enter__(); "
        f"hk.cpu_reset_hold(); time.sleep({RESET_HOLD_SECONDS!r}); "
        "hk.cpu_reset_release(); "
        "hk.__exit__(None,None,None); "
        "print('HK_RESET_DONE')"
    )
    markers["reset_hold_monotonic"] = time.monotonic()
    proc = subprocess.run(
        ["sudo", "-S", "/home/ubuntu-24-04/caravel_venv/bin/python", "-c", code],
        input=HK_SUDO_PASSWORD + "\n",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=max(10.0, RESET_HOLD_SECONDS + 5.0),
    )
    markers["reset_release_monotonic"] = time.monotonic()
    markers["reset_subprocess_stdout"] = proc.stdout.strip()
    if proc.returncode != 0:
        stderr = "\n".join(proc.stderr.strip().splitlines()[-6:])
        raise RuntimeError(f"HK sudo reset failed with code {proc.returncode}: {stderr}")


def first_edge(edges, channel, from_value=None, to_value=None, after=None):
    for edge in edges.get(channel, []):
        if from_value is not None and edge["from"] != from_value:
            continue
        if to_value is not None and edge["to"] != to_value:
            continue
        if after is not None and edge["t_s"] <= after:
            continue
        return edge
    return None


def parse_digital(path):
    channel_fields = [f"Channel {channel}" for channel in DIGITAL_CHANNELS]
    channel_names = {f"Channel {channel}": f"D{channel}" for channel in DIGITAL_CHANNELS}
    edges = {f"D{channel}": [] for channel in DIGITAL_CHANNELS}
    edge_counts = {f"D{channel}": 0 for channel in DIGITAL_CHANNELS}
    values_seen = {f"D{channel}": set() for channel in DIGITAL_CHANNELS}
    clock_rising = []
    first_values = {}
    last_values = {}
    rows = 0
    last_time = None

    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        previous = None
        for row in reader:
            rows += 1
            t_s = float(row["Time [s]"])
            last_time = t_s
            current = {}
            for field in channel_fields:
                if field not in row:
                    continue
                name = channel_names[field]
                value = int(row[field])
                current[name] = value
                values_seen[name].add(value)
                last_values[name] = value
                if name not in first_values:
                    first_values[name] = value
            if previous is None:
                previous = current
                continue
            for name, value in current.items():
                prior = previous.get(name)
                if prior is None:
                    continue
                if value != prior:
                    edge_counts[name] += 1
                    if len(edges[name]) < 400:
                        edges[name].append({"t_s": t_s, "from": prior, "to": value})
                    if name == "D8" and prior == 0 and value == 1:
                        clock_rising.append(t_s)
            previous = current

    channels = {}
    for name in sorted(values_seen, key=lambda item: int(item[1:])):
        channels[name] = {
            "first": first_values.get(name),
            "last": last_values.get(name),
            "values_seen": sorted(values_seen[name]),
            "edge_count": edge_counts[name],
            "stored_edges": edges[name],
        }
    return {
        "rows": rows,
        "last_time_s": last_time,
        "channels": channels,
        "edges": edges,
        "clock_rising_times": clock_rising,
    }


def summarize_events(digital, markers, saleae_start):
    edges = digital["edges"]
    reset_fall = first_edge(edges, "D7", 1, 0)
    reset_rise = first_edge(edges, "D7", 0, 1, after=reset_fall["t_s"] if reset_fall else None)
    if reset_rise is None:
        reset_rise = first_edge(edges, "D7", 0, 1)
    ready_fall = first_edge(edges, "D6", 1, 0, after=reset_fall["t_s"] if reset_fall else None)
    ready_rise = first_edge(edges, "D6", 0, 1, after=ready_fall["t_s"] if ready_fall else (reset_rise["t_s"] if reset_rise else None))
    if ready_rise is None:
        ready_rise = first_edge(edges, "D6", 0, 1)
    tm_rise = first_edge(edges, "D9", 0, 1, after=ready_rise["t_s"] if ready_rise else None)
    if tm_rise is None:
        tm_rise = first_edge(edges, "D9", 0, 1)
    dr_fall = first_edge(edges, "D11", 1, 0, after=tm_rise["t_s"] if tm_rise else None)
    dr_rise = first_edge(edges, "D11", 0, 1, after=dr_fall["t_s"] if dr_fall else None)
    dl_first_edge = edges.get("D10", [None])[0] if edges.get("D10") else None

    clock_rising = digital["clock_rising_times"]
    periods = []
    if dr_fall:
        near = [t for t in clock_rising if dr_fall["t_s"] - 10e-6 <= t <= dr_fall["t_s"] + 20e-6]
        periods = [b - a for a, b in zip(near, near[1:])]
    if not periods and len(clock_rising) > 2:
        periods = [b - a for a, b in zip(clock_rising[:200], clock_rising[1:201])]
    clock_period_s = statistics.median(periods) if periods else None

    reset_hold_s = None
    reset_release_s = None
    if saleae_start is not None:
        if "reset_hold_monotonic" in markers:
            reset_hold_s = markers["reset_hold_monotonic"] - saleae_start
        if "reset_release_monotonic" in markers:
            reset_release_s = markers["reset_release_monotonic"] - saleae_start

    events = {
        "software_reset_hold_s": reset_hold_s,
        "software_reset_release_s": reset_release_s,
        "D7_reset_fall": reset_fall,
        "D7_reset_rise": reset_rise,
        "D6_ready_fall": ready_fall,
        "D6_ready_rise": ready_rise,
        "D9_TM_rise": tm_rise,
        "D11_ScanInDR_fall": dr_fall,
        "D11_ScanInDR_rise": dr_rise,
        "D10_ScanInDL_first_edge": dl_first_edge,
        "D8_clock_edge_count": digital["channels"].get("D8", {}).get("edge_count"),
        "D8_clock_period_s_median_near_scan": round(clock_period_s, 12) if clock_period_s else None,
    }
    if reset_rise and ready_rise:
        events["ready_rise_after_reset_s"] = round(ready_rise["t_s"] - reset_rise["t_s"], 9)
    if ready_rise and tm_rise:
        events["tm_rise_after_ready_s"] = round(tm_rise["t_s"] - ready_rise["t_s"], 9)
    if tm_rise and dr_fall:
        delta = dr_fall["t_s"] - tm_rise["t_s"]
        events["tm_rise_to_scanindr_fall_s"] = round(delta, 12)
        if clock_period_s:
            events["tm_rise_to_scanindr_fall_cycles"] = round(delta / clock_period_s, 3)
    if dr_fall and dr_rise:
        width = dr_rise["t_s"] - dr_fall["t_s"]
        events["scanindr_low_width_s"] = round(width, 12)
        if clock_period_s:
            events["scanindr_low_width_cycles"] = round(width / clock_period_s, 3)
    return events


def analog_summary(path):
    if not path.exists():
        return {"ok": False, "error": "analog.csv missing"}

    values = {f"A{channel}": [] for channel in ANALOG_CHANNELS}
    set_current = []
    reset_current = []
    rows = 0
    last_time = None

    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rows += 1
            last_time = float(row["Time [s]"])
            for channel in ANALOG_CHANNELS:
                field = f"Channel {channel}"
                if field not in row:
                    continue
                try:
                    values[f"A{channel}"].append(float(row[field]))
                except ValueError:
                    pass
            try:
                set_current.append((float(row["Channel 12"]) - float(row["Channel 13"])) / SHUNT_OHMS * 1e6)
            except (KeyError, ValueError):
                pass
            try:
                reset_current.append((float(row["Channel 14"]) - float(row["Channel 15"])) / SHUNT_OHMS * 1e6)
            except (KeyError, ValueError):
                pass

    rail_measured = {}
    for rail, channel in RAIL_ANALOG_MAP.items():
        key = f"A{channel}"
        rail_measured[rail] = stats(values.get(key, []))
        rail_measured[rail]["source"] = key
        rail_measured[rail]["units"] = "V"

    return {
        "ok": True,
        "rows": rows,
        "last_time_s": last_time,
        "analog_channels": {name: {**stats(vals), "units": "V"} for name, vals in values.items()},
        "rail_measured": rail_measured,
        "currents": {
            "set_A12_minus_A13_uA": {**stats(set_current), "units": "uA", "shunt_ohms": SHUNT_OHMS},
            "reset_A14_minus_A15_uA": {**stats(reset_current), "units": "uA", "shunt_ohms": SHUNT_OHMS},
        },
    }


def adc_summary(path):
    if not path.exists():
        return {"ok": False, "error": "adc_monitor.csv missing"}
    channels = {
        "adc_A2_A3_read_uA": [],
        "adc_A0_A1_set_uA": [],
        "adc_A4_A5_reset_uA": [],
    }
    rows = 0
    last_time = None
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rows += 1
            try:
                last_time = float(row["t_s"])
            except (KeyError, ValueError):
                pass
            if row.get("check") != "PASS":
                continue
            for name in channels:
                try:
                    channels[name].append(float(row[name]))
                except (KeyError, TypeError, ValueError):
                    pass
    return {
        "ok": True,
        "rows": rows,
        "last_time_s": last_time,
        "channels": {name: {**stats(values), "units": "uA"} for name, values in channels.items()},
    }


def write_edges_csv(path, digital):
    rows = []
    for channel, edges in digital.get("edges", {}).items():
        for edge in edges:
            rows.append((edge["t_s"], channel, edge["from"], edge["to"]))
    rows.sort()
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["t_s", "channel", "from", "to"])
        writer.writerows(rows)


def run_capture(output_dir):
    stop_event = threading.Event()
    starts = {}
    markers = {}
    adc_state = {"enabled": ENABLE_ADC_MONITOR}
    adc_thread = None
    if ENABLE_ADC_MONITOR:
        adc_thread = threading.Thread(target=adc_monitor, args=(stop_event, output_dir, starts, adc_state), daemon=True)
        adc_thread.start()
        time.sleep(0.25)

    last_error = None
    for digital_sample_rate in DIGITAL_SAMPLE_RATE_CANDIDATES:
        try:
            with automation.Manager.connect(port=SALEAE_PORT, connect_timeout_seconds=5) as manager:
                device = find_logic_device(manager)
                app_info = manager.get_app_info()
                device_config = automation.LogicDeviceConfiguration(
                    enabled_digital_channels=DIGITAL_CHANNELS,
                    enabled_analog_channels=ANALOG_CHANNELS,
                    digital_sample_rate=digital_sample_rate,
                    analog_sample_rate=ANALOG_SAMPLE_RATE if ANALOG_CHANNELS else None,
                    digital_threshold_volts=DIGITAL_THRESHOLD_VOLTS,
                )
                if TRIGGER_CHANNEL >= 0:
                    if TRIGGER_EDGE == "falling":
                        trigger_type = automation.DigitalTriggerType.FALLING
                    elif TRIGGER_EDGE == "rising":
                        trigger_type = automation.DigitalTriggerType.RISING
                    else:
                        raise RuntimeError(f"Unsupported TRIGGER_EDGE={TRIGGER_EDGE!r}")
                    capture_mode = automation.DigitalTriggerCaptureMode(
                        trigger_channel_index=TRIGGER_CHANNEL,
                        trigger_type=trigger_type,
                        after_trigger_seconds=AFTER_TRIGGER_SECONDS,
                        trim_data_seconds=TRIM_DATA_SECONDS,
                    )
                else:
                    capture_mode = automation.TimedCaptureMode(duration_seconds=DURATION_SECONDS)
                capture_config = automation.CaptureConfiguration(capture_mode=capture_mode)
                saleae_start = time.monotonic()
                starts["saleae_start_monotonic"] = saleae_start
                with manager.start_capture(
                    device_id=device.device_id,
                    device_configuration=device_config,
                    capture_configuration=capture_config,
                ) as capture:
                    print(
                        f"SALEAE_ARMED digital_rate={digital_sample_rate} "
                        f"analog_rate={ANALOG_SAMPLE_RATE} duration={DURATION_SECONDS} "
                        f"reset_mode={RESET_MODE} trigger_channel={TRIGGER_CHANNEL} "
                        f"trigger_edge={TRIGGER_EDGE}",
                        flush=True,
                    )
                    time.sleep(PRE_RESET_DELAY_SECONDS)
                    if RESET_MODE == "hk":
                        reset_caravel(markers)
                    elif RESET_MODE in ("hk_sudo", "sudo_hk", "hk_sudo_subprocess"):
                        reset_caravel_sudo_subprocess(markers)
                    elif RESET_MODE in ("manual", "none"):
                        markers["reset_skipped"] = True
                        markers["reset_skip_reason"] = f"RESET_MODE={RESET_MODE}"
                        markers["manual_reset_window_monotonic"] = time.monotonic()
                    else:
                        raise RuntimeError(f"Unsupported RESET_MODE={RESET_MODE!r}")
                    capture.wait()
                    capture.export_raw_data_csv(
                        directory=str(output_dir),
                        digital_channels=DIGITAL_CHANNELS,
                        analog_channels=ANALOG_CHANNELS,
                        analog_downsample_ratio=1,
                    )
                    capture_path = output_dir / f"fpga_scan{SCAN_REQUEST_TAG}_la12_15.sal"
                    capture.save_capture(filepath=str(capture_path))
                stop_event.set()
                if adc_thread is not None:
                    adc_thread.join(timeout=2.0)
                return {
                    "logic_app_version": app_info.app_version,
                    "automation_api_version": (
                        f"{app_info.api_version.major}."
                        f"{app_info.api_version.minor}."
                        f"{app_info.api_version.patch}"
                    ),
                    "device_id": device.device_id,
                    "digital_sample_rate": digital_sample_rate,
                    "analog_sample_rate": ANALOG_SAMPLE_RATE,
                    "capture": str(capture_path),
                    "saleae_start_monotonic": saleae_start,
                    "starts": starts,
                    "markers": markers,
                    "adc_state": adc_state,
                }
        except Exception as exc:
            last_error = exc
            print(f"CAPTURE_ATTEMPT_FAILED digital_rate={digital_sample_rate}: {type(exc).__name__}: {exc}", flush=True)
    stop_event.set()
    if adc_thread is not None:
        adc_thread.join(timeout=2.0)
    raise last_error if last_error else RuntimeError("Capture did not run")


def main():
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_dir = Path.home() / "saleae-api" / "captures" / f"fpga-scan{SCAN_REQUEST_TAG}-la12-15-{stamp}"
    output_dir.mkdir(parents=True, exist_ok=True)

    metadata = {
        "ok": False,
        "scan_request": SCAN_REQUEST,
        "rails_requested": RAILS_REQUESTED,
        "fpga_expected": {
            "scan_word": SCAN_REQUEST,
            "updates_outputs_on": "falling edge of wb_clk_i",
            "caravel_samples_on": "rising edge of wb_clk_i",
            "post_reset_wait_cycles": 128,
            "tm_setup_cycles": 0,
            "final_hold_state": "TM=1, ScanInDR=1, ScanInDL=0, ScanInCC=0",
        },
        "labels": LABELS,
        "digital_channels": DIGITAL_CHANNELS,
        "analog_channels": ANALOG_CHANNELS,
        "duration_seconds": DURATION_SECONDS,
        "pre_reset_delay_seconds": PRE_RESET_DELAY_SECONDS,
        "reset_hold_seconds": RESET_HOLD_SECONDS,
        "post_rail_delay_seconds": POST_RAIL_DELAY_SECONDS,
        "reset_mode": RESET_MODE,
        "trigger_channel": TRIGGER_CHANNEL,
        "trigger_edge": TRIGGER_EDGE,
        "after_trigger_seconds": AFTER_TRIGGER_SECONDS,
        "trim_data_seconds": TRIM_DATA_SECONDS,
        "enable_adc_monitor": ENABLE_ADC_MONITOR,
        "output_dir": str(output_dir),
    }

    try:
        rails = {
            "command": "FPGA_DAC81416",
            "response": "SKIPPED_BY_ENV",
            "parsed_volts": {
                "Vcc_set_V": RAILS_REQUESTED["Vcc_set_V"],
                "Vcc_wl_set_V": RAILS_REQUESTED["Vcc_wl_set_V"],
            },
        } if SKIP_SET_RAILS else set_scan_set_rails()
        if POST_RAIL_DELAY_SECONDS > 0:
            time.sleep(POST_RAIL_DELAY_SECONDS)
        capture_info = run_capture(output_dir)
        digital = parse_digital(output_dir / "digital.csv")
        analog = analog_summary(output_dir / "analog.csv") if ANALOG_CHANNELS else {"ok": True, "skipped": True}
        adc = adc_summary(output_dir / "adc_monitor.csv") if ENABLE_ADC_MONITOR else {"ok": True, "skipped": True}
        events = summarize_events(
            digital,
            capture_info.get("markers", {}),
            capture_info.get("saleae_start_monotonic"),
        )
        digital_for_json = dict(digital)
        digital_for_json.pop("clock_rising_times", None)
        write_edges_csv(output_dir / "edges.csv", digital_for_json)
        metadata.update({
            "ok": True,
            "rail_set_command": rails,
            **capture_info,
            "digital": digital_for_json,
            "analog": analog,
            "adc": adc,
            "events": events,
        })
    except Exception as exc:
        metadata.update({"ok": False, "error_type": type(exc).__name__, "error": str(exc)})
        (output_dir / "analysis.json").write_text(json.dumps(metadata, indent=2) + "\n")
        print(json.dumps(metadata, indent=2), flush=True)
        raise

    (output_dir / "analysis.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps(metadata, indent=2), flush=True)
    print(f"OUTPUT_DIR={output_dir}", flush=True)


if __name__ == "__main__":
    main()
