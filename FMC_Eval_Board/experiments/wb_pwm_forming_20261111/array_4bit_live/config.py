from __future__ import annotations

import os
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = APP_ROOT.parent
RUNS_ROOT = WORKSPACE_ROOT / "array_4bit_runs"

CONTROLLER_ROOT = Path(
    os.environ.get(
        "SENSEEVAL_CONTROLLER_ROOT",
        r"C:\Users\SER7\Documents\GitHub\Evaluation_Research_Boards-\FMC_Eval_Board\zedboard_cell_control",
    )
)
CONTROL_SCRIPT = CONTROLLER_ROOT / "runtime" / "control_zedboard_remote.tcl"
CONTROLLER_ARTIFACTS = {
    "control_wrapper_tcl": CONTROL_SCRIPT,
    "control_tcl": CONTROLLER_ROOT / "runtime" / "control.tcl",
    "i2c_host_tcl": CONTROLLER_ROOT / "runtime" / "i2c_host.tcl",
    "scan_ramp_tcl": CONTROLLER_ROOT / "runtime" / "scan_ramp.tcl",
    "fpga_bitstream": CONTROLLER_ROOT / "build" / "zedboard_adc_capture.bit",
    "fpga_probes_ltx": CONTROLLER_ROOT / "build" / "zedboard_adc_capture.ltx",
}
VIVADO = Path(
    os.environ.get(
        "SENSEEVAL_VIVADO",
        r"C:\Xilinx\Vivado\2022.2\bin\vivado.bat",
    )
)

REFERENCE_DIR = (
    WORKSPACE_ROOT
    / "pwm_scan_chip4_single_cell"
    / "run_20261106_r7c1"
)
REFERENCE_CSV = REFERENCE_DIR / "4bit_allcodes_cumulative_pwm_voltage_results.csv"
REFERENCE_REPORT = REFERENCE_DIR / "4bit_allcodes_cumulative_pwm_voltage_results.md"

CHIP_ID = 4
ROWS = 32
COLUMNS = 32
REFERENCE_CELL = (7, 1)
READ_VOLTAGE_V = 0.5
READ_HOLD_CLOCKS = 2400
PRE_READS = 5
POST_WRITE_READS = 5
SCAN_CLOCK_MHZ = 2
PROGRAM_CLOCK_MHZ = 10

TARGET_BANDS_US = tuple((53.0 + 4.0 * code, 57.0 + 4.0 * code) for code in range(16))
PWM_CODES = tuple(range(0x10, 0x100, 0x10))
SET_FIXED_VOLTAGE_V = 1.19
RESET_PWM = 0xF0
# The chip-4 r00/c00 2.5-V/0xF0 attempt caused a persistent sentinel
# disturbance.  2.3 V/0xF0 is the least-aggressive chip-4 condition with
# confirmed WB ACK/TDC/R11 evidence and sub-limit sentinel drift.
RESET_VOLTAGE_V = 2.3
RESET_SAFE_MAX_US = 2.0 * 34.2563054
SET_CEILING_US = 120.0
SET_PRE_PULSE_GUARD_US = 119.5
SENTINEL_LIMIT_UA = 10.0

# A cell whose five fresh scan-debug reads are all strictly below this
# conductance may receive a bounded sequence of conservative scan-debug SET
# forming pulses.  Every pulse is a separate hardware job with its own strict
# five-read guard, five post-reads, sentinels, and safe pause boundary.  A
# single hardware job can never contain more than one pulse.  Any verified
# target read at or above the threshold permanently disables scan SET for that
# cell.  Exhausting the per-cell budget below the threshold is a safety stop;
# it must never fall through to ordinary WB programming.
SCAN_FORM_THRESHOLD_US = 20.0
# Operator-authorized forming condition after the 1.20-V condition left the
# selected target below 20 uS.  Keep the strict five-read target guard, four
# sentinels, one pulse per job, and the 20-uS permanent scan-SET lockout.  The
# voltage is selected from the fabric-enforced op15 ladder; the host cannot
# request an arbitrary value.
SCAN_FORM_VCC_V = 1.7
SCAN_FORM_WL_V = 2.5
SCAN_FORM_HOLD_CLOCKS = 1200
SCAN_FORM_PULSES_PER_JOB = 1
SCAN_FORM_MAX_PULSES_PER_CELL = 8

# Exact continuation used only when the cumulative fixed-voltage PWM-code
# ladder has not yet landed the assigned state. The cell is not returned to a
# common baseline between codes or voltage points, and no stronger pulse
# follows a target landing. After the verified 1.36-V boundary, the
# user-authorized 2026-11-09 accelerated WB-only SET ramp skips the remaining
# unresponsive fine steps and advances by at most 100 mV per verified attempt,
# including the user-authorized 2.1-V and 2.2-V ceiling extension.
# It retains every pre-pulse,
# absolute-current, five-read, sentinel, clipping, and evidence gate.
SET_VOLTAGE_SKEW_V = (
    1.20, 1.21, 1.22, 1.23, 1.24, 1.25, 1.26, 1.27, 1.28, 1.29,
    1.30, 1.31, 1.32, 1.33, 1.34, 1.35, 1.36,
    1.425, 1.500, 1.600, 1.700, 1.800, 1.900, 2.000, 2.100, 2.200,
)

GLOBAL_HARDWARE_LOCK = APP_ROOT / "hardware.lock"
CONTROL_FILE = APP_ROOT / "control.json"
CHIP_SAFETY_FILE = APP_ROOT / "chip_safety.json"


def validate_environment() -> list[str]:
    problems: list[str] = []
    required = [("Vivado", VIVADO)] + [
        (f"controller artifact {label}", path)
        for label, path in CONTROLLER_ARTIFACTS.items()
    ]
    for label, path in required:
        if not path.is_file():
            problems.append(f"{label} not found: {path}")
    return problems

