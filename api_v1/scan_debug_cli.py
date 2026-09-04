#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

try:
    from .cell_api import (
        RESET_PROGRAM_VCC_SET_V,
        RESET_PROGRAM_VCC_SET_SWEEP_V,
        RESET_PROGRAM_VCC_WL_V,
        SET_PROGRAM_VCC_SET_V,
        SET_PROGRAM_VCC_WL_V,
        RailVoltages,
        ScanDebugCellAPI,
        ScanDebugConfig,
        SweepConfig,
    )
except ImportError:
    from cell_api import (
        RESET_PROGRAM_VCC_SET_V,
        RESET_PROGRAM_VCC_SET_SWEEP_V,
        RESET_PROGRAM_VCC_WL_V,
        SET_PROGRAM_VCC_SET_V,
        SET_PROGRAM_VCC_WL_V,
        RailVoltages,
        ScanDebugCellAPI,
        ScanDebugConfig,
        SweepConfig,
    )


def parse_sweep(text: str) -> tuple[float, ...]:
    """Parse `0.5:2.0:0.1` or `2.0,2.3,2.4` into voltages."""

    if ":" in text:
        start, stop, step = [float(part) for part in text.split(":")]
        values = []
        value = start
        while value <= stop + (step / 2):
            values.append(round(value, 6))
            value += step
        return tuple(values)
    return tuple(float(part) for part in text.split(",") if part)


def apply_saved_experiment_defaults(args, argv, path=None):
    path = path or Path(__file__).resolve().parent / "experiment_profiles.json"
    if not path.exists():
        return
    explicit = {token.split("=", 1)[0] for token in argv if token.startswith("--")}
    allowed = {"set_threshold", "reset_threshold", "set_vcc_set", "reset_vcc_set", "confirm_reads"}
    for profile in json.loads(path.read_text()).get("profiles", []):
        if (args.operation, args.row, args.col) != (profile["operation"], profile["row"], profile["col"]):
            continue
        for key, value in profile["defaults"].items():
            if key not in allowed:
                raise ValueError(f"Unsupported experiment default: {key}")
            if "--" + key.replace("_", "-") not in explicit:
                setattr(args, key, value)


def build_config(args: argparse.Namespace) -> ScanDebugConfig:
    return ScanDebugConfig(
        run_dir=Path(args.run_dir),
        dry_run=args.dry_run,
        attempts=args.attempts,
        read_feedback_attempts=args.read_feedback_attempts,
        read_rails=RailVoltages(args.read_vcc_set, args.read_vcc_wl_set),
        shunt_ohms=args.shunt_ohms,
        read_calibration_path=Path(args.read_calibration) if args.read_calibration else None,
        zynq_host=args.zynq_host or None,
        zynq_password=args.zynq_password or None,
        zynq_os=args.zynq_os,
        zynq_dir=args.zynq_dir,
        vivado_cmd=args.vivado_cmd,
        saleae_host=args.saleae_host or None,
        saleae_dir=args.saleae_dir,
        saleae_restart_script=args.saleae_restart_script,
        saleae_restart_wait_seconds=args.saleae_restart_wait_seconds,
        saleae_usb_recovery_enabled=not args.disable_saleae_usb_recovery,
        saleae_usb_controller_pci=args.saleae_usb_controller_pci,
        saleae_sudo_password=args.saleae_sudo_password or None,
        adc_dac_port=args.adc_dac_port,
        fpga_dac_enabled=not args.legacy_teensy_dac,
        persistent_fpga_runtime=not args.disable_persistent_fpga_runtime,
        capture_program_pulses=args.capture_program_pulses,
        enable_adc_monitor=args.legacy_teensy_dac,
        dac_teensy_reflash_enabled=args.legacy_teensy_dac and not args.disable_dac_teensy_reflash,
        dac_teensy_app_serial=args.dac_teensy_app_serial,
        dac_teensy_bootloader_serial=args.dac_teensy_bootloader_serial,
        dac_teensy_loader=args.dac_teensy_loader,
        dac_teensy_mcu=args.dac_teensy_mcu,
        dac_teensy_hex=args.dac_teensy_hex,
        hardware_queue_enabled=not args.disable_hardware_queue,
        hardware_queue_host=args.hardware_queue_host or None,
        hardware_queue_dir=args.hardware_queue_dir,
        hardware_queue_timeout_seconds=args.hardware_queue_timeout_seconds,
        hardware_queue_poll_seconds=args.hardware_queue_poll_seconds,
        hardware_queue_stale_seconds=args.hardware_queue_stale_seconds,
        burst_initial_delay_cycles=args.burst_initial_delay_cycles,
        burst_repeat_after_done_cycles=args.burst_repeat_cycles,
        burst_capture_strategy=args.burst_capture_strategy,
        burst_post_dr_tm_hold_cycles=args.burst_post_dr_tm_hold_cycles,
        burst_fpga_reset_assert_cycles=args.burst_fpga_reset_assert_cycles,
        burst_reset_release_fallback_cycles=args.burst_reset_release_fallback_cycles,
        burst_post_reset_wait_cycles=args.burst_post_reset_wait_cycles,
        burst_wb_clk_period_seconds=args.burst_wb_clk_period_seconds,
        burst_single_capture_margin_seconds=args.burst_single_capture_margin_seconds,
        burst_analog_sample_rate=args.burst_analog_sample_rate,
        full_array_burst_digital_sample_rate=args.full_array_burst_digital_sample_rate,
        full_array_burst_analog_sample_rate=args.full_array_burst_analog_sample_rate,
        full_array_burst_capture_timeout_seconds=args.full_array_burst_capture_timeout_seconds,
        full_array_burst_packet_period_seconds=args.full_array_burst_packet_period_seconds,
        burst_capture_timeout_seconds=args.burst_capture_timeout_seconds,
        set_sweep=SweepConfig.from_ranges(
            vcc_set_v=parse_sweep(args.set_vcc_set),
            vcc_wl_set_v=parse_sweep(args.set_vcc_wl_set),
            threshold_uA=args.set_threshold,
            direction="above",
            confirm_reads=args.confirm_reads,
        ),
        reset_sweep=SweepConfig.from_ranges(
            vcc_set_v=parse_sweep(args.reset_vcc_set),
            vcc_wl_set_v=parse_sweep(args.reset_vcc_wl_set),
            threshold_uA=args.reset_threshold,
            direction="below",
            confirm_reads=args.confirm_reads,
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan-debug cell read/set/reset API CLI")
    parser.add_argument(
        "operation",
        choices=[
            "read",
            "set",
            "reset",
            "cycle",
            "read-array",
            "build-runtime-bitstream",
            "build-array-bitstreams",
        ],
    )
    parser.add_argument("--row", type=int)
    parser.add_argument("--col", type=int, default=0)
    parser.add_argument("--row-start", type=int, default=0)
    parser.add_argument("--row-end", type=int, default=31)
    parser.add_argument("--col-start", type=int, default=0)
    parser.add_argument("--col-end", type=int, default=31)
    parser.add_argument("--array-mode", choices=["burst", "burst-columns", "serial"], default="burst-columns")
    parser.add_argument("--force-bitstreams", action="store_true")
    parser.add_argument("--run-dir", default=f"api_v1/runs/run_{time.strftime('%Y%m%d_%H%M%S_IST')}")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--attempts", type=int, default=int(os.environ.get("SCAN_DEBUG_ATTEMPTS", "3")))
    parser.add_argument("--read-feedback-attempts", type=int, choices=(1,2,3), default=3,
                        help="Bounded fresh READ attempts for invalid feedback; never repeats a SET/RESET pulse")
    parser.add_argument("--confirm-reads", type=int, default=10)
    parser.add_argument("--shunt-ohms", type=float, default=470.0)

    parser.add_argument("--read-vcc-set", type=float, default=0.5)
    parser.add_argument("--read-calibration", default=str(Path(__file__).resolve().parent / "calibration/read_offset_A25E1BAA6577FA4D_0p5V.json"),
                        help="Device/read-voltage-specific offset profile; pass an empty value for raw diagnostics")
    parser.add_argument("--read-vcc-wl-set", type=float, default=2.5)
    parser.add_argument("--set-vcc-set", default=str(SET_PROGRAM_VCC_SET_V))
    parser.add_argument("--set-vcc-wl-set", default=",".join(str(value) for value in SET_PROGRAM_VCC_WL_V))
    parser.add_argument(
        "--reset-vcc-set",
        default=",".join(str(value) for value in RESET_PROGRAM_VCC_SET_SWEEP_V),
    )
    parser.add_argument("--reset-vcc-wl-set", default=",".join(str(value) for value in RESET_PROGRAM_VCC_WL_V))
    parser.add_argument("--set-threshold", type=float, default=70.0)
    parser.add_argument("--reset-threshold", type=float, default=5.0)

    parser.add_argument("--zynq-host", default=os.environ.get("SCAN_DEBUG_ZYNQ_HOST", "geethika@100.116.216.70"))
    parser.add_argument("--zynq-password", default=os.environ.get("SCAN_DEBUG_ZYNQ_PASSWORD", ""))
    parser.add_argument("--zynq-os", choices=["windows", "posix"], default=os.environ.get("SCAN_DEBUG_ZYNQ_OS", "windows"))
    parser.add_argument("--zynq-dir", default=os.environ.get("SCAN_DEBUG_ZYNQ_DIR", "C:/Users/geethika/zynq_scan_debug"))
    parser.add_argument("--vivado-cmd", default=os.environ.get("SCAN_DEBUG_VIVADO_CMD", "C:/Xilinx/Vivado/2019.1/bin/vivado.bat"))
    parser.add_argument("--saleae-host", default=os.environ.get("SCAN_DEBUG_SALEAE_HOST", "ubuntu-24-04@100.98.132.51"))
    parser.add_argument("--saleae-dir", default=os.environ.get("SCAN_DEBUG_SALEAE_DIR", "/home/ubuntu-24-04/saleae-api"))
    parser.add_argument("--saleae-restart-script", default=os.environ.get("SCAN_DEBUG_SALEAE_RESTART_SCRIPT", "./start-logic2-automation.sh"))
    parser.add_argument("--saleae-restart-wait-seconds", type=float, default=float(os.environ.get("SCAN_DEBUG_SALEAE_RESTART_WAIT_SECONDS", "10")))
    parser.add_argument("--disable-saleae-usb-recovery", action="store_true", default=os.environ.get("SCAN_DEBUG_DISABLE_SALEAE_USB_RECOVERY", "0") == "1")
    parser.add_argument("--saleae-usb-controller-pci", default=os.environ.get("SCAN_DEBUG_SALEAE_USB_CONTROLLER_PCI", "0000:00:0c.0"))
    parser.add_argument("--saleae-sudo-password", default=os.environ.get("SCAN_DEBUG_SALEAE_SUDO_PASSWORD", ""))
    parser.add_argument("--burst-initial-delay-cycles", type=int, default=int(os.environ.get("SCAN_DEBUG_BURST_INITIAL_DELAY_CYCLES", "1000000")))
    parser.add_argument("--burst-repeat-cycles", type=int, default=int(os.environ.get("SCAN_DEBUG_BURST_REPEAT_CYCLES", "1")))
    parser.add_argument(
        "--burst-capture-strategy",
        choices=["single", "per-cell"],
        default=os.environ.get("SCAN_DEBUG_BURST_CAPTURE_STRATEGY", "single"),
        help="single captures one continuous Saleae trace per burst; per-cell keeps the old rearm/export loop",
    )
    parser.add_argument("--burst-post-dr-tm-hold-cycles", type=int, default=int(os.environ.get("SCAN_DEBUG_BURST_POST_DR_TM_HOLD_CYCLES", "100")))
    parser.add_argument("--burst-fpga-reset-assert-cycles", type=int, default=int(os.environ.get("SCAN_DEBUG_BURST_FPGA_RESET_ASSERT_CYCLES", "24000")))
    parser.add_argument("--burst-reset-release-fallback-cycles", type=int, default=int(os.environ.get("SCAN_DEBUG_BURST_RESET_RELEASE_FALLBACK_CYCLES", "2000")))
    parser.add_argument("--burst-post-reset-wait-cycles", type=int, default=int(os.environ.get("SCAN_DEBUG_BURST_POST_RESET_WAIT_CYCLES", "128")))
    parser.add_argument("--burst-wb-clk-period-seconds", type=float, default=float(os.environ.get("SCAN_DEBUG_BURST_WB_CLK_PERIOD_SECONDS", "0.0000005")))
    parser.add_argument("--burst-single-capture-margin-seconds", type=float, default=float(os.environ.get("SCAN_DEBUG_BURST_SINGLE_CAPTURE_MARGIN_SECONDS", "0.25")))
    parser.add_argument("--burst-analog-sample-rate", type=int, default=int(os.environ.get("SCAN_DEBUG_BURST_ANALOG_SAMPLE_RATE", "3125000")))
    parser.add_argument("--full-array-burst-digital-sample-rate", type=int, default=int(os.environ.get("SCAN_DEBUG_FULL_ARRAY_BURST_DIGITAL_SAMPLE_RATE", "6250000")))
    parser.add_argument("--full-array-burst-analog-sample-rate", type=int, default=int(os.environ.get("SCAN_DEBUG_FULL_ARRAY_BURST_ANALOG_SAMPLE_RATE", "31250")))
    parser.add_argument("--full-array-burst-packet-period-seconds", type=float, default=float(os.environ.get("SCAN_DEBUG_FULL_ARRAY_BURST_PACKET_PERIOD_SECONDS", "0.01312428")))
    parser.add_argument(
        "--full-array-burst-capture-timeout-seconds",
        type=float,
        default=float(os.environ.get("SCAN_DEBUG_FULL_ARRAY_BURST_CAPTURE_TIMEOUT_SECONDS", "900")),
    )
    parser.add_argument(
        "--burst-capture-timeout-seconds",
        type=float,
        default=float(os.environ.get("SCAN_DEBUG_BURST_CAPTURE_TIMEOUT_SECONDS", "420")),
    )
    parser.add_argument(
        "--adc-dac-port",
        default=os.environ.get("SCAN_DEBUG_ADC_DAC_PORT", "/dev/serial/by-id/usb-Teensyduino_USB_Serial_8829000-if00"),
    )
    parser.add_argument(
        "--legacy-teensy-dac",
        action="store_true",
        default=os.environ.get("SCAN_DEBUG_LEGACY_TEENSY_DAC", "0") == "1",
        help="use the legacy USB Teensy for DAC rails and ADC monitoring instead of FPGA DAC control",
    )
    parser.add_argument(
        "--disable-persistent-fpga-runtime",
        action="store_true",
        help="fall back to starting a Vivado batch process for every FPGA command",
    )
    parser.add_argument(
        "--capture-program-pulses",
        action="store_true",
        help="capture set/reset pulse waveforms as well as their verification reads",
    )
    parser.add_argument("--disable-dac-teensy-reflash", action="store_true", default=os.environ.get("SCAN_DEBUG_DISABLE_DAC_TEENSY_REFLASH", "0") == "1")
    parser.add_argument("--dac-teensy-app-serial", default=os.environ.get("SCAN_DEBUG_DAC_TEENSY_APP_SERIAL", "8829000"))
    parser.add_argument("--dac-teensy-bootloader-serial", default=os.environ.get("SCAN_DEBUG_DAC_TEENSY_BOOTLOADER_SERIAL", "000D78D4"))
    parser.add_argument("--dac-teensy-loader", default=os.environ.get("SCAN_DEBUG_DAC_TEENSY_LOADER", "/home/ubuntu-24-04/teensy-tools-src/teensy_loader_cli_serial/teensy_loader_cli"))
    parser.add_argument("--dac-teensy-mcu", default=os.environ.get("SCAN_DEBUG_DAC_TEENSY_MCU", "TEENSY41"))
    parser.add_argument("--dac-teensy-hex", default=os.environ.get("SCAN_DEBUG_DAC_TEENSY_HEX", "/home/ubuntu-24-04/teensy-flash/build-DAC_analog_vltgs/DAC_analog_vltgs.ino.hex"))
    parser.add_argument("--disable-hardware-queue", action="store_true", default=os.environ.get("SCAN_DEBUG_DISABLE_HARDWARE_QUEUE", "0") == "1")
    parser.add_argument("--hardware-queue-host", default=os.environ.get("SCAN_DEBUG_HARDWARE_QUEUE_HOST", ""))
    parser.add_argument("--hardware-queue-dir", default=os.environ.get("SCAN_DEBUG_HARDWARE_QUEUE_DIR", "/tmp/scan_debug_hardware_queue.lock"))
    parser.add_argument("--hardware-queue-timeout-seconds", type=float, default=float(os.environ.get("SCAN_DEBUG_HARDWARE_QUEUE_TIMEOUT_SECONDS", "86400")))
    parser.add_argument("--hardware-queue-poll-seconds", type=float, default=float(os.environ.get("SCAN_DEBUG_HARDWARE_QUEUE_POLL_SECONDS", "5")))
    parser.add_argument("--hardware-queue-stale-seconds", type=float, default=float(os.environ.get("SCAN_DEBUG_HARDWARE_QUEUE_STALE_SECONDS", "43200")))
    args = parser.parse_args()
    apply_saved_experiment_defaults(args, sys.argv[1:])
    if args.operation not in {"read-array", "build-runtime-bitstream", "build-array-bitstreams"} and args.row is None:
        parser.error("--row is required unless operation is read-array or a bitstream build")

    api = ScanDebugCellAPI(build_config(args))
    api._append_jsonl("experiment_settings.jsonl", {
        "operation": args.operation, "row": args.row, "col": args.col,
        "read_voltage_V": args.read_vcc_set, "read_wl_voltage_V": args.read_vcc_wl_set,
        "set_threshold_uA": args.set_threshold, "reset_threshold_uA": args.reset_threshold,
        "set_vcc_set_V": api.config.set_sweep.vcc_set_v,
        "reset_vcc_set_V": api.config.reset_sweep.vcc_set_v,
        "set_wl_V": api.config.set_sweep.vcc_wl_set_v,
        "reset_wl_V": api.config.reset_sweep.vcc_wl_set_v,
        "confirm_reads": args.confirm_reads, "noise_allowance_uA": api._read_noise_allowance(),
    })
    with api.hardware_queue(args.operation):
        if args.operation == "cycle":
            api._append_progress("cycle", "Starting calibrated SET/RESET cycle",
                row=args.row, col=args.col, set_threshold_uA=args.set_threshold,
                reset_threshold_uA=args.reset_threshold, confirm_reads=args.confirm_reads)
        if args.operation == "read":
            result = api.read(args.row, args.col)
        elif args.operation == "set":
            result = api.set_cell(args.row, args.col)
        elif args.operation == "reset":
            result = api.reset_cell(args.row, args.col)
        elif args.operation == "cycle":
            result = api.cycle_cell(args.row, args.col)
        elif args.operation == "read-array":
            result = api.read_array(args.row_start, args.row_end, args.col_start, args.col_end, mode=args.array_mode)
        else:
            result = api.prebuild_array_column_bitstreams(
                row_start=args.row_start,
                col_start=args.col_start,
                col_end=args.col_end,
                force=args.force_bitstreams,
            )
    print(json.dumps(result if isinstance(result, dict) else result.__dict__, default=lambda item: item.__dict__, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
