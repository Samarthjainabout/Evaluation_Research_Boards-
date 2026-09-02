#!/usr/bin/env python3
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import math
import os
import platform
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator, Iterable, Literal


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SUMMARIZER = ROOT / "api_v1/tools/summarize_capture.py"
FPGA_BITSTREAM_DIR = ROOT / "api_v1/prerequisites/fpga_zynq7020/bitstreams"
FPGA_RUNTIME_BITSTREAM = "caravel_scan_debug_runtime_dac81416_v2.bit"
FPGA_RUNTIME_PROBES = "caravel_scan_debug_runtime_dac81416_v2.ltx"

# Five-bit programming projection from set_reset_vcc_wl_projection_0_to_31.xlsx.
# Set keeps Vcc_set fixed. Reset uses an outer Vcc_set sweep; the complete
# DAC3/Vcc_wl_set projection runs inside each Vcc_set value.
SET_PROGRAM_VCC_SET_V = 2.5
SET_PROGRAM_VCC_WL_V = (
    0.44, 0.50, 0.56, 0.63, 0.69, 0.75, 0.81, 0.87,
    0.94, 1.00, 1.06, 1.12, 1.19, 1.25, 1.31, 1.37,
    1.43, 1.50, 1.56, 1.62, 1.68, 1.75, 1.81, 1.87,
    1.93, 1.99, 2.06, 2.12, 2.18, 2.24, 2.31, 2.37,
)
RESET_PROGRAM_VCC_SET_V = 3.5
RESET_PROGRAM_VCC_SET_SWEEP_V = (
    2.3, 2.7, 3.1, 3.5,
)
RESET_PROGRAM_VCC_WL_V = (
    0.94, 1.01, 1.07, 1.13, 1.19, 1.26, 1.32, 1.38,
    1.44, 1.51, 1.57, 1.63, 1.69, 1.76, 1.82, 1.88,
    1.94, 2.01, 2.07, 2.13, 2.19, 2.26, 2.32, 2.38,
    2.44, 2.50, 2.57, 2.63, 2.69, 2.75, 2.82, 2.88,
)
MANIFEST_FIELDS = [
    "index",
    "stage",
    "kind",
    "cell",
    "operation",
    "packet",
    "vcc_set_V",
    "vcc_wl_set_V",
    "bits_lsb_first",
    "bitstream",
    "ok",
    "decoded_packet",
    "la_set_window_mean_uA",
    "local_output_dir",
    "error",
]


Operation = Literal["read", "set", "reset"]


@dataclass(frozen=True)
class CellAddress:
    row: int
    col: int = 0

    def validate(self) -> None:
        if not 0 <= self.row <= 31:
            raise ValueError(f"row must be 0..31, got {self.row}")
        if not 0 <= self.col <= 31:
            raise ValueError(f"col must be 0..31, got {self.col}")

    @property
    def label(self) -> str:
        return f"{self.row}_{self.col}"


@dataclass(frozen=True)
class RailVoltages:
    """Voltages used by the DAC rail command.

    The existing Teensy command path controls the two rails relevant for scan-debug
    pulse experiments as millivolts: Vcc_set and Vcc_wl_set. Other rails are kept
    at the firmware defaults used by SCAN_CUSTOM_RAILS.
    """

    vcc_set_v: float
    vcc_wl_set_v: float

    @property
    def command(self) -> str:
        return f"SCAN_CUSTOM_RAILS {round(self.vcc_set_v * 1000):.0f} {round(self.vcc_wl_set_v * 1000):.0f}"

    @property
    def bitstream_tag(self) -> str:
        return f"vcc{round(self.vcc_set_v * 1000):04d}_wl{round(self.vcc_wl_set_v * 1000):04d}"


@dataclass(frozen=True)
class SweepConfig:
    vcc_set_v: tuple[float, ...]
    vcc_wl_set_v: tuple[float, ...]
    threshold_uA: float
    direction: Literal["above", "below"]
    confirm_reads: int = 10
    stop_on_threshold: bool = True

    @staticmethod
    def from_ranges(
        *,
        vcc_set_v: Iterable[float],
        vcc_wl_set_v: Iterable[float],
        threshold_uA: float,
        direction: Literal["above", "below"],
        confirm_reads: int = 10,
        stop_on_threshold: bool = True,
    ) -> "SweepConfig":
        return SweepConfig(
            tuple(vcc_set_v),
            tuple(vcc_wl_set_v),
            threshold_uA,
            direction,
            confirm_reads,
            stop_on_threshold,
        )


@dataclass
class ScanDebugConfig:
    run_dir: Path = ROOT / "api_v1/runs/default"
    read_rails: RailVoltages = field(default_factory=lambda: RailVoltages(0.5, 2.5))
    set_sweep: SweepConfig = field(
        default_factory=lambda: SweepConfig.from_ranges(
            vcc_set_v=(SET_PROGRAM_VCC_SET_V,),
            vcc_wl_set_v=SET_PROGRAM_VCC_WL_V,
            threshold_uA=70.0,
            direction="above",
        )
    )
    reset_sweep: SweepConfig = field(
        default_factory=lambda: SweepConfig.from_ranges(
            vcc_set_v=RESET_PROGRAM_VCC_SET_SWEEP_V,
            vcc_wl_set_v=RESET_PROGRAM_VCC_WL_V,
            threshold_uA=5.0,
            direction="below",
        )
    )
    attempts: int = 3
    shunt_ohms: float = 470.0
    dry_run: bool = False

    zynq_host: str | None = "geethika@100.116.216.70"
    zynq_password: str | None = None
    zynq_os: Literal["windows", "posix"] = "windows"
    zynq_dir: str = "C:/Users/geethika/zynq_scan_debug"
    vivado_cmd: str = "C:/Xilinx/Vivado/2019.1/bin/vivado.bat"

    saleae_host: str | None = "ubuntu-24-04@100.98.132.51"
    saleae_dir: str = "/home/ubuntu-24-04/saleae-api"
    saleae_capture_script: str = ".venv/bin/python run_fpga_scan0000_la12_15_capture.py"
    saleae_burst_capture_script: str = ".venv/bin/python run_full_array_burst_capture.py"
    saleae_restart_script: str = "./start-logic2-automation.sh"
    saleae_restart_wait_seconds: float = 10.0
    saleae_usb_recovery_enabled: bool = True
    saleae_usb_controller_pci: str = "0000:00:0c.0"
    saleae_sudo_password: str | None = os.environ.get("SCAN_DEBUG_SALEAE_SUDO_PASSWORD") or None
    adc_dac_port: str = "/dev/serial/by-id/usb-Teensyduino_USB_Serial_8829000-if00"
    fpga_dac_enabled: bool = True
    persistent_fpga_runtime: bool = True
    capture_program_pulses: bool = False
    defer_capture_copy: bool = True
    runtime_daemon_start_timeout_seconds: float = 60.0
    runtime_command_timeout_seconds: float = 20.0
    dac_teensy_reflash_enabled: bool = False
    dac_teensy_app_serial: str = "8829000"
    dac_teensy_bootloader_serial: str = "000D78D4"
    dac_teensy_loader: str = "/home/ubuntu-24-04/teensy-tools-src/teensy_loader_cli_serial/teensy_loader_cli"
    dac_teensy_mcu: str = "TEENSY41"
    dac_teensy_hex: str = "/home/ubuntu-24-04/teensy-flash/build-DAC_analog_vltgs/DAC_analog_vltgs.ino.hex"
    hardware_queue_enabled: bool = True
    hardware_queue_host: str | None = None
    hardware_queue_dir: str = "/tmp/scan_debug_hardware_queue.lock"
    hardware_queue_timeout_seconds: float = 86_400.0
    hardware_queue_poll_seconds: float = 5.0
    hardware_queue_stale_seconds: float = 43_200.0
    summarizer: Path = DEFAULT_SUMMARIZER

    digital_sample_rate: int = 50_000_000
    analog_sample_rate: int = 6_250_000
    analog_channels: str = "12,13,14,15"
    trigger_channel: int = 11
    trigger_edge: str = "falling"
    after_trigger_seconds: float = 0.000090
    trim_data_seconds: float = 0.000003
    digital_threshold_volts: float = 1.2
    enable_adc_monitor: bool = False
    burst_initial_delay_cycles: int = 1_000_000
    burst_repeat_after_done_cycles: int = 1
    burst_capture_strategy: Literal["single", "per-cell"] = "single"
    burst_post_dr_tm_hold_cycles: int = 100
    burst_fpga_reset_assert_cycles: int = 24_000
    burst_reset_release_fallback_cycles: int = 2_000
    burst_post_reset_wait_cycles: int = 128
    burst_wb_clk_period_seconds: float = 0.0000005
    burst_single_capture_margin_seconds: float = 0.25
    burst_analog_sample_rate: int = 3_125_000
    full_array_burst_digital_sample_rate: int = 6_250_000
    full_array_burst_analog_sample_rate: int = 31_250
    full_array_burst_capture_timeout_seconds: float = 900.0
    full_array_burst_packet_period_seconds: float = 0.01312428
    burst_after_trigger_seconds: float = 0.000028
    burst_trim_data_seconds: float = 0.000003
    burst_capture_timeout_seconds: float = 420.0
    saleae_arm_timeout_seconds: float = 30.0
    saleae_capture_completion_timeout_seconds: float = 30.0


@dataclass
class CellOperationResult:
    cell: CellAddress
    operation: str
    packet: str
    rails: RailVoltages
    current_uA: float | None
    decoded_packet: str = ""
    ok: bool = False
    local_output_dir: str = ""
    error: str = ""


def packet_for_cell(cell: CellAddress, op_set: int) -> int:
    """Return `{OP_SET, SL_SEL[4:0], BL_SEL[4:0], WL_SEL[4:0]}`.

    Current hardware mapping uses row for SL/WL and col for BL. Examples:
    `(1,0), read -> 0x0401`; `(10,10), read -> 0x294a`.
    """

    cell.validate()
    if op_set not in (0, 1):
        raise ValueError(f"op_set must be 0 or 1, got {op_set}")
    return (op_set << 15) | (cell.row << 10) | (cell.col << 5) | cell.row


def cell_from_packet(packet: int, *, op_set: int = 0) -> CellAddress | None:
    """Decode a scan packet back to its cell when it matches the hardware map."""

    decoded_op_set = (packet >> 15) & 0x1
    row = packet & 0x1F
    col = (packet >> 5) & 0x1F
    sl = (packet >> 10) & 0x1F
    if decoded_op_set != op_set or sl != row:
        return None
    return CellAddress(row=row, col=col)


def bits_lsb(packet: int) -> str:
    return f"{packet:016b}"[::-1]


class CommandRunner:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run

    def run(self, cmd: list[str], *, log: Path | None = None, timeout_s: int | None = None) -> subprocess.CompletedProcess[str]:
        text = " ".join(cmd)
        if self.dry_run:
            if log:
                log.write_text(f"DRY_RUN {text}\n")
            return subprocess.CompletedProcess(cmd, 0, f"DRY_RUN {text}\n", "")
        proc = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout_s,
        )
        if log:
            log.write_text(proc.stdout)
        return proc

    def ssh(self, host: str, command: str, *, timeout_s: int | None = None, log: Path | None = None) -> subprocess.CompletedProcess[str]:
        return self.run(["ssh", "-o", "ConnectTimeout=15", host, command], timeout_s=timeout_s, log=log)

    def ssh_with_expect_password(
        self,
        host: str,
        password: str,
        command: str,
        *,
        timeout_s: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        if self.dry_run:
            text = f"DRY_RUN ssh {host} {command}\n"
            return subprocess.CompletedProcess(["ssh", host, command], 0, text, "")
        if platform.system().lower().startswith("win"):
            return self._ssh_with_paramiko_password(host, password, command, timeout_s=timeout_s)
        if shutil.which("expect") is None:
            raise RuntimeError("expect is required for password SSH automation on this platform; use SSH keys or install expect")
        script = f"""
set timeout {timeout_s or 600}
spawn ssh -o ConnectTimeout=15 {host} {{{command}}}
expect {{
  -re "password:" {{ send "{password}\\r"; exp_continue }}
  eof
}}
catch wait result
exit [lindex $result 3]
"""
        proc = subprocess.run(["expect", "-c", script], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return subprocess.CompletedProcess(["ssh", host, command], proc.returncode, proc.stdout, "")

    @staticmethod
    def _ssh_with_paramiko_password(
        host: str,
        password: str,
        command: str,
        *,
        timeout_s: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            import paramiko
        except ImportError as exc:
            raise RuntimeError(
                "password SSH automation on Windows requires Paramiko; "
                "install it with: python -m pip install -r api_v1/requirements.txt"
            ) from exc

        username, separator, hostname = host.rpartition("@")
        if not separator or not username or not hostname:
            raise RuntimeError("password SSH host must use the user@hostname form")

        client = paramiko.SSHClient()
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=hostname,
                username=username,
                password=password,
                timeout=timeout_s,
                banner_timeout=timeout_s,
                auth_timeout=timeout_s,
                allow_agent=False,
                look_for_keys=False,
            )
            transport = client.get_transport()
            if transport is None or not transport.is_active():
                raise RuntimeError(f"SSH connection to {host} did not become active")
            channel = transport.open_session(timeout=timeout_s)
            try:
                channel.set_combine_stderr(True)
                if timeout_s is not None:
                    channel.settimeout(timeout_s)
                channel.exec_command(command)
                raw_output = channel.makefile("rb", -1).read()
                output = raw_output.decode("utf-8", errors="replace")
                returncode = channel.recv_exit_status()
            finally:
                channel.close()
        finally:
            client.close()
        return subprocess.CompletedProcess(["ssh", host, command], returncode, output, "")


class ScanDebugCellAPI:
    def __init__(self, config: ScanDebugConfig | None = None):
        self.config = config or ScanDebugConfig()
        self.config.run_dir.mkdir(parents=True, exist_ok=True)
        (self.config.run_dir / "raw").mkdir(exist_ok=True)
        self.runner = CommandRunner(self.config.dry_run)
        self.manifest = self.config.run_dir / "manifest.csv"
        self._runtime_bitstream_ready = False
        self._runtime_daemon_ready = False
        self._runtime_daemon_process: subprocess.Popen[str] | None = None
        self._runtime_daemon_log_handle = None
        self._pending_capture_copies: list[tuple[threading.Thread, list[BaseException]]] = []
        self._saleae_capture_script_ready = False
        self._ensure_manifest()

    @contextmanager
    def hardware_queue(self, operation: str) -> Iterator[None]:
        if (
            self.config.dry_run
            or not self.config.hardware_queue_enabled
            or operation in {"build-runtime-bitstream", "build-array-bitstreams"}
        ):
            yield
            return
        host = self.config.hardware_queue_host or self.config.saleae_host
        if not host:
            yield
            return
        token = uuid.uuid4().hex
        owner = (
            f"token={token} host={platform.node() or 'unknown'} pid={os.getpid()} "
            f"operation={operation} run_dir={self.config.run_dir} started={time.strftime('%Y-%m-%d %H:%M:%S %Z')}"
        )
        self._acquire_hardware_queue(host, token, owner, operation)
        try:
            yield
        finally:
            try:
                self._wait_for_pending_capture_copies()
            finally:
                try:
                    self._stop_runtime_vio_daemon()
                finally:
                    self._release_hardware_queue(host, token, operation)

    def _acquire_hardware_queue(self, host: str, token: str, owner: str, operation: str) -> None:
        deadline = time.time() + max(1.0, self.config.hardware_queue_timeout_seconds)
        poll_seconds = max(1.0, self.config.hardware_queue_poll_seconds)
        next_progress = 0.0
        while True:
            command = self._hardware_queue_acquire_command(token, owner)
            try:
                proc = self.runner.ssh(host, command, timeout_s=20)
            except subprocess.TimeoutExpired:
                # The remote mkdir/write can complete even when the SSH session
                # is slow to close (notably just after the Saleae VM reboots).
                # Confirm this worker owns the lock before treating the return
                # timeout as an acquisition failure.
                token_path = self._sh_quote(self.config.hardware_queue_dir + "/token")
                verify = self.runner.ssh(host, f"cat {token_path} 2>/dev/null || true", timeout_s=10)
                if verify.returncode == 0 and verify.stdout.strip() == token:
                    self._append_progress(operation, "Hardware queue lock acquired after SSH return timeout", queue="acquired")
                    return
                raise
            if proc.returncode == 0:
                self._append_progress(operation, "Hardware queue lock acquired", queue="acquired")
                return
            now = time.time()
            if now >= deadline:
                owner_text = self._hardware_queue_owner(host)
                raise RuntimeError(f"Timed out waiting for hardware queue. Current owner: {owner_text or 'unknown'}")
            if now >= next_progress:
                owner_text = self._hardware_queue_owner(host)
                self._append_progress(
                    operation,
                    f"Queued: waiting for hardware bench{f' ({owner_text})' if owner_text else ''}",
                    queue="waiting",
                )
                next_progress = now + 30.0
            time.sleep(poll_seconds)

    def _release_hardware_queue(self, host: str, token: str, operation: str) -> None:
        proc = self.runner.ssh(host, self._hardware_queue_release_command(token), timeout_s=20)
        if proc.returncode == 0:
            self._append_progress(operation, "Hardware queue lock released", queue="released")
        else:
            self._append_progress(operation, "Hardware queue release failed", queue="release_failed")

    def _hardware_queue_owner(self, host: str) -> str:
        try:
            proc = self.runner.ssh(
                host,
                f"cat {self._sh_quote(self.config.hardware_queue_dir + '/owner')} 2>/dev/null || true",
                timeout_s=10,
            )
        except subprocess.TimeoutExpired:
            # Owner text is diagnostic only. A slow SSH close must not abort
            # the operation while it is legitimately waiting for the bench.
            return ""
        return " ".join(proc.stdout.strip().split())[:180] if proc.returncode == 0 else ""

    def _hardware_queue_acquire_command(self, token: str, owner: str) -> str:
        lock_dir = self._sh_quote(self.config.hardware_queue_dir)
        token_q = self._sh_quote(token)
        owner_q = self._sh_quote(owner)
        stale_seconds = int(max(60.0, self.config.hardware_queue_stale_seconds))
        return (
            f"lock_dir={lock_dir}; token={token_q}; owner={owner_q}; stale_seconds={stale_seconds}; "
            "now=$(date +%s); "
            'if mkdir "$lock_dir" 2>/dev/null; then '
            'printf "%s\\n" "$token" > "$lock_dir/token"; '
            'printf "%s\\n" "$owner" > "$lock_dir/owner"; '
            'printf "%s\\n" "$now" > "$lock_dir/started"; '
            "exit 0; "
            "fi; "
            'started=$(cat "$lock_dir/started" 2>/dev/null || echo 0); '
            'case "$started" in (*[!0-9]*|"") started=0;; esac; '
            'if [ "$started" -gt 0 ] && [ $((now - started)) -gt "$stale_seconds" ]; then '
            'stale_dir="${lock_dir}.stale.$$"; '
            'mv "$lock_dir" "$stale_dir" 2>/dev/null && rm -rf "$stale_dir"; '
            "fi; "
            "exit 1"
        )

    def _hardware_queue_release_command(self, token: str) -> str:
        lock_dir = self._sh_quote(self.config.hardware_queue_dir)
        token_q = self._sh_quote(token)
        return (
            f"lock_dir={lock_dir}; token={token_q}; "
            'if [ "$(cat "$lock_dir/token" 2>/dev/null)" = "$token" ]; then '
            'rm -rf "$lock_dir"; '
            "fi"
        )

    def read(self, row: int, col: int = 0) -> CellOperationResult:
        cell = CellAddress(row, col)
        return self._pulse_and_capture(cell, "read", self.config.read_rails, "read")

    def read_array(
        self,
        row_start: int = 0,
        row_end: int = 31,
        col_start: int = 0,
        col_end: int = 31,
        *,
        mode: Literal["burst", "burst-columns", "serial"] = "burst-columns",
    ) -> dict[str, object]:
        if not 0 <= row_start <= row_end <= 31:
            raise ValueError(f"row range must be 0..31, got {row_start}..{row_end}")
        if not 0 <= col_start <= col_end <= 31:
            raise ValueError(f"col range must be 0..31, got {col_start}..{col_end}")
        if mode == "burst":
            return self.read_array_burst(row_start, row_end, col_start, col_end)
        if mode == "burst-columns":
            return self.read_array_burst_columns(row_start, row_end, col_start, col_end)
        if mode != "serial":
            raise ValueError(f"array mode must be burst, burst-columns, or serial, got {mode!r}")
        reads: list[dict[str, object]] = []
        for row in range(row_start, row_end + 1):
            for col in range(col_start, col_end + 1):
                reads.append(asdict(self.read(row, col)))
        summary = {
            "operation": "read-array",
            "row_start": row_start,
            "row_end": row_end,
            "col_start": col_start,
            "col_end": col_end,
            "count": len(reads),
            "reads": reads,
        }
        self._append_jsonl("array_reads.jsonl", summary)
        return summary

    def read_array_burst_columns(
        self,
        row_start: int = 0,
        row_end: int = 31,
        col_start: int = 0,
        col_end: int = 31,
    ) -> dict[str, object]:
        total = (row_end - row_start + 1) * (col_end - col_start + 1)
        all_reads: list[dict[str, object]] = []
        self._ensure_saleae_burst_script()
        for col in range(col_start, col_end + 1):
            column_total = row_end - row_start + 1
            packet = packet_for_cell(CellAddress(row_start, col), 0)
            self._append_progress("read-array", f"Column {col}: preparing burst", cells=len(all_reads), total=total)
            bitstream = self._ensure_array_bitstream(row_start, col)
            if self.config.dry_run:
                for row in range(row_start, row_end + 1):
                    all_reads.append(
                        {
                            "cell": asdict(CellAddress(row, col)),
                            "operation": "read",
                            "packet": f"0x{packet_for_cell(CellAddress(row, col), 0):04x}",
                            "rails": asdict(self.config.read_rails),
                            "current_uA": None,
                            "decoded_packet": "",
                            "ok": True,
                            "dry_run": True,
                        }
                    )
                self._append_progress("read-array", f"Column {col}: dry-run complete", cells=len(all_reads), total=total)
                continue
            index = self._next_index()
            attempts = max(1, self.config.attempts)
            local_output_dir: Path | None = None
            remote_output_dir = ""
            best_local_output_dir: Path | None = None
            best_remote_output_dir = ""
            best_valid_count = -1
            best_validation_error = ""
            failures: list[str] = []
            for attempt in range(1, attempts + 1):
                attempt_note = f" attempt {attempt}" if attempts > 1 else ""
                strategy = "single-capture" if self.config.burst_capture_strategy == "single" else "per-cell"
                self._append_progress(
                    "read-array",
                    f"Column {col}: starting Saleae {strategy} burst{attempt_note}",
                    cells=len(all_reads),
                    total=total,
                )
                remote_output_dir = self._capture_array_burst(
                    packet,
                    self.config.read_rails,
                    bitstream,
                    index,
                    column_total,
                    row_start,
                    col,
                    f"column {col}",
                    cells_done=len(all_reads),
                    total_cells=total,
                )
                self._append_progress("read-array", f"Column {col}: copying capture", cells=len(all_reads), total=total)
                local_output_dir = self._copy_capture(remote_output_dir, index, f"read_array_col{col:02d}_burst", self.config.read_rails)
                self._append_progress("read-array", f"Column {col}: checking capture", cells=len(all_reads), total=total)
                validation_error = self._validate_burst_manifest(local_output_dir, column_total)
                valid_count = self._count_valid_burst_packets(local_output_dir)
                if valid_count > best_valid_count:
                    best_valid_count = valid_count
                    best_local_output_dir = local_output_dir
                    best_remote_output_dir = remote_output_dir
                    best_validation_error = validation_error
                if not validation_error:
                    break
                failures.append(f"attempt={attempt} {validation_error}")
                if attempt >= attempts:
                    if best_local_output_dir is not None and best_valid_count > 0:
                        local_output_dir = best_local_output_dir
                        remote_output_dir = best_remote_output_dir
                        self._append_progress(
                            "read-array",
                            f"Column {col}: keeping {best_valid_count}/{column_total} decoded cells; {best_validation_error}",
                            cells=len(all_reads) + best_valid_count,
                            total=total,
                        )
                        break
                    raise RuntimeError(
                        f"Column {col} capture failed validation after {attempts} attempts: {validation_error}; "
                        f"see {local_output_dir / 'manifest.csv'}"
                    )
                if self._saleae_needs_restart(validation_error):
                    self._append_progress(
                        "read-array",
                        f"Column {col}: restarting capture service after validation error",
                        cells=len(all_reads),
                        total=total,
                    )
                    restart_log = self._restart_saleae_automation(index, f"read_array_col{col:02d}", attempt)
                    failures.append(f"saleae_restart_after_attempt={attempt} log={restart_log}")
                self._append_progress("read-array", f"Column {col}: retrying capture", cells=len(all_reads), total=total)
                time.sleep(2.0)
            if local_output_dir is None:
                raise RuntimeError(f"Column {col} capture did not produce a local output directory")
            self._append_progress("read-array", f"Column {col}: decoding reads", cells=len(all_reads), total=total)
            reads = self._append_burst_manifest(local_output_dir, remote_output_dir, bitstream)
            all_reads.extend(reads)
            self._append_progress("read-array", f"Column {col}: decoded", cells=len(all_reads), total=total)
        summary = {
            "operation": "read-array",
            "mode": "burst-columns",
            "row_start": row_start,
            "row_end": row_end,
            "col_start": col_start,
            "col_end": col_end,
            "count": len(all_reads),
            "rails": asdict(self.config.read_rails),
            "reads": all_reads,
        }
        self._append_jsonl("array_reads.jsonl", summary)
        return summary

    def read_array_burst(
        self,
        row_start: int = 0,
        row_end: int = 31,
        col_start: int = 0,
        col_end: int = 31,
    ) -> dict[str, object]:
        if (row_start, row_end, col_start, col_end) != (0, 31, 0, 31):
            raise ValueError("burst array read currently supports the full 32x32 array only; use mode='serial' for sub-ranges")
        cells = self._array_sweep_cells(row_start, col_start)
        packet = packet_for_cell(CellAddress(row_start, col_start), 0)
        self._append_progress("read-array", "Preparing burst bitstream", cells=0, total=len(cells))
        bitstream = self._ensure_array_bitstream(row_start, col_start)
        if self.config.dry_run:
            summary = {
                "operation": "read-array",
                "mode": "burst",
                "row_start": row_start,
                "row_end": row_end,
                "col_start": col_start,
                "col_end": col_end,
                "count": len(cells),
                "start_packet": f"0x{packet:04x}",
                "bitstream": bitstream,
                "rails": asdict(self.config.read_rails),
                "dry_run": True,
            }
            self._append_jsonl("array_reads.jsonl", summary)
            return summary

        self._ensure_saleae_burst_script()
        index = self._next_index()
        self._append_progress("read-array", "Starting Saleae burst capture", mode="burst")
        remote_output_dir = self._capture_array_burst(packet, self.config.read_rails, bitstream, index, len(cells), row_start, col_start, "full-array burst")
        self._append_progress("read-array", "Copying burst capture", mode="burst")
        local_output_dir = self._copy_capture(remote_output_dir, index, "read_array_burst", self.config.read_rails)
        self._append_progress("read-array", "Decoding burst reads", mode="burst")
        reads = self._append_burst_manifest(local_output_dir, remote_output_dir, bitstream)
        self._append_progress("read-array", "Burst read decoded", cells=len(reads), total=len(cells))
        summary = {
            "operation": "read-array",
            "mode": "burst",
            "row_start": row_start,
            "row_end": row_end,
            "col_start": col_start,
            "col_end": col_end,
            "count": len(reads),
            "start_packet": f"0x{packet:04x}",
            "bitstream": bitstream,
            "remote_output_dir": remote_output_dir,
            "local_output_dir": str(local_output_dir),
            "reads": reads,
        }
        self._append_jsonl("array_reads.jsonl", summary)
        return summary

    def set_cell(self, row: int, col: int = 0) -> dict[str, object]:
        return self._ramp_until(CellAddress(row, col), "set", self.config.set_sweep)

    def reset_cell(self, row: int, col: int = 0) -> dict[str, object]:
        return self._ramp_until(CellAddress(row, col), "reset", self.config.reset_sweep)

    def cycle_cell(self, row: int, col: int = 0) -> dict[str, object]:
        cell = CellAddress(row, col)
        initial = self.read(row, col)
        set_result = self.set_cell(row, col)
        reset_result = self.reset_cell(row, col)
        result = {
            "cell": asdict(cell),
            "initial_read_uA": initial.current_uA,
            "set": set_result,
            "reset": reset_result,
        }
        self._append_jsonl("cell_cycles.jsonl", result)
        return result

    def _ramp_until(self, cell: CellAddress, operation: Operation, sweep: SweepConfig) -> dict[str, object]:
        if operation not in ("set", "reset"):
            raise ValueError("ramp operation must be set or reset")
        results: list[dict[str, object]] = []
        best: CellOperationResult | None = None
        target_hit = False
        completed_rails = self._completed_sweep_rails(cell, operation)
        for vcc_set_v in sweep.vcc_set_v:
            for vcc_wl_set_v in sweep.vcc_wl_set_v:
                if self._rail_key(vcc_set_v, vcc_wl_set_v) in completed_rails:
                    self._append_progress(
                        operation,
                        f"Skipping completed {operation} pulse",
                        vcc_set_V=vcc_set_v,
                        vcc_wl_set_V=vcc_wl_set_v,
                    )
                    continue
                rails = RailVoltages(vcc_set_v, vcc_wl_set_v)
                pre_read: CellOperationResult | None = None
                if operation in ("set", "reset"):
                    pre_read = self._pulse_and_capture(cell, "read", self.config.read_rails, f"read_before_{operation}")
                    if pre_read.current_uA is not None and self._passes(pre_read.current_uA, sweep.threshold_uA, sweep.direction):
                        confirms = self.confirm_reads(cell, sweep.confirm_reads, sweep.threshold_uA, sweep.direction)
                        target_hit = all(
                            item.current_uA is not None and self._passes(item.current_uA, sweep.threshold_uA, sweep.direction)
                            for item in confirms
                        )
                        entry = {
                            "pre_read": asdict(pre_read),
                            "pulse": None,
                            "verify": asdict(pre_read),
                            "confirm_reads": [asdict(item) for item in confirms],
                            "threshold_uA": sweep.threshold_uA,
                            "direction": sweep.direction,
                            "skipped_pulse": target_hit,
                        }
                        results.append(entry)
                        best = pre_read
                        if target_hit and sweep.stop_on_threshold:
                            break
                pulse = self._program_pulse(cell, operation, rails, f"{operation}_pulse")
                verify = self._pulse_and_capture(cell, "read", self.config.read_rails, f"read_after_{operation}")
                entry = {
                    "pre_read": asdict(pre_read) if pre_read else None,
                    "pulse": asdict(pulse),
                    "verify": asdict(verify),
                    "threshold_uA": sweep.threshold_uA,
                    "direction": sweep.direction,
                }
                results.append(entry)
                if verify.current_uA is not None and self._passes(verify.current_uA, sweep.threshold_uA, sweep.direction):
                    confirms = self.confirm_reads(cell, sweep.confirm_reads, sweep.threshold_uA, sweep.direction)
                    entry["confirm_reads"] = [asdict(item) for item in confirms]
                    target_hit = all(
                        item.current_uA is not None and self._passes(item.current_uA, sweep.threshold_uA, sweep.direction)
                        for item in confirms
                    )
                    best = verify
                    if target_hit and sweep.stop_on_threshold:
                        break
                if best is None or self._is_better(verify, best, sweep.direction):
                    best = verify
            if target_hit and sweep.stop_on_threshold:
                break
        summary = {
            "cell": asdict(cell),
            "operation": operation,
            "target_hit": target_hit,
            "best_read_uA": best.current_uA if best else None,
            "best_packet": best.packet if best else "",
            "steps": results,
        }
        self._append_jsonl("cell_operations.jsonl", summary)
        return summary

    def _completed_sweep_rails(self, cell: CellAddress, operation: Operation) -> set[tuple[float, float]]:
        if operation not in ("set", "reset") or not self.manifest.exists():
            return set()
        completed: set[tuple[float, float]] = set()
        with self.manifest.open(newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("operation") != operation or row.get("cell") != cell.label:
                    continue
                if str(row.get("ok", "")).lower() != "true":
                    continue
                vcc_set_v = self._float_or_none(row.get("vcc_set_V"))
                vcc_wl_set_v = self._float_or_none(row.get("vcc_wl_set_V"))
                if vcc_set_v is None or vcc_wl_set_v is None:
                    continue
                completed.add(self._rail_key(vcc_set_v, vcc_wl_set_v))
        return completed

    def _rail_key(self, vcc_set_v: float, vcc_wl_set_v: float) -> tuple[float, float]:
        return (round(vcc_set_v, 6), round(vcc_wl_set_v, 6))

    def confirm_reads(
        self,
        cell: CellAddress,
        count: int,
        threshold_uA: float | None = None,
        direction: Literal["above", "below"] = "above",
    ) -> list[CellOperationResult]:
        out: list[CellOperationResult] = []
        for _ in range(count):
            result = self._pulse_and_capture(cell, "read", self.config.read_rails, "confirm_read")
            out.append(result)
            if threshold_uA is not None and (
                result.current_uA is None or not self._passes(result.current_uA, threshold_uA, direction)
            ):
                break
        return out

    def _pulse_and_capture(
        self,
        cell: CellAddress,
        operation: Operation,
        rails: RailVoltages,
        stage: str,
    ) -> CellOperationResult:
        cell.validate()
        op_set = 1 if operation == "set" else 0
        packet = packet_for_cell(cell, op_set)
        self._ensure_saleae_capture_script()
        bitstream = self._ensure_bitstream(cell, op_set, rails)
        if self.config.fpga_dac_enabled and self.config.persistent_fpga_runtime and not self.config.dry_run:
            self._ensure_runtime_vio_daemon(bitstream)
        index = self._next_index()
        kind = f"r{cell.row:02d}c{cell.col:02d}_{stage}_vcc{rails.vcc_set_v:.3f}_wl{rails.vcc_wl_set_v:.3f}".replace(".", "p")

        if self.config.dry_run:
            result = CellOperationResult(
                cell=cell,
                operation=operation,
                packet=f"0x{packet:04x}",
                rails=rails,
                current_uA=None,
                decoded_packet=f"0x{packet:04x}",
                ok=True,
                local_output_dir="DRY_RUN",
            )
            self._append_manifest(index, stage, kind, result, bitstream, bits_lsb(packet))
            return result

        summary_errors: list[str] = []
        for attempt in range(1, max(1, self.config.attempts) + 1):
            remote_output_dir = self._capture_remote(packet, rails, bitstream, index, kind)
            try:
                if self.config.defer_capture_copy and self.config.saleae_host:
                    local_output_dir = self._capture_local_path(remote_output_dir, index, kind, rails)
                    summary = self._summarize_remote_capture(
                        index, stage, kind, packet, rails, remote_output_dir, local_output_dir
                    )
                    self._schedule_capture_copy(remote_output_dir, index, kind, rails)
                else:
                    local_output_dir = self._copy_capture(remote_output_dir, index, kind, rails)
                    summary = self._summarize_capture(
                        index, stage, kind, packet, rails, remote_output_dir, local_output_dir
                    )
                break
            except RuntimeError as exc:
                summary_errors.append(f"attempt={attempt}: {exc}")
                # Preserve the proven synchronous path as the recovery route.
                try:
                    local_output_dir = self._copy_capture(remote_output_dir, index, kind, rails)
                    summary = self._summarize_capture(
                        index, stage, kind, packet, rails, remote_output_dir, local_output_dir
                    )
                    break
                except RuntimeError as fallback_exc:
                    summary_errors.append(f"attempt={attempt} synchronous fallback: {fallback_exc}")
                if attempt >= max(1, self.config.attempts):
                    raise RuntimeError(
                        f"capture summary failed index={index} kind={kind} after {attempt} attempts:\n"
                        + "\n".join(summary_errors)
                    ) from exc
                time.sleep(2.0)
        result = CellOperationResult(
            cell=cell,
            operation=operation,
            packet=f"0x{packet:04x}",
            rails=rails,
            current_uA=self._float_or_none(summary.get("la_set_window_mean_uA")),
            decoded_packet=str(summary.get("decoded_packet", "")),
            ok=str(summary.get("ok")) == "True",
            local_output_dir=str(local_output_dir),
            error=str(summary.get("error", "")),
        )
        self._append_manifest(index, stage, kind, result, bitstream, bits_lsb(packet))
        if not result.ok:
            raise RuntimeError(f"capture decoded incorrectly: expected 0x{packet:04x}, got {result.decoded_packet}: {result.error}")
        return result

    def _program_pulse(
        self,
        cell: CellAddress,
        operation: Operation,
        rails: RailVoltages,
        stage: str,
    ) -> CellOperationResult:
        """Issue a set/reset pulse, capturing it only when explicitly requested.

        The adaptive algorithms make their decision from the following read,
        not from the programming-pulse waveform.  With the runtime FPGA DAC,
        waiting for the VIO status acknowledgement is enough to prove that the
        pulse completed before its captured verification read starts.
        """

        if operation not in ("set", "reset"):
            return self._pulse_and_capture(cell, operation, rails, stage)
        if self.config.capture_program_pulses or not self.config.fpga_dac_enabled:
            return self._pulse_and_capture(cell, operation, rails, stage)

        cell.validate()
        packet = packet_for_cell(cell, 1 if operation == "set" else 0)
        bitstream = self._ensure_bitstream(cell, 1 if operation == "set" else 0, rails)
        if self.config.persistent_fpga_runtime and not self.config.dry_run:
            self._ensure_runtime_vio_daemon(bitstream)
        index = self._next_index()
        kind = f"r{cell.row:02d}c{cell.col:02d}_{stage}_vcc{rails.vcc_set_v:.3f}_wl{rails.vcc_wl_set_v:.3f}".replace(".", "p")
        if not self.config.dry_run:
            rc = self._program_fpga(bitstream, packet=packet, rails=rails, packet_count=1)
            if rc != 0:
                raise RuntimeError(f"FPGA runtime pulse failed for {kind} with exit code {rc}")
        result = CellOperationResult(
            cell=cell,
            operation=operation,
            packet=f"0x{packet:04x}",
            rails=rails,
            current_uA=None,
            decoded_packet=f"0x{packet:04x}",
            ok=True,
            local_output_dir="FPGA_RUNTIME_ACK_NO_CAPTURE",
        )
        self._append_manifest(index, stage, kind, result, bitstream, bits_lsb(packet))
        return result

    def _ensure_bitstream(self, cell: CellAddress, op_set: int, rails: RailVoltages) -> str:
        if self.config.fpga_dac_enabled:
            return self._ensure_runtime_bitstream()

        packet = packet_for_cell(cell, op_set)
        mode = "set" if op_set else "read"
        dac_tag = f"_dac81416_{rails.bitstream_tag}" if self.config.fpga_dac_enabled else ""
        bit_name = f"caravel_scan_debug_fpga_{mode}{packet:04x}{dac_tag}_fpga_reset_delay_repeat.bit"
        if self.config.dry_run:
            return bit_name

        if self._remote_file_exists(bit_name):
            return bit_name

        self._ensure_remote_fpga_sources()
        tcl_name = f"build_scan_debug_{mode}{packet:04x}{dac_tag}_fpga_reset_delay_repeat.tcl"
        tcl = self._build_tcl(cell, op_set, rails, bit_name)
        self._write_remote_text(tcl_name, tcl)
        proc = self._run_zynq(f"{self.config.vivado_cmd} -mode batch -source {tcl_name}", timeout_s=900)
        if proc.returncode != 0:
            raise RuntimeError(proc.stdout)
        return bit_name

    def _build_tcl(self, cell: CellAddress, op_set: int, rails: RailVoltages, bit_name: str) -> str:
        packet = packet_for_cell(cell, op_set)
        mode = "set" if op_set else "read"
        return f"""set script_dir [file dirname [file normalize [info script]]]
set part_name "xc7z020clg400-2"
set project_name "vivado_project_{mode}{packet:04x}_fpga_reset_delay_repeat"
set project_dir [file join $script_dir $project_name]
set bit_name "{bit_name}"
set xdc_file [file join $script_dir "caravel_scan_debug_fpga.xdc"]

if {{[file exists $project_dir]}} {{
    file delete -force $project_dir
}}

create_project $project_name $project_dir -part $part_name -force
add_files [file join $script_dir "caravel_scan_debug_fpga.v"]
add_files [file join $script_dir "dac81416_spi.v"]
set_property top caravel_scan_debug_fpga [current_fileset]
add_files -fileset constrs_1 $xdc_file

synth_design -top caravel_scan_debug_fpga -part $part_name -generic [list \\
    OP_SET={op_set} \\
    WL_SEL={cell.row} \\
    BL_SEL={cell.col} \\
    SL_SEL={cell.row} \\
    SEQUENCE_MODE=1 \\
    INITIAL_SEQUENCE_DELAY_CYCLES=10000000 \\
    SEQ_START_ROW={cell.row} \\
    SEQ_START_COL={cell.col} \\
    MANUAL_RESET_MODE=0 \\
    FPGA_RESET_ASSERT_CYCLES=240000 \\
    POST_RESET_WAIT_CYCLES=1000000 \\
    POST_DR_TM_HOLD_CYCLES=100 \\
    REPEAT_AFTER_DONE_CYCLES=0 \\
    DAC_VCC_SET_MV={round(rails.vcc_set_v * 1000)} \\
    DAC_VCC_WL_SET_MV={round(rails.vcc_wl_set_v * 1000)} \\
]
opt_design
place_design
route_design
write_bitstream -force [file join $script_dir $bit_name]
puts "BUILT $bit_name fpga-reset delayed packet=0x{packet:04x}"
exit
"""

    def _ensure_array_bitstream(self, row_start: int, col_start: int) -> str:
        if self.config.fpga_dac_enabled:
            return self._ensure_runtime_bitstream()

        dac_tag = f"_dac81416_{self.config.read_rails.bitstream_tag}" if self.config.fpga_dac_enabled else ""
        bit_name = (
            f"caravel_scan_debug_fpga_array_read_r{row_start:02d}c{col_start:02d}"
            f"_init{self.config.burst_initial_delay_cycles}"
            f"_tm{self.config.burst_post_dr_tm_hold_cycles}"
            f"_rst{self.config.burst_fpga_reset_assert_cycles}"
            f"_gap{self.config.burst_repeat_after_done_cycles}{dac_tag}_burst.bit"
        )
        if self.config.dry_run:
            return bit_name
        if self._remote_file_exists(bit_name):
            return bit_name
        cached = self._cached_array_bitstream(row_start, col_start)
        if cached.exists():
            self._write_remote_binary(bit_name, cached.read_bytes())
            return bit_name
        self._ensure_remote_fpga_sources()
        tcl_name = f"build_scan_debug_array_read_r{row_start:02d}c{col_start:02d}_burst.tcl"
        tcl = self._build_array_tcl(row_start, col_start, bit_name)
        self._write_remote_text(tcl_name, tcl)
        proc = self._run_zynq(f"{self.config.vivado_cmd} -mode batch -source {tcl_name}", timeout_s=900)
        if proc.returncode != 0:
            raise RuntimeError(proc.stdout)
        return bit_name

    def prebuild_array_column_bitstreams(
        self,
        row_start: int = 0,
        col_start: int = 0,
        col_end: int = 31,
        *,
        force: bool = False,
    ) -> dict[str, object]:
        if self.config.fpga_dac_enabled:
            bitstream = self._ensure_runtime_bitstream(force=force)
            return {
                "operation": "build-runtime-bitstream",
                "row_start": row_start,
                "col_start": col_start,
                "col_end": col_end,
                "built": [bitstream],
                "cached": [],
                "bitstream_dir": str(FPGA_BITSTREAM_DIR.relative_to(ROOT)),
            }

        if not 0 <= row_start <= 31:
            raise ValueError(f"row_start must be 0..31, got {row_start}")
        if not 0 <= col_start <= col_end <= 31:
            raise ValueError(f"col range must be 0..31, got {col_start}..{col_end}")
        FPGA_BITSTREAM_DIR.mkdir(parents=True, exist_ok=True)
        built: list[str] = []
        cached: list[str] = []
        for col in range(col_start, col_end + 1):
            local_path = self._cached_array_bitstream(row_start, col)
            if local_path.exists() and not force:
                cached.append(local_path.name)
                continue
            dac_tag = f"_dac81416_{self.config.read_rails.bitstream_tag}" if self.config.fpga_dac_enabled else ""
            bit_name = (
                f"caravel_scan_debug_fpga_array_read_r{row_start:02d}c{col:02d}"
                f"_init{self.config.burst_initial_delay_cycles}"
                f"_tm{self.config.burst_post_dr_tm_hold_cycles}"
                f"_rst{self.config.burst_fpga_reset_assert_cycles}"
                f"_gap{self.config.burst_repeat_after_done_cycles}{dac_tag}_burst.bit"
            )
            if self.config.dry_run:
                built.append(bit_name)
                continue
            if force and self._remote_file_exists(bit_name):
                self._remove_remote_file(bit_name)
            if force and local_path.exists():
                local_path.unlink()
            self._ensure_array_bitstream(row_start, col)
            self._copy_remote_binary_to_local(bit_name, local_path)
            built.append(local_path.name)
        return {
            "operation": "build-array-bitstreams",
            "row_start": row_start,
            "col_start": col_start,
            "col_end": col_end,
            "built": built,
            "cached": cached,
            "bitstream_dir": str(FPGA_BITSTREAM_DIR.relative_to(ROOT)),
        }

    def _cached_array_bitstream(self, row_start: int, col_start: int) -> Path:
        if self.config.fpga_dac_enabled:
            return FPGA_BITSTREAM_DIR / FPGA_RUNTIME_BITSTREAM

        dac_tag = f"_dac81416_{self.config.read_rails.bitstream_tag}" if self.config.fpga_dac_enabled else ""
        return FPGA_BITSTREAM_DIR / (
            f"caravel_scan_debug_fpga_array_read_r{row_start:02d}c{col_start:02d}"
            f"_init{self.config.burst_initial_delay_cycles}"
            f"_tm{self.config.burst_post_dr_tm_hold_cycles}"
            f"_rst{self.config.burst_fpga_reset_assert_cycles}"
            f"_gap{self.config.burst_repeat_after_done_cycles}{dac_tag}_burst.bit"
        )

    @staticmethod
    def _array_sweep_cells(row_start: int = 0, col_start: int = 0) -> list[CellAddress]:
        cells = [CellAddress(row, 0) for row in range(32)]
        for col in range(1, 32):
            cells.extend(CellAddress(row, col) for row in range(32))
        start = CellAddress(row_start, col_start)
        try:
            start_index = cells.index(start)
        except ValueError as exc:
            raise ValueError(f"start cell ({row_start},{col_start}) is not in the array sweep order") from exc
        return cells[start_index:]

    def _build_array_tcl(self, row_start: int, col_start: int, bit_name: str) -> str:
        return f"""set script_dir [file dirname [file normalize [info script]]]
set part_name "xc7z020clg400-2"
set project_name "vivado_project_array_read_r{row_start:02d}c{col_start:02d}_burst"
set project_dir [file join $script_dir $project_name]
set bit_name "{bit_name}"
set xdc_file [file join $script_dir "caravel_scan_debug_fpga.xdc"]

if {{[file exists $project_dir]}} {{
    file delete -force $project_dir
}}

create_project $project_name $project_dir -part $part_name -force
add_files [file join $script_dir "caravel_scan_debug_fpga.v"]
add_files [file join $script_dir "dac81416_spi.v"]
set_property top caravel_scan_debug_fpga [current_fileset]
add_files -fileset constrs_1 $xdc_file

synth_design -top caravel_scan_debug_fpga -part $part_name -generic [list \\
    OP_SET=0 \\
    SEQUENCE_MODE=1 \\
    INITIAL_SEQUENCE_DELAY_CYCLES={self.config.burst_initial_delay_cycles} \\
    RESET_RELEASE_FALLBACK_CYCLES={self.config.burst_reset_release_fallback_cycles} \\
    FPGA_RESET_ASSERT_CYCLES={self.config.burst_fpga_reset_assert_cycles} \\
    POST_RESET_WAIT_CYCLES={self.config.burst_post_reset_wait_cycles} \\
    POST_DR_TM_HOLD_CYCLES={self.config.burst_post_dr_tm_hold_cycles} \\
    REPEAT_AFTER_DONE_CYCLES={self.config.burst_repeat_after_done_cycles} \\
    SEQ_START_ROW={row_start} \\
    SEQ_START_COL={col_start} \\
    DAC_VCC_SET_MV={round(self.config.read_rails.vcc_set_v * 1000)} \\
    DAC_VCC_WL_SET_MV={round(self.config.read_rails.vcc_wl_set_v * 1000)} \\
]
opt_design
place_design
route_design
write_bitstream -force [file join $script_dir $bit_name]
puts "BUILT $bit_name array-read burst start=({row_start},{col_start})"
exit
"""

    def _ensure_saleae_burst_script(self) -> None:
        script_path = ROOT / "api_v1/prerequisites/saleae_ubuntu/run_full_array_burst_capture.py"
        text = script_path.read_text()
        target = "run_full_array_burst_capture.py"
        if self.config.saleae_host:
            self._write_remote_saleae_text(target, text)
            return
        saleae_dir = Path(self.config.saleae_dir)
        saleae_dir.mkdir(parents=True, exist_ok=True)
        target_path = saleae_dir / target
        if not target_path.exists() or target_path.read_text() != text:
            target_path.write_text(text)

    def _ensure_saleae_capture_script(self) -> None:
        if self.config.dry_run or self._saleae_capture_script_ready:
            return
        script_path = ROOT / "api_v1/prerequisites/saleae_ubuntu/run_fpga_scan0000_la12_15_capture.py"
        text = script_path.read_text()
        target = "run_fpga_scan0000_la12_15_capture.py"
        if self.config.saleae_host:
            self._write_remote_saleae_text(target, text)
        else:
            saleae_dir = Path(self.config.saleae_dir)
            saleae_dir.mkdir(parents=True, exist_ok=True)
            target_path = saleae_dir / target
            if not target_path.exists() or target_path.read_text() != text:
                target_path.write_text(text)
        summarizer_path = ROOT / "api_v1/tools/summarize_capture.py"
        summarizer_text = summarizer_path.read_text()
        if self.config.saleae_host:
            self._write_remote_saleae_text("summarize_capture.py", summarizer_text)
        else:
            target_path = Path(self.config.saleae_dir) / "summarize_capture.py"
            if not target_path.exists() or target_path.read_text() != summarizer_text:
                target_path.write_text(summarizer_text)
        self._saleae_capture_script_ready = True

    def _ensure_remote_fpga_sources(self) -> None:
        source_dir = ROOT / "api_v1/prerequisites/fpga_zynq7020"
        for filename in (
            "caravel_scan_debug_fpga.v",
            "dac81416_spi.v",
            "caravel_scan_debug_runtime.v",
            "dac81416_runtime_spi.v",
            "caravel_scan_debug_fpga.xdc",
            "build_runtime_bitstream.tcl",
            "program_and_run_runtime.tcl",
            "runtime_vio_daemon.tcl",
        ):
            self._write_remote_binary(filename, (source_dir / filename).read_bytes())

    def _ensure_runtime_bitstream(self, *, force: bool = False) -> str:
        if self.config.dry_run or (self._runtime_bitstream_ready and not force):
            return FPGA_RUNTIME_BITSTREAM

        if force:
            self._stop_runtime_vio_daemon()

        local_bitstream = FPGA_BITSTREAM_DIR / FPGA_RUNTIME_BITSTREAM
        local_probes = FPGA_BITSTREAM_DIR / FPGA_RUNTIME_PROBES
        remote_ready = (
            not force
            and self._remote_file_exists(FPGA_RUNTIME_BITSTREAM)
            and self._remote_file_exists(FPGA_RUNTIME_PROBES)
            and self._remote_file_exists("program_and_run_runtime.tcl")
            and self._remote_file_exists("runtime_vio_daemon.tcl")
        )
        if not remote_ready:
            self._ensure_remote_fpga_sources()
            if not force and local_bitstream.exists() and local_probes.exists():
                self._write_remote_binary(FPGA_RUNTIME_BITSTREAM, local_bitstream.read_bytes())
                self._write_remote_binary(FPGA_RUNTIME_PROBES, local_probes.read_bytes())
            else:
                proc = self._run_zynq(
                    f"{self.config.vivado_cmd} -mode batch -source build_runtime_bitstream.tcl",
                    timeout_s=900,
                )
                if proc.returncode != 0:
                    raise RuntimeError(proc.stdout or "Vivado runtime bitstream build failed")

        FPGA_BITSTREAM_DIR.mkdir(parents=True, exist_ok=True)
        if force or not local_bitstream.exists():
            local_bitstream.write_bytes(self._read_remote_binary(FPGA_RUNTIME_BITSTREAM))
        if force or not local_probes.exists():
            local_probes.write_bytes(self._read_remote_binary(FPGA_RUNTIME_PROBES))

        self._runtime_bitstream_ready = True
        return FPGA_RUNTIME_BITSTREAM

    def _write_remote_saleae_text(self, filename: str, text: str) -> None:
        if not self.config.saleae_host:
            raise RuntimeError("remote Saleae upload requires saleae_host")
        data = text.encode()
        digest = hashlib.sha256(data).hexdigest()
        filename_q = self._sh_quote(filename)
        digest_q = self._sh_quote(digest)
        check = self._run_saleae(
            f"test -f {filename_q} && "
            f"test \"$(sha256sum {filename_q} | cut -d ' ' -f 1)\" = {digest_q}",
            timeout_s=30,
        )
        if check.returncode == 0:
            return

        # Transfer the script as a file rather than embedding it in an SSH
        # command. Large command payloads are unreliable on Windows and can
        # leave an otherwise healthy SSH session waiting until its timeout.
        upload_id = uuid.uuid4().hex
        upload_name = f".{filename}.{upload_id}.upload"
        upload_q = self._sh_quote(upload_name)
        local_upload = self.config.run_dir / upload_name
        local_upload.write_bytes(data)
        remote_path = f"{self.config.saleae_dir.rstrip('/')}/{upload_name}"
        errors: list[str] = []
        transferred = False
        try:
            for tool_name, tool_args in (("scp", []), ("rsync", ["-a"])):
                tool = shutil.which(tool_name)
                if not tool:
                    continue
                target = f"{self.config.saleae_host}:{remote_path}"
                proc = self.runner.run([tool, *tool_args, str(local_upload), target], timeout_s=180)
                if proc.returncode == 0:
                    transferred = True
                    break
                errors.append(f"{tool_name}: {proc.stdout.strip()}")
            if not transferred:
                detail = "; ".join(errors) or "scp and rsync were not found on PATH"
                raise RuntimeError(f"Saleae script upload failed: {detail}")
            proc = self._run_saleae(
                f"chmod 755 {upload_q} && mv -f {upload_q} {filename_q}",
                timeout_s=60,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stdout or f"remote install failed with exit code {proc.returncode}")
        except Exception:
            self._run_saleae(f"rm -f {upload_q}", timeout_s=30)
            raise
        finally:
            local_upload.unlink(missing_ok=True)

    def _run_saleae(self, command: str, timeout_s: int | None = None) -> subprocess.CompletedProcess[str]:
        full_command = f"cd {self.config.saleae_dir} && {command}"
        if self.config.saleae_host:
            return self.runner.ssh(self.config.saleae_host, full_command, timeout_s=timeout_s)
        return self.runner.run(self._local_shell_command(full_command), timeout_s=timeout_s)

    def _capture_array_burst(
        self,
        packet: int,
        rails: RailVoltages,
        bitstream: str,
        index: int,
        max_cells: int,
        row_start: int,
        col_start: int,
        burst_label: str = "burst read",
        cells_done: int | None = None,
        total_cells: int | None = None,
    ) -> str:
        env = {
            "ADC_DAC_PORT": self.config.adc_dac_port,
            "DIGITAL_SAMPLE_RATE": str(self.config.digital_sample_rate),
            "ANALOG_SAMPLE_RATE": str(self.config.analog_sample_rate),
            "DIGITAL_THRESHOLD_VOLTS": str(self.config.digital_threshold_volts),
            "SHUNT_OHMS": str(self.config.shunt_ohms),
            "RAIL_COMMAND": rails.command,
            "VCC_SET_V": str(rails.vcc_set_v),
            "VCC_WL_SET_V": str(rails.vcc_wl_set_v),
            "ENABLE_ADC_MONITOR": "1" if self.config.enable_adc_monitor else "0",
            "SKIP_SET_RAILS": "1" if self.config.fpga_dac_enabled else "0",
            "START_ROW": str(row_start),
            "START_COL": str(col_start),
            "MAX_CELLS": str(max_cells),
            "CAPTURE_STRATEGY": self.config.burst_capture_strategy,
            "POST_DR_TM_HOLD_CYCLES": str(self.config.burst_post_dr_tm_hold_cycles),
            "FPGA_RESET_ASSERT_CYCLES": str(self.config.burst_fpga_reset_assert_cycles),
            "RESET_RELEASE_FALLBACK_CYCLES": str(self.config.burst_reset_release_fallback_cycles),
            "POST_RESET_WAIT_CYCLES": str(self.config.burst_post_reset_wait_cycles),
            "REPEAT_AFTER_DONE_CYCLES": str(self.config.burst_repeat_after_done_cycles),
            "WB_CLK_PERIOD_SECONDS": str(self.config.burst_wb_clk_period_seconds),
            "FULL_ARRAY_PACKET_PERIOD_SECONDS": str(self.config.full_array_burst_packet_period_seconds),
            "MEASURE_SKIP_END_CYCLES": "3",
            "AFTER_TRIGGER_SECONDS": str(self._burst_after_trigger_seconds(max_cells)),
            "TRIM_DATA_SECONDS": str(self.config.burst_trim_data_seconds),
            "STOP_ON_MISMATCH": "0",
            "TRIGGER_CHANNEL_INDEX": "11",
            "TRIGGER_TYPE": "FALLING",
        }
        if self.config.burst_capture_strategy == "single" and max_cells > 128:
            env["DIGITAL_SAMPLE_RATE"] = str(self.config.full_array_burst_digital_sample_rate)
            env["ANALOG_SAMPLE_RATE"] = str(self.config.full_array_burst_analog_sample_rate)
            env["FULL_ARRAY_DETERMINISTIC_TIMING"] = "1"
        elif self.config.burst_capture_strategy == "single":
            env["ANALOG_SAMPLE_RATE"] = str(self.config.burst_analog_sample_rate)
        env_text = " ".join(f"{k}={self._sh_quote(v)}" for k, v in env.items())
        capture_cmd = f"env {env_text} {self.config.saleae_burst_capture_script}"
        capture_log = self.config.run_dir / f"capture_{index}_read_array_burst.log"
        # One extra slot lets an automatic recovery action (USB reset, Logic restart,
        # DAC Teensy reflash) happen on the last configured attempt and still retry.
        attempts = max(1, self.config.attempts) + 1
        failures: list[str] = []
        restarted_saleae = False
        progress_kwargs = {
            "cells": cells_done,
            "total": total_cells,
        } if cells_done is not None and total_cells is not None else {}
        for attempt in range(1, attempts + 1):
            attempt_log = capture_log if attempts == 1 else self.config.run_dir / f"capture_{index}_read_array_burst_attempt{attempt}.log"
            capture_proc = self._popen_saleae(capture_cmd)
            output_lines: list[str] = []
            reader_done = threading.Event()

            def read_capture_stdout() -> None:
                try:
                    if capture_proc.stdout is not None:
                        for line in capture_proc.stdout:
                            output_lines.append(line)
                finally:
                    reader_done.set()

            threading.Thread(target=read_capture_stdout, daemon=True).start()
            armed = False
            arm_deadline = time.monotonic() + 60.0
            while time.monotonic() < arm_deadline:
                if any("SINGLE_CAPTURE_ARMED" in line or line.startswith("ARMED ") for line in output_lines):
                    armed = True
                    break
                if capture_proc.poll() is not None:
                    break
                time.sleep(0.05)

            program_rc = -1
            if armed:
                self._append_progress("read-array", f"Programming FPGA for {burst_label}", mode="burst")
                program_rc = self._program_fpga(
                    bitstream,
                    packet=packet,
                    rails=rails,
                    packet_count=max_cells,
                )
                self._append_progress("read-array", f"Saleae capturing {burst_label}", mode="burst")
            else:
                self._append_progress("read-array", f"Saleae did not arm for {burst_label}", mode="burst")
                capture_proc.terminate()
            timed_out = False
            capture_timeout_s = (
                self.config.full_array_burst_capture_timeout_seconds
                if self.config.burst_capture_strategy == "single" and max_cells > 128
                else self.config.burst_capture_timeout_seconds
            )
            try:
                capture_proc.wait(timeout=capture_timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                capture_proc.kill()
                capture_proc.wait()
                output_lines.append(f"\nTIMEOUT after {capture_timeout_s:.0f}s waiting for Saleae burst capture\n")
            reader_done.wait(timeout=2.0)
            output = "".join(output_lines)
            attempt_log.write_text(output or "")
            remote_output_dir = ""
            for line in (output or "").splitlines():
                if line.startswith("OUTPUT_ROOT="):
                    remote_output_dir = line.split("=", 1)[1].strip()
                elif line.startswith("DONE output_root="):
                    remote_output_dir = line.split("output_root=", 1)[1].split()[0].strip()
            capture_rc = capture_proc.returncode
            if capture_rc == 0 and program_rc == 0 and remote_output_dir:
                if attempts > 1:
                    capture_log.write_text(f"SUCCESS attempt={attempt}; see {attempt_log}\n")
                return remote_output_dir

            reason = (
                f"attempt={attempt} capture_rc={capture_rc} program_rc={program_rc} "
                f"remote_output_dir={remote_output_dir or '<missing>'} log={attempt_log}"
            )
            if timed_out:
                reason += " timeout=true"
            failures.append(reason)
            if attempt < attempts:
                should_restart = timed_out or self._saleae_needs_restart(output or "")
                if self._dac_teensy_needs_reflash(output or ""):
                    self._append_progress(
                        "read-array",
                        f"{burst_label.capitalize()}: reflashing DAC Teensy after serial write timeout",
                        mode="burst",
                        **progress_kwargs,
                    )
                    reflash_log = self._reflash_dac_teensy(index, "read_array_burst", attempt)
                    failures.append(f"dac_teensy_reflash_after_attempt={attempt} log={reflash_log}")
                elif self._usb_needs_recovery(output or ""):
                    self._append_progress(
                        "read-array",
                        f"{burst_label.capitalize()}: recovering Ubuntu USB after attempt {attempt}",
                        mode="burst",
                        **progress_kwargs,
                    )
                    recovery_log = self._recover_saleae_usb(index, "read_array_burst", attempt)
                    failures.append(f"usb_recovery_after_attempt={attempt} log={recovery_log}")
                    restarted_saleae = True
                elif should_restart:
                    self._append_progress(
                        "read-array",
                        f"{burst_label.capitalize()}: restarting capture service after attempt {attempt}",
                        mode="burst",
                        **progress_kwargs,
                    )
                    restart_log = self._restart_saleae_automation(index, "read_array_burst", attempt)
                    failures.append(f"saleae_restart_after_attempt={attempt} log={restart_log}")
                    restarted_saleae = True
                elif restarted_saleae:
                    self._append_progress(
                        "read-array",
                        f"{burst_label.capitalize()}: retrying capture after restart",
                        mode="burst",
                        **progress_kwargs,
                    )
                else:
                    self._append_progress(
                        "read-array",
                        f"{burst_label.capitalize()}: retrying capture after attempt {attempt}",
                        mode="burst",
                        **progress_kwargs,
                    )
                time.sleep(2.0)

        capture_log.write_text("\n".join(failures) + "\n")
        raise RuntimeError(
            f"burst capture/program failed {burst_label} after {attempts} attempts; "
            f"see {capture_log}"
        )

    def _burst_after_trigger_seconds(self, max_cells: int) -> float:
        if self.config.burst_capture_strategy != "single":
            return self.config.burst_after_trigger_seconds
        cycles_per_cell = (
            (self.config.burst_fpga_reset_assert_cycles + 1)
            + (self.config.burst_reset_release_fallback_cycles + 1)
            + (self.config.burst_post_reset_wait_cycles + 1)
            + 1
            + 18
            + self.config.burst_post_dr_tm_hold_cycles
            + (self.config.burst_repeat_after_done_cycles + 1)
        )
        estimated = cycles_per_cell * max(1, max_cells) * self.config.burst_wb_clk_period_seconds
        return max(self.config.burst_after_trigger_seconds, estimated + self.config.burst_single_capture_margin_seconds)

    def _append_burst_manifest(self, local_output_dir: Path, remote_output_dir: str, bitstream: str) -> list[dict[str, object]]:
        burst_manifest = local_output_dir / "manifest.csv"
        if not burst_manifest.exists():
            raise RuntimeError(f"burst manifest missing: {burst_manifest}")
        with burst_manifest.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        expected_packets = [int(str(row["packet"]), 16) for row in rows]
        expected_packet_set = set(expected_packets)
        rows_by_decoded_packet: dict[int, dict[str, str]] = {}
        for row in rows:
            decoded_text = str(row.get("decoded_packet", "")).strip()
            if not decoded_text:
                continue
            decoded_packet = int(decoded_text, 16)
            if decoded_packet in expected_packet_set and cell_from_packet(decoded_packet) is not None:
                rows_by_decoded_packet[decoded_packet] = row

        reads: list[dict[str, object]] = []
        next_index = self._next_index()
        for offset, packet in enumerate(expected_packets):
            row = rows_by_decoded_packet.get(packet)
            if row is None:
                continue
            cell = cell_from_packet(packet)
            if cell is None:
                continue
            packet_text = f"0x{packet:04x}"
            current = self._float_or_none(row.get("la_set_mean_uA"))
            result = CellOperationResult(
                cell=cell,
                operation="read",
                packet=packet_text,
                rails=self.config.read_rails,
                current_uA=current,
                decoded_packet=packet_text,
                ok=True,
                local_output_dir=str(local_output_dir),
                error=str(row.get("error", "")),
            )
            self._append_manifest(next_index + offset, "array_burst", "read", result, bitstream, bits_lsb(packet))
            reads.append(
                {
                    "cell": asdict(cell),
                    "operation": "read",
                    "packet": packet_text,
                    "rails": asdict(self.config.read_rails),
                    "current_uA": current,
                    "decoded_packet": result.decoded_packet,
                    "ok": result.ok,
                    "local_output_dir": str(local_output_dir),
                    "remote_output_dir": remote_output_dir,
                    "error": result.error,
                }
            )
            if (offset + 1) % 64 == 0:
                self._append_progress("read-array", "Publishing burst reads", cells=offset + 1)
        return reads

    def _validate_burst_manifest(self, local_output_dir: Path, expected_count: int) -> str:
        burst_manifest = local_output_dir / "manifest.csv"
        if not burst_manifest.exists():
            return f"burst manifest missing: {burst_manifest}"
        with burst_manifest.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        if len(rows) < expected_count:
            return f"burst manifest has {len(rows)} rows, expected {expected_count}"
        saleae_errors = [
            str(row.get("error", ""))
            for row in rows
            if row.get("error") and self._saleae_needs_restart(str(row.get("error", "")))
        ]
        if saleae_errors:
            return saleae_errors[0][:220]
        try:
            expected_packets = [int(str(row["packet"]), 16) for row in rows]
        except (KeyError, TypeError, ValueError) as exc:
            return f"burst manifest has invalid packet field: {exc}"

        decoded_packets: list[int] = []
        invalid_decodes: list[str] = []
        for row in rows:
            decoded_text = str(row.get("decoded_packet", "")).strip()
            if not decoded_text:
                continue
            try:
                decoded_packet = int(decoded_text, 16)
            except ValueError:
                invalid_decodes.append(decoded_text)
                continue
            if cell_from_packet(decoded_packet) is None:
                invalid_decodes.append(decoded_text)
                continue
            decoded_packets.append(decoded_packet)
        if invalid_decodes:
            sample = ", ".join(invalid_decodes[:4])
            return f"burst manifest has invalid decoded packets: {sample}"

        expected_set = set(expected_packets)
        decoded_set = set(decoded_packets)
        missing = sorted(expected_set - decoded_set)
        unexpected = sorted(decoded_set - expected_set)
        duplicates = sorted(packet for packet in decoded_set if decoded_packets.count(packet) > 1)
        if missing or unexpected or duplicates:
            parts = []
            if missing:
                parts.append("missing decoded packets: " + ", ".join(f"0x{packet:04x}" for packet in missing[:8]))
            if unexpected:
                parts.append("unexpected decoded packets: " + ", ".join(f"0x{packet:04x}" for packet in unexpected[:8]))
            if duplicates:
                parts.append("duplicate decoded packets: " + ", ".join(f"0x{packet:04x}" for packet in duplicates[:8]))
            return "burst manifest " + "; ".join(parts)
        return ""

    def _count_valid_burst_packets(self, local_output_dir: Path) -> int:
        burst_manifest = local_output_dir / "manifest.csv"
        if not burst_manifest.exists():
            return 0
        with burst_manifest.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        expected_packets: set[int] = set()
        for row in rows:
            try:
                expected_packets.add(int(str(row["packet"]), 16))
            except (KeyError, TypeError, ValueError):
                continue
        decoded_packets: set[int] = set()
        for row in rows:
            decoded_text = str(row.get("decoded_packet", "")).strip()
            if not decoded_text:
                continue
            try:
                decoded_packet = int(decoded_text, 16)
            except ValueError:
                continue
            if decoded_packet in expected_packets and cell_from_packet(decoded_packet) is not None:
                decoded_packets.add(decoded_packet)
        return len(decoded_packets)

    def _capture_remote(self, packet: int, rails: RailVoltages, bitstream: str, index: int, kind: str) -> str:
        env = {
            "ADC_DAC_PORT": self.config.adc_dac_port,
            "RESET_MODE": "none",
            "PRE_RESET_DELAY_SECONDS": "0",
            "TRIGGER_CHANNEL": str(self.config.trigger_channel),
            "TRIGGER_EDGE": self.config.trigger_edge,
            "AFTER_TRIGGER_SECONDS": str(self.config.after_trigger_seconds),
            "TRIM_DATA_SECONDS": str(self.config.trim_data_seconds),
            "DIGITAL_SAMPLE_RATES": str(self.config.digital_sample_rate),
            "ANALOG_SAMPLE_RATE": str(self.config.analog_sample_rate),
            "ANALOG_CHANNELS": self.config.analog_channels,
            "DIGITAL_THRESHOLD_VOLTS": str(self.config.digital_threshold_volts),
            "SHUNT_OHMS": str(self.config.shunt_ohms),
            "ENABLE_ADC_MONITOR": "1" if self.config.enable_adc_monitor else "0",
            "SKIP_SET_RAILS": "1" if self.config.fpga_dac_enabled else "0",
            "SCAN_REQUEST": f"0x{packet:04x}",
            "SCAN_RAIL_COMMAND": rails.command,
            "VCC_SET_V": str(rails.vcc_set_v),
            "VCC_WL_SET_V": str(rails.vcc_wl_set_v),
        }
        env_text = " ".join(f"{k}={self._sh_quote(v)}" for k, v in env.items())
        capture_cmd = f"cd {self.config.saleae_dir} && env {env_text} {self.config.saleae_capture_script}"
        capture_log = self.config.run_dir / f"capture_{index}_{kind}.log"
        # One extra slot lets an automatic recovery action (USB reset, Logic restart,
        # DAC Teensy reflash) happen on the last configured attempt and still retry.
        attempts = max(1, self.config.attempts) + 1
        failures: list[str] = []
        for attempt in range(1, attempts + 1):
            attempt_log = capture_log if attempts == 1 else self.config.run_dir / f"capture_{index}_{kind}_attempt{attempt}.log"
            capture_proc = self._popen_saleae(capture_cmd)
            output_lines: list[str] = []
            armed = threading.Event()
            reader_done = threading.Event()

            def drain_capture_output() -> None:
                try:
                    if capture_proc.stdout is not None:
                        for line in capture_proc.stdout:
                            output_lines.append(line)
                            if line.startswith("SALEAE_ARMED"):
                                armed.set()
                finally:
                    reader_done.set()

            reader = threading.Thread(target=drain_capture_output, daemon=True)
            reader.start()
            arm_deadline = time.monotonic() + self.config.saleae_arm_timeout_seconds
            while not armed.is_set() and not reader_done.is_set() and time.monotonic() < arm_deadline:
                armed.wait(timeout=0.1)

            program_rc = -1
            if armed.is_set():
                program_rc = self._program_fpga(bitstream, packet=packet, rails=rails, packet_count=1)
                try:
                    capture_proc.wait(timeout=self.config.saleae_capture_completion_timeout_seconds)
                except subprocess.TimeoutExpired:
                    output_lines.append(
                        "SALEAE_CAPTURE_COMPLETION_TIMEOUT: capture remained active after the FPGA command; "
                        "the expected trigger may have been missed.\n"
                    )
                    capture_proc.terminate()
                    try:
                        capture_proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        capture_proc.kill()
                        capture_proc.wait(timeout=5)
            else:
                output_lines.append(
                    "SALEAE_ARM_TIMEOUT: Logic 2 did not confirm that the physical capture was armed; "
                    "the FPGA command was not sent.\n"
                )
                if capture_proc.poll() is None:
                    capture_proc.terminate()
                    try:
                        capture_proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        capture_proc.kill()
                        capture_proc.wait(timeout=5)

            reader.join(timeout=5)
            output = "".join(output_lines)
            attempt_log.write_text(output or "")
            remote_output_dir = ""
            for line in (output or "").splitlines():
                if line.startswith("OUTPUT_DIR="):
                    remote_output_dir = line.split("=", 1)[1].strip()
            if capture_proc.returncode == 0 and program_rc == 0 and remote_output_dir:
                if attempts > 1:
                    capture_log.write_text(f"SUCCESS attempt={attempt}; see {attempt_log}\n")
                return remote_output_dir

            failures.append(
                f"attempt={attempt} capture_rc={capture_proc.returncode} program_rc={program_rc} "
                f"remote_output_dir={remote_output_dir or '<missing>'} log={attempt_log}"
            )
            if self._remote_transport_needs_retry(output or ""):
                summary = "remote SSH/VM transport unavailable"
                failures.append(f"attempt={attempt} transport_error={summary}")
                self._record_recovery_event(
                    component="saleae_transport",
                    index=index,
                    kind=kind,
                    attempt=attempt,
                    error=summary,
                    action="reconnect_and_retry",
                )
            elif self._dac_teensy_needs_reflash(output or ""):
                failures.append(f"attempt={attempt} dac_teensy_error=Serial write timeout")
                reflash_log = self._reflash_dac_teensy(index, kind, attempt)
                failures.append(f"dac_teensy_reflash_after_attempt={attempt} log={reflash_log}")
            elif self._usb_needs_recovery(output or ""):
                summary = self._usb_error_summary(output or "")
                failures.append(f"attempt={attempt} usb_error={summary}")
                recovery_log = self._recover_saleae_usb(index, kind, attempt)
                failures.append(f"usb_recovery_after_attempt={attempt} log={recovery_log}")
                self._record_recovery_event(
                    component="saleae_usb",
                    index=index,
                    kind=kind,
                    attempt=attempt,
                    error=summary,
                    action="wait_for_usb_and_restart_logic",
                    recovery_log=str(recovery_log),
                )
            elif self._saleae_needs_restart(output or ""):
                summary = self._saleae_error_summary(output or "")
                failures.append(f"attempt={attempt} saleae_error={summary}")
                restart_log = self._restart_saleae_automation(index, kind, attempt)
                failures.append(f"saleae_restart_after_attempt={attempt} log={restart_log}")
                self._record_recovery_event(
                    component="saleae_logic",
                    index=index,
                    kind=kind,
                    attempt=attempt,
                    error=summary,
                    action="restart_logic",
                    recovery_log=str(restart_log),
                )
            if attempt < attempts:
                time.sleep(2.0)

        capture_log.write_text("\n".join(failures) + "\n")
        raise RuntimeError(f"capture/program failed index={index} kind={kind} after {attempts} attempts; see {capture_log}")

    def _saleae_error_summary(self, output: str) -> str:
        for line in reversed(output.splitlines()):
            if "DeviceSetupFailure" in line:
                return "DeviceSetupFailure"
            if "Connection refused" in line:
                return "Connection refused"
            if "StatusCode.UNAVAILABLE" in line:
                return "StatusCode.UNAVAILABLE"
            if "_InactiveRpcError" in line:
                return "_InactiveRpcError"
            if "failed to connect to all addresses" in line:
                return "failed to connect to all addresses"
        return "restartable Saleae error"

    def _saleae_needs_restart(self, output: str) -> bool:
        restart_markers = (
            "Failed to connect to remote host: Connection refused",
            "Connection refused",
            "DeviceSetupFailure",
            "Cannot switch sessions while recording",
            "InternalServerError",
            "failed to connect to all addresses",
            "StatusCode.UNAVAILABLE",
            "_InactiveRpcError",
        )
        return any(marker in output for marker in restart_markers)

    def _usb_error_summary(self, output: str) -> str:
        for line in reversed(output.splitlines()):
            if self.config.adc_dac_port in line and "No such file or directory" in line:
                return "ADC/DAC Teensy serial port missing"
            if "LIBUSB_ERROR_BUSY" in line:
                return "LIBUSB_ERROR_BUSY"
            if "xHCI host controller not responding" in line or "HC died" in line:
                return "xHCI controller died"
            if "No Saleae device found" in line:
                return "No Saleae device found"
            if "DeviceError: Error interacting with device during capture: ReadTimeout" in line:
                return "Saleae ReadTimeout"
        return "recoverable USB error"

    def _usb_needs_recovery(self, output: str) -> bool:
        recovery_markers = (
            f"could not open port {self.config.adc_dac_port}",
            f"No such file or directory: '{self.config.adc_dac_port}'",
            "/dev/serial/by-id",
            "No Saleae device found",
            "DeviceError: Error interacting with device during capture: ReadTimeout",
            "SALEAE_ARM_TIMEOUT",
            "SALEAE_CAPTURE_COMPLETION_TIMEOUT",
            "LIBUSB_ERROR_BUSY",
            "xHCI host controller not responding",
            "HC died; cleaning up",
        )
        return any(marker in output for marker in recovery_markers)

    @staticmethod
    def _remote_transport_needs_retry(output: str) -> bool:
        markers = (
            "ssh: connect to host",
            "Connection timed out during banner exchange",
            "Connection to ",
            "No route to host",
            "Network is unreachable",
            "Connection reset by peer",
        )
        return any(marker in output for marker in markers)

    def _record_recovery_event(self, **event: object) -> None:
        event.setdefault("time", time.time())
        path = self.config.run_dir / "hardware_recovery.jsonl"
        with path.open("a") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")

    def _dac_teensy_needs_reflash(self, output: str) -> bool:
        write_timeout = (
            "SerialTimeoutException" in output
            and "Write timeout" in output
            and ("set_scan_set_rails" in output or "SCAN_RAIL_COMMAND" in output or "SCAN_CUSTOM_RAILS" in output)
        )
        missing_configured_port = (
            self.config.adc_dac_port in output
            and "No such file or directory" in output
            and ("SerialException" in output or "could not open port" in output)
        )
        return write_timeout or missing_configured_port

    def _sudo_prefix(self) -> str:
        if self.config.saleae_sudo_password:
            return f"printf '%s\\n' {self._sh_quote(self.config.saleae_sudo_password)} | sudo -S"
        return "sudo -n"

    def _recover_saleae_usb(self, index: int, kind: str, attempt: int) -> Path:
        recovery_log = self.config.run_dir / f"saleae_usb_recovery_{index}_{kind}_after_attempt{attempt}.log"
        if not self.config.saleae_usb_recovery_enabled:
            recovery_log.write_text("USB recovery disabled by config\n")
            return recovery_log

        sudo = self._sudo_prefix()
        pci = self._sh_quote(self.config.saleae_usb_controller_pci)
        script = f"""
set -u
echo "BEFORE_LSUSB"
lsusb || true
echo "BEFORE_SERIAL"
ls -l /dev/serial/by-id/ 2>&1 || true
VIRT=$(systemd-detect-virt 2>/dev/null || true)
if [ "$VIRT" = "oracle" ]; then
  echo "VIRTUALBOX_USB_PASSTHROUGH: waiting for host attachment; guest controller reset skipped"
else
  echo "RESET_XHCI {self.config.saleae_usb_controller_pci}"
  {sudo} sh -c 'echo {pci} > /sys/bus/pci/drivers/xhci_hcd/unbind' || true
  sleep 3
  {sudo} sh -c 'echo {pci} > /sys/bus/pci/drivers/xhci_hcd/bind' || true
  sleep 8
fi
echo "WAIT_FOR_SALEAE_USB"
i=0
while ! lsusb -d 21a9:1006 >/dev/null 2>&1 && [ $i -lt 12 ]; do
  i=$((i+1))
  echo "saleae_usb_absent wait=$i/12"
  sleep 5
done
echo "AFTER_LSUSB"
lsusb || true
echo "AFTER_SERIAL"
ls -l /dev/serial/by-id/ 2>&1 || true
echo "FORCE_RESTART_LOGIC"
pkill -TERM -f '[L]ogic-linux-x64.AppImage' 2>/dev/null || true
pkill -TERM -f '[L]ogic.bin' 2>/dev/null || true
sleep 3
pkill -KILL -f '[L]ogic-linux-x64.AppImage' 2>/dev/null || true
pkill -KILL -f '[L]ogic.bin' 2>/dev/null || true
sleep 2
cd {self._sh_quote(self.config.saleae_dir)} && {self.config.saleae_restart_script}
sleep {self.config.saleae_restart_wait_seconds}
echo "PORT_10430"
ss -ltnp 2>/dev/null | grep 10430 || true
echo "SALEAE_AUTOMATION_TEST"
.venv/bin/python - <<'PY'
from saleae import automation
with automation.Manager.connect(port=10430, connect_timeout_seconds=5) as manager:
    print(manager.get_app_info())
    devices = manager.get_devices(include_simulation_devices=False)
    print([(d.device_type, d.device_id) for d in devices])
    if not devices:
        raise RuntimeError("Logic automation is listening but no physical Saleae device is attached")
PY
"""
        if self.config.saleae_host:
            proc = self.runner.ssh(self.config.saleae_host, script, timeout_s=90)
        else:
            proc = self.runner.run(self._local_shell_command(script), timeout_s=90)
        recovery_log.write_text(proc.stdout or "")
        return recovery_log

    def _reflash_dac_teensy(self, index: int, kind: str, attempt: int) -> Path:
        reflash_log = self.config.run_dir / f"dac_teensy_reflash_{index}_{kind}_after_attempt{attempt}.log"
        if not self.config.dac_teensy_reflash_enabled:
            reflash_log.write_text("DAC Teensy reflash disabled by config\n")
            return reflash_log

        loader = self._sh_quote(self.config.dac_teensy_loader)
        hex_path = self._sh_quote(self.config.dac_teensy_hex)
        mcu = self._sh_quote(self.config.dac_teensy_mcu)
        app_serial = self._sh_quote(self.config.dac_teensy_app_serial)
        boot_serial = self._sh_quote(self.config.dac_teensy_bootloader_serial)
        port = self._sh_quote(self.config.adc_dac_port)
        script = f"""
set -u
echo "BEFORE_SERIAL"
ls -l /dev/serial/by-id/ 2>&1 || true
echo "KILL_STALE_ACM_HOLDERS"
fuser -k {port} 2>/dev/null || true
sleep 1
echo "REFLASH_DAC_TEENSY app={self.config.dac_teensy_app_serial} boot={self.config.dac_teensy_bootloader_serial}"
TEENSY_LOADER_SERIAL={app_serial} TEENSY_LOADER_SERIAL_ALT={boot_serial} \\
  {loader} --mcu={mcu} -s -w -v {hex_path}
sleep 5
echo "AFTER_SERIAL"
ls -l /dev/serial/by-id/ 2>&1 || true
echo "DAC_TEENSY_SMOKE"
python3 - <<'PY' || true
import serial, time
port = {self.config.adc_dac_port!r}
for command in ("SCAN_CUSTOM_RAILS 1000 2500", "SCAN_CUSTOM_RAILS 0 0"):
    s = serial.Serial(port, 115200, timeout=1, write_timeout=3)
    time.sleep(0.8)
    s.reset_input_buffer()
    s.reset_output_buffer()
    s.write((command + "\\n").encode())
    s.flush()
    time.sleep(0.8)
    print(command, "=>", s.read(s.in_waiting or 200).decode(errors="replace").strip())
    s.close()
PY
"""
        if self.config.saleae_host:
            proc = self.runner.ssh(self.config.saleae_host, script, timeout_s=90)
        else:
            proc = self.runner.run(self._local_shell_command(script), timeout_s=90)
        reflash_log.write_text(proc.stdout or "")
        return reflash_log

    def _restart_saleae_automation(self, index: int, kind: str, attempt: int) -> Path:
        restart_log = self.config.run_dir / f"saleae_restart_{index}_{kind}_after_attempt{attempt}.log"
        command = (
            "pkill -TERM -f '[r]un_full_array_burst_capture.py' 2>/dev/null || true; "
            "pkill -TERM -f '[L]ogic-linux-x64.AppImage' 2>/dev/null || true; "
            "pkill -TERM -f '[L]ogic.bin' 2>/dev/null || true; "
            "sleep 3; "
            "pkill -KILL -f '[r]un_full_array_burst_capture.py' 2>/dev/null || true; "
            "pkill -KILL -f '[L]ogic-linux-x64.AppImage' 2>/dev/null || true; "
            "pkill -KILL -f '[L]ogic.bin' 2>/dev/null || true; "
            f"sleep 2; {self.config.saleae_restart_script}; "
            f"sleep {self.config.saleae_restart_wait_seconds}; "
            ".venv/bin/python - <<'PY'\n"
            "from saleae import automation\n"
            "with automation.Manager.connect(port=10430, connect_timeout_seconds=5) as manager:\n"
            "    devices = manager.get_devices(include_simulation_devices=False)\n"
            "    print([(d.device_type, d.device_id) for d in devices])\n"
            "    if not devices:\n"
            "        raise RuntimeError('Logic automation is listening but no physical Saleae device is attached')\n"
            "PY"
        )
        if self.config.saleae_host:
            proc = self.runner.ssh(
                self.config.saleae_host,
                f"cd {self.config.saleae_dir} && {command}",
                timeout_s=60,
            )
        else:
            proc = self.runner.run(
                self._local_shell_command(
                    f"cd {self.config.saleae_dir} && {command}"
                ),
                timeout_s=60,
            )
        restart_log.write_text(proc.stdout or "")
        return restart_log

    @staticmethod
    def _runtime_command_payload(packet: int, rails: RailVoltages, packet_count: int = 1) -> str:
        op_set = (packet >> 15) & 1
        cell = cell_from_packet(packet, op_set=op_set)
        if cell is None:
            raise ValueError(f"invalid runtime scan packet 0x{packet:04x}")
        if not 1 <= packet_count <= 1024:
            raise ValueError(f"runtime packet count must be 1..1024, got {packet_count}")
        if not 0.0 <= rails.vcc_set_v <= 10.0:
            raise ValueError(f"Vcc_set must be 0..10 V, got {rails.vcc_set_v}")
        if not 0.0 <= rails.vcc_wl_set_v <= 5.0:
            raise ValueError(f"Vcc_wl_set must be 0..5 V, got {rails.vcc_wl_set_v}")

        vcc_set_code = round(rails.vcc_set_v * 65535 / 10.0)
        vcc_wl_set_code = round(rails.vcc_wl_set_v * 65535 / 5.0)
        payload = (
            (1 << 63)
            | (op_set << 62)
            | (cell.row << 57)
            | (cell.col << 52)
            | (packet_count << 41)
            | (vcc_set_code << 25)
            | (vcc_wl_set_code << 9)
        )
        return f"0x{payload:016X}"

    def _ensure_runtime_vio_daemon(self, bitstream: str = FPGA_RUNTIME_BITSTREAM) -> None:
        if self.config.dry_run or not self.config.persistent_fpga_runtime or self._runtime_daemon_ready:
            return
        if self.config.zynq_os != "windows":
            raise RuntimeError("persistent FPGA runtime currently requires the Windows Zynq host")
        if self.config.zynq_password:
            raise RuntimeError("persistent FPGA runtime requires SSH key authentication")

        self._ensure_remote_fpga_sources()
        timeout_s = max(10.0, self.config.runtime_daemon_start_timeout_seconds)
        # Keep recovery commands separate. Compound CMD commands can leave the
        # Windows OpenSSH session open after a forced disconnect.
        self._run_zynq_cmd("taskkill /IM vivado.exe /F", timeout_s=15)
        self._run_zynq_cmd(
            "del /Q runtime_vio_daemon.heartbeat runtime_vio_daemon.stop "
            "runtime_vio_request.txt runtime_vio_response.*.txt",
            timeout_s=15,
        )

        remote_daemon_log = f"runtime_vio_daemon.{uuid.uuid4().hex}.log"
        daemon_command = (
            f"& '{self.config.vivado_cmd}' -mode batch -source runtime_vio_daemon.tcl "
            f"-tclargs '{bitstream}' '{FPGA_RUNTIME_PROBES}' *> '{remote_daemon_log}'; "
            "$rc=$LASTEXITCODE; Write-Output ('RUNTIME_VIO_DAEMON_EXIT='+$rc); exit $rc"
        )
        encoded = base64.b64encode(daemon_command.encode("utf-16le")).decode()
        full_command = f"cd {self.config.zynq_dir} && powershell -NoProfile -EncodedCommand {encoded}"
        log_path = self.config.run_dir / "runtime_vio_daemon_ssh.log"
        self._runtime_daemon_log_handle = log_path.open("a")
        if self.config.zynq_host:
            self._runtime_daemon_process = subprocess.Popen(
                ["ssh", "-o", "ConnectTimeout=15", self.config.zynq_host, full_command],
                stdin=subprocess.DEVNULL,
                text=True,
                stdout=self._runtime_daemon_log_handle,
                stderr=subprocess.STDOUT,
            )
        else:
            self._runtime_daemon_process = subprocess.Popen(
                self._local_shell_command(full_command),
                stdin=subprocess.DEVNULL,
                text=True,
                stdout=self._runtime_daemon_log_handle,
                stderr=subprocess.STDOUT,
            )

        deadline = time.time() + timeout_s
        ready = False
        while time.time() < deadline:
            try:
                proc = self._run_zynq_cmd("dir /B runtime_vio_daemon.heartbeat", timeout_s=30)
            except subprocess.TimeoutExpired:
                continue
            if proc.returncode == 0 and "runtime_vio_daemon.heartbeat" in (proc.stdout or ""):
                ready = True
                break
            time.sleep(1.0)
        if not ready:
            proc = self._run_zynq_cmd(f"type {remote_daemon_log}", timeout_s=10)
            if self._runtime_daemon_process and self._runtime_daemon_process.poll() is None:
                self._runtime_daemon_process.terminate()
            if self._runtime_daemon_log_handle:
                self._runtime_daemon_log_handle.close()
            raise RuntimeError(proc.stdout or "persistent FPGA runtime daemon did not start")
        self._runtime_daemon_ready = True

    def _program_fpga_via_runtime_daemon(self, payload: str) -> int:
        self._ensure_runtime_vio_daemon()
        request_id = uuid.uuid4().hex
        timeout_s = max(5.0, self.config.runtime_command_timeout_seconds)
        response_file = f"runtime_vio_response.{request_id}.txt"
        temp_file = f"runtime_vio_request.{request_id}.tmp"
        try:
            write = self._run_zynq_cmd(f"echo {request_id} {payload}>{temp_file}", timeout_s=30)
        except subprocess.TimeoutExpired:
            # The CMD redirection completes before Windows OpenSSH occasionally
            # delays closing the channel. The following move verifies the file.
            write = subprocess.CompletedProcess([], 0, "", "")
        try:
            move = self._run_zynq_cmd(f"move /Y {temp_file} runtime_vio_request.txt", timeout_s=30)
        except subprocess.TimeoutExpired:
            # Continue to the unique response poll, which is the authoritative
            # acknowledgement that the move and FPGA command completed.
            move = subprocess.CompletedProcess([], 0, "", "")
        if write.returncode != 0 or move.returncode != 0:
            self._runtime_daemon_ready = False
            raise RuntimeError(write.stdout or move.stdout or f"could not submit runtime command {request_id}")
        deadline = time.time() + timeout_s
        reply = ""
        while time.time() < deadline:
            try:
                exists = self._run_zynq_cmd(f"dir /B {response_file}", timeout_s=10)
            except subprocess.TimeoutExpired:
                continue
            if exists.returncode == 0 and response_file in (exists.stdout or ""):
                response = self._run_zynq_cmd(f"type {response_file}", timeout_s=10)
                reply = (response.stdout or "").strip()
                break
            time.sleep(0.25)
        try:
            self._run_zynq_cmd(f"del /Q {response_file}", timeout_s=10)
        except subprocess.TimeoutExpired:
            pass
        if f"{request_id} OK " not in reply:
            self._runtime_daemon_ready = False
            raise RuntimeError(reply or f"persistent FPGA runtime command {request_id} timed out")
        return 0

    def _stop_runtime_vio_daemon(self) -> None:
        self._runtime_daemon_ready = False
        if self.config.dry_run or self.config.zynq_os != "windows":
            return
        self._run_zynq_cmd("echo stop>runtime_vio_daemon.stop", timeout_s=10)
        deadline = time.time() + 10.0
        while time.time() < deadline:
            exists = self._run_zynq_cmd("dir /B runtime_vio_daemon.heartbeat", timeout_s=10)
            if exists.returncode != 0:
                break
            time.sleep(0.5)
        else:
            self._run_zynq_cmd("taskkill /IM vivado.exe /F", timeout_s=15)
        self._run_zynq_cmd("del /Q runtime_vio_daemon.heartbeat runtime_vio_daemon.stop", timeout_s=10)
        if self._runtime_daemon_process is not None:
            try:
                self._runtime_daemon_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._runtime_daemon_process.terminate()
                try:
                    self._runtime_daemon_process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._runtime_daemon_process.kill()
                    self._runtime_daemon_process.wait(timeout=3)
            self._runtime_daemon_process = None
        if self._runtime_daemon_log_handle is not None:
            self._runtime_daemon_log_handle.close()
            self._runtime_daemon_log_handle = None

    def _program_fpga(
        self,
        bitstream: str,
        *,
        packet: int | None = None,
        rails: RailVoltages | None = None,
        packet_count: int = 1,
    ) -> int:
        if self.config.fpga_dac_enabled:
            if packet is None or rails is None:
                raise ValueError("runtime FPGA programming requires packet and rails")
            payload = self._runtime_command_payload(packet, rails, packet_count)
            if self.config.persistent_fpga_runtime:
                return self._program_fpga_via_runtime_daemon(payload)
            probes = FPGA_RUNTIME_PROBES
            if self.config.zynq_os == "windows":
                command = (
                    f"& '{self.config.vivado_cmd}' -mode batch -source program_and_run_runtime.tcl "
                    f"-tclargs '{bitstream}' '{probes}' '{payload}' *> vivado_api_program.log; "
                    "$vivado_exit = $LASTEXITCODE; "
                    "Get-Process hw_server -ErrorAction SilentlyContinue | Stop-Process -Force; "
                    "Write-Output ('VIVADO_EXIT=' + $vivado_exit); "
                    "exit $vivado_exit"
                )
                proc = self._run_zynq_powershell(command, timeout_s=180)
            else:
                proc = self._run_zynq(
                    f"{self.config.vivado_cmd} -mode batch -source program_and_run_runtime.tcl "
                    f"-tclargs {self._sh_quote(bitstream)} {self._sh_quote(probes)} {self._sh_quote(payload)}",
                    timeout_s=180,
                )
            return proc.returncode

        if self.config.zynq_os == "windows":
            command = (
                f"Copy-Item -Force {bitstream} caravel_scan_debug_fpga.bit; "
                f"& '{self.config.vivado_cmd}' -mode batch -source program_scan_debug_zynq7020.tcl "
                "*> vivado_api_program.log; "
                "$vivado_exit = $LASTEXITCODE; "
                # Vivado can leave child processes holding the SSH session's stdout handle
                # after batch programming has finished. Redirect native output to a remote
                # log and terminate hw_server so ssh exits instead of falsely timing out.
                "Get-Process hw_server -ErrorAction SilentlyContinue | Stop-Process -Force; "
                "Write-Output ('VIVADO_EXIT=' + $vivado_exit); "
                "exit $vivado_exit"
            )
            proc = self._run_zynq_powershell(command, timeout_s=180)
        else:
            proc = self._run_zynq(
                f"cp -f {self._sh_quote(bitstream)} caravel_scan_debug_fpga.bit && "
                f"{self.config.vivado_cmd} -mode batch -source program_scan_debug_zynq7020.tcl",
                timeout_s=180,
            )
        return proc.returncode

    def _capture_local_path(self, remote_output_dir: str, index: int, kind: str, rails: RailVoltages) -> Path:
        return self.config.run_dir / "raw" / (
            f"{index}_{kind}_wl{round(rails.vcc_wl_set_v * 1000):.0f}_{Path(remote_output_dir).name}"
        )

    def _copy_capture(self, remote_output_dir: str, index: int, kind: str, rails: RailVoltages) -> Path:
        local = self._capture_local_path(remote_output_dir, index, kind, rails)
        if local.exists():
            shutil.rmtree(local)
        if self.config.saleae_host:
            rsync = shutil.which("rsync")
            if rsync:
                proc = self.runner.run([rsync, "-a", f"{self.config.saleae_host}:{remote_output_dir}/", f"{local}/"])
            else:
                scp = shutil.which("scp")
                if not scp:
                    raise RuntimeError("copying a remote Saleae capture requires rsync or scp on PATH")
                local.mkdir(parents=True)
                proc = self.runner.run([scp, "-r", f"{self.config.saleae_host}:{remote_output_dir}/.", str(local)])
            if proc.returncode != 0:
                raise RuntimeError(proc.stdout)
        else:
            shutil.copytree(remote_output_dir, local)
        return local

    def _schedule_capture_copy(self, remote_output_dir: str, index: int, kind: str, rails: RailVoltages) -> None:
        errors: list[BaseException] = []

        def copy_worker() -> None:
            try:
                self._copy_capture(remote_output_dir, index, kind, rails)
            except BaseException as exc:  # retained and raised by the owning hardware operation
                errors.append(exc)

        thread = threading.Thread(target=copy_worker, name=f"capture-copy-{index}", daemon=False)
        self._pending_capture_copies.append((thread, errors))
        thread.start()

    def _wait_for_pending_capture_copies(self) -> None:
        failures: list[str] = []
        pending, self._pending_capture_copies = self._pending_capture_copies, []
        for thread, errors in pending:
            thread.join()
            failures.extend(str(exc) for exc in errors)
        if failures:
            raise RuntimeError("deferred capture copy failed: " + "; ".join(failures))

    def _summarize_remote_capture(
        self,
        index: int,
        stage: str,
        kind: str,
        packet: int,
        rails: RailVoltages,
        remote_output_dir: str,
        local_output_dir: Path,
    ) -> dict[str, str]:
        tmp = f".manifest_summary_{index}_{uuid.uuid4().hex}.csv"
        args = [
            "--index", str(index),
            "--phase", stage,
            "--packet", f"0x{packet:04x}",
            "--bits", bits_lsb(packet),
            "--vcc-set-v", str(rails.vcc_set_v),
            "--vcc-wl-set-v", str(rails.vcc_wl_set_v),
            "--remote-output-dir", remote_output_dir,
            "--local-output-dir", remote_output_dir,
            "--recorded-local-output-dir", str(local_output_dir),
            "--manifest", tmp,
        ]
        arg_text = " ".join(self._sh_quote(value) for value in args)
        begin = "__REMOTE_SUMMARY_CSV_BEGIN__"
        end = "__REMOTE_SUMMARY_CSV_END__"
        command = (
            f"{self.config.saleae_capture_script.rsplit('/', 1)[0]}/python summarize_capture.py {arg_text}; "
            f"rc=$?; echo {begin}; cat {self._sh_quote(tmp)} 2>/dev/null; echo {end}; "
            f"rm -f {self._sh_quote(tmp)}; exit $rc"
        )
        proc = self._run_saleae(command, timeout_s=30)
        output = proc.stdout or ""
        if proc.returncode != 0 or begin not in output or end not in output:
            raise RuntimeError(output or "remote capture summary failed")
        csv_text = output.split(begin, 1)[1].split(end, 1)[0].strip()
        rows = list(csv.DictReader(io.StringIO(csv_text)))
        if not rows:
            raise RuntimeError(f"remote capture summary returned no CSV row: {output}")
        return rows[-1]

    def _summarize_capture(
        self,
        index: int,
        stage: str,
        kind: str,
        packet: int,
        rails: RailVoltages,
        remote_output_dir: str,
        local_output_dir: Path,
    ) -> dict[str, str]:
        tmp = self.config.run_dir / f"manifest_tmp_{index}_{kind}.csv"
        tmp.write_text(
            "index,phase,vcc_set_V,vcc_wl_set_V,packet,bits_lsb_first,remote_output_dir,local_output_dir,"
            "ok,decoded_packet,la_set_window_mean_uA,la_reset_window_mean_uA,adc_read_uA,adc_set_uA,adc_reset_uA,error\n"
        )
        proc = self.runner.run(
            [
                sys.executable,
                str(self.config.summarizer),
                "--index",
                str(index),
                "--phase",
                stage,
                "--packet",
                f"0x{packet:04x}",
                "--bits",
                bits_lsb(packet),
                "--vcc-set-v",
                str(rails.vcc_set_v),
                "--vcc-wl-set-v",
                str(rails.vcc_wl_set_v),
                "--remote-output-dir",
                remote_output_dir,
                "--local-output-dir",
                str(local_output_dir),
                "--manifest",
                str(tmp),
            ]
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stdout)
        with tmp.open(newline="") as handle:
            return list(csv.DictReader(handle))[-1]

    def _ensure_manifest(self) -> None:
        if self.manifest.exists():
            return
        with self.manifest.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS)
            writer.writeheader()

    def _append_manifest(
        self,
        index: int,
        stage: str,
        kind: str,
        result: CellOperationResult,
        bitstream: str,
        bits: str,
    ) -> None:
        with self.manifest.open("a", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS)
            writer.writerow(
                {
                    "index": index,
                    "stage": stage,
                    "kind": kind,
                    "cell": result.cell.label,
                    "operation": result.operation,
                    "packet": result.packet,
                    "vcc_set_V": result.rails.vcc_set_v,
                    "vcc_wl_set_V": result.rails.vcc_wl_set_v,
                    "bits_lsb_first": bits,
                    "bitstream": bitstream,
                    "ok": result.ok,
                    "decoded_packet": result.decoded_packet,
                    "la_set_window_mean_uA": result.current_uA,
                    "local_output_dir": result.local_output_dir,
                    "error": result.error,
                }
            )

    def _next_index(self) -> int:
        with self.manifest.open(newline="") as handle:
            return sum(1 for _ in csv.DictReader(handle))

    def _append_jsonl(self, filename: str, item: dict[str, object]) -> None:
        with (self.config.run_dir / filename).open("a") as handle:
            handle.write(json.dumps(item, sort_keys=True) + "\n")

    def _append_progress(self, operation: str, message: str, **extra: object) -> None:
        item = {
            "operation": operation,
            "message": message,
            "time": time.time(),
            **extra,
        }
        self._append_jsonl("progress.jsonl", item)
        print(json.dumps({"progress": item}, sort_keys=True), flush=True)

    def _remote_file_exists(self, filename: str) -> bool:
        if self.config.zynq_os == "windows":
            proc = self._run_zynq_powershell(f"if (Test-Path '{filename}') {{ exit 0 }} else {{ exit 1 }}", timeout_s=60)
        else:
            proc = self._run_zynq(f"test -f {self._sh_quote(filename)}", timeout_s=60)
        return proc.returncode == 0

    def _write_remote_text(self, filename: str, text: str) -> None:
        encoded = base64.b64encode(text.encode()).decode()
        if self.config.zynq_os == "windows":
            cmd = (
                f"$b='{encoded}'; "
                f"[IO.File]::WriteAllText('{filename}', [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b)))"
            )
            proc = self._run_zynq_powershell(cmd, timeout_s=60)
        else:
            proc = self._run_zynq(
                f"python3 - <<'PY'\n"
                f"import base64, pathlib\n"
                f"pathlib.Path({filename!r}).write_bytes(base64.b64decode({encoded!r}))\n"
                f"PY",
                timeout_s=60,
            )
        if proc.returncode != 0:
            raise RuntimeError(proc.stdout)

    def _write_remote_binary(self, filename: str, data: bytes) -> None:
        scp_error = ""
        if self.config.zynq_host and not self.config.zynq_password:
            scp = shutil.which("scp")
            if scp:
                upload_path = self.config.run_dir / f".{filename}.upload"
                upload_path.write_bytes(data)
                try:
                    target = f"{self.config.zynq_host}:{self.config.zynq_dir.rstrip('/')}/{filename}"
                    proc = self.runner.run([scp, str(upload_path), target], timeout_s=180)
                    if proc.returncode == 0:
                        return
                    scp_error = proc.stdout
                finally:
                    upload_path.unlink(missing_ok=True)
            if len(data) > 100_000:
                raise RuntimeError(f"scp upload failed for {filename}: {scp_error or 'scp not found'}")
        elif self.config.zynq_host and len(data) > 100_000:
            self._write_remote_binary_chunked(filename, data)
            return

        encoded = base64.b64encode(data).decode()
        # Encoded PowerShell commands hit Windows' command-line limit well
        # before a source file is large enough for the old 100 kB fallback.
        if self.config.zynq_host and self.config.zynq_os == "windows" and len(encoded) > 20_000:
            self._write_remote_binary_chunked(filename, data)
            return
        if self.config.zynq_os == "windows":
            cmd = f"$b='{encoded}'; [IO.File]::WriteAllBytes('{filename}', [Convert]::FromBase64String($b))"
            proc = self._run_zynq_powershell(cmd, timeout_s=180)
        else:
            proc = self._run_zynq(
                f"python3 - <<'PY'\n"
                f"import base64, pathlib\n"
                f"pathlib.Path({filename!r}).write_bytes(base64.b64decode({encoded!r}))\n"
                f"PY",
                timeout_s=180,
            )
        if proc.returncode != 0:
            raise RuntimeError(proc.stdout)

    def _read_remote_binary(self, filename: str) -> bytes:
        if not self.config.zynq_host:
            return (Path(self.config.zynq_dir) / filename).read_bytes()

        if not self.config.zynq_password:
            scp = shutil.which("scp")
            if scp:
                download_path = self.config.run_dir / f".{filename}.download"
                source = f"{self.config.zynq_host}:{self.config.zynq_dir.rstrip('/')}/{filename}"
                try:
                    proc = self.runner.run([scp, source, str(download_path)], timeout_s=180)
                    if proc.returncode == 0:
                        return download_path.read_bytes()
                    scp_error = proc.stdout
                finally:
                    download_path.unlink(missing_ok=True)
                raise RuntimeError(f"scp download failed for {filename}: {scp_error}")

        if self.config.zynq_os == "windows":
            proc = self._run_zynq_powershell(
                f"Write-Output ('B64:' + [Convert]::ToBase64String([IO.File]::ReadAllBytes('{filename}')))",
                timeout_s=180,
            )
        else:
            proc = self._run_zynq(
                f"base64 {self._sh_quote(filename)} | tr -d '\\n' | sed 's/^/B64:/'",
                timeout_s=180,
            )
        if proc.returncode != 0:
            raise RuntimeError(proc.stdout or f"could not download {filename}")
        marker = "B64:"
        start = proc.stdout.find(marker)
        if start < 0:
            raise RuntimeError(f"could not find binary payload for {filename}")
        return base64.b64decode(proc.stdout[start + len(marker):].strip())

    def _write_remote_binary_chunked(self, filename: str, data: bytes) -> None:
        encoded = base64.b64encode(data).decode()
        chunk_chars = 48_000
        b64_name = f"{filename}.b64tmp"
        if self.config.zynq_os == "windows":
            proc = self._run_zynq_powershell(
                f"[IO.File]::WriteAllText('{b64_name}', '', [Text.Encoding]::ASCII)",
                timeout_s=60,
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stdout)
            for offset in range(0, len(encoded), chunk_chars):
                chunk = encoded[offset:offset + chunk_chars]
                proc = self._run_zynq_powershell(
                    f"[IO.File]::AppendAllText('{b64_name}', '{chunk}', [Text.Encoding]::ASCII)",
                    timeout_s=60,
                )
                if proc.returncode != 0:
                    raise RuntimeError(proc.stdout)
            proc = self._run_zynq_powershell(
                f"[IO.File]::WriteAllBytes('{filename}', [Convert]::FromBase64String([IO.File]::ReadAllText('{b64_name}'))); "
                f"Remove-Item -Force '{b64_name}'",
                timeout_s=180,
            )
        else:
            proc = self._run_zynq(f": > {self._sh_quote(b64_name)}", timeout_s=60)
            if proc.returncode != 0:
                raise RuntimeError(proc.stdout)
            for offset in range(0, len(encoded), chunk_chars):
                chunk = encoded[offset:offset + chunk_chars]
                proc = self._run_zynq(f"printf %s {self._sh_quote(chunk)} >> {self._sh_quote(b64_name)}", timeout_s=60)
                if proc.returncode != 0:
                    raise RuntimeError(proc.stdout)
            proc = self._run_zynq(
                f"base64 -d {self._sh_quote(b64_name)} > {self._sh_quote(filename)} && rm -f {self._sh_quote(b64_name)}",
                timeout_s=180,
            )
        if proc.returncode != 0:
            raise RuntimeError(proc.stdout)

    def _copy_remote_binary_to_local(self, filename: str, local_path: Path) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.config.zynq_host:
            source = Path(self.config.zynq_dir) / filename
            shutil.copy2(source, local_path)
            return
        if self.config.zynq_os == "windows":
            proc = self._run_zynq_powershell(
                f"Write-Output '__BITSTREAM_B64_BEGIN__'; "
                f"[Convert]::ToBase64String([IO.File]::ReadAllBytes('{filename}')); "
                f"Write-Output '__BITSTREAM_B64_END__'",
                timeout_s=180,
            )
        else:
            proc = self._run_zynq(
                f"echo __BITSTREAM_B64_BEGIN__; base64 {self._sh_quote(filename)}; echo __BITSTREAM_B64_END__",
                timeout_s=180,
            )
        if proc.returncode != 0:
            raise RuntimeError(proc.stdout)
        match = re.search(r"__BITSTREAM_B64_BEGIN__\s*(.*?)\s*__BITSTREAM_B64_END__", proc.stdout, re.S)
        if not match:
            raise RuntimeError(f"could not find bitstream payload in remote output for {filename}")
        payload = re.sub(r"[^A-Za-z0-9+/=]", "", match.group(1))
        payload += "=" * (-len(payload) % 4)
        local_path.write_bytes(base64.b64decode(payload))

    def _remove_remote_file(self, filename: str) -> None:
        if self.config.zynq_os == "windows":
            self._run_zynq_powershell(f"if (Test-Path '{filename}') {{ Remove-Item -Force '{filename}' }}", timeout_s=60)
        else:
            self._run_zynq(f"rm -f {self._sh_quote(filename)}", timeout_s=60)

    def _run_zynq_powershell(self, command: str, timeout_s: int | None = None) -> subprocess.CompletedProcess[str]:
        encoded = base64.b64encode(command.encode("utf-16le")).decode()
        return self._run_zynq(f"powershell -NoProfile -EncodedCommand {encoded}", timeout_s=timeout_s)

    def _run_zynq_cmd(self, command: str, timeout_s: int | None = None) -> subprocess.CompletedProcess[str]:
        """Run native CMD syntax even when OpenSSH's configured shell is PowerShell."""

        windows_dir = self.config.zynq_dir.replace("/", "\\")
        if self.config.zynq_host:
            if self.config.zynq_password:
                full_command = f'cmd /D /S /C "cd /D {windows_dir} && {command}"'
                return self.runner.ssh_with_expect_password(
                    self.config.zynq_host,
                    self.config.zynq_password,
                    full_command,
                    timeout_s=timeout_s,
                )
            # Pass CMD and its arguments separately. Sending the entire command
            # as one OpenSSH argument can leave cmd.exe in interactive mode.
            return self.runner.run(
                [
                    "ssh",
                    "-o",
                    "ConnectTimeout=15",
                    self.config.zynq_host,
                    "cmd",
                    "/D",
                    "/S",
                    "/C",
                    f'"cd /D {windows_dir} && {command} & exit"',
                ],
                timeout_s=timeout_s,
            )
        full_command = f'cmd /D /S /C "cd /D {windows_dir} && {command}"'
        return self.runner.run(self._local_shell_command(full_command), timeout_s=timeout_s)

    def _run_zynq(self, command: str, timeout_s: int | None = None) -> subprocess.CompletedProcess[str]:
        full_command = f"cd {self.config.zynq_dir} && {command}"
        if self.config.zynq_host:
            if self.config.zynq_password:
                return self.runner.ssh_with_expect_password(
                    self.config.zynq_host,
                    self.config.zynq_password,
                    full_command,
                    timeout_s=timeout_s,
                )
            return self.runner.ssh(self.config.zynq_host, full_command, timeout_s=timeout_s)
        return self.runner.run(self._local_shell_command(full_command), timeout_s=timeout_s)

    def _popen_saleae(self, command: str) -> subprocess.Popen[str]:
        full_command = f"cd {self.config.saleae_dir} && {command}"
        if self.config.saleae_host:
            return subprocess.Popen(
                ["ssh", "-o", "ConnectTimeout=15", self.config.saleae_host, full_command],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        return subprocess.Popen(self._local_shell_command(full_command), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    @staticmethod
    def _local_shell_command(command: str) -> list[str]:
        if platform.system().lower().startswith("win"):
            return ["powershell", "-NoProfile", "-Command", command]
        return ["bash", "-lc", command]

    @staticmethod
    def _passes(value: float, threshold: float, direction: Literal["above", "below"]) -> bool:
        return value > threshold if direction == "above" else value < threshold

    @staticmethod
    def _is_better(candidate: CellOperationResult, current: CellOperationResult, direction: Literal["above", "below"]) -> bool:
        if candidate.current_uA is None:
            return False
        if current.current_uA is None:
            return True
        return candidate.current_uA > current.current_uA if direction == "above" else candidate.current_uA < current.current_uA

    @staticmethod
    def _float_or_none(value: object) -> float | None:
        try:
            out = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return out if math.isfinite(out) else None

    @staticmethod
    def _sh_quote(value: object) -> str:
        text = str(value)
        return "'" + text.replace("'", "'\"'\"'") + "'"
