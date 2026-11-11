# Full-array WB-PWM forming experiment

Code and firmware snapshot for the chip-4 full-array forming campaign prepared on 2026-11-11.

This directory intentionally contains **reproducibility assets only**. Measurement data, Vivado logs, console evidence, checkpoints, manifests, result CSV/XLSX files, and hardware-run directories are excluded.

## Experiment order

1. Run a read-only 32 × 32 prescreen at 0.5 V read bias.
2. Select every cell with fresh baseline conductance below 20 µS.
3. Preassign eligible cells within baseline-conductance quartile × spatial-quadrant strata to `set_only`, `wl_only`, or `coupled`.
4. On every eligible cell, exhaust the WB-PWM SET ladder `0x10, 0x20, …, 0xF0` at `VCC_SET = VCC_WL = 1.65 V`.
5. Only cells still below 20 µS enter their preassigned voltage-split ladder at PWM `0x20`:
   - `set_only`: VCC_SET = 1.85, 2.00, 2.15, 2.30, 2.50, 2.70, 3.00 V; VCC_WL = 1.65 V.
   - `wl_only`: VCC_SET = 1.65 V; VCC_WL = 1.70, 1.80, 1.90, 2.00 V.
   - `coupled`: (1.85, 1.70), (2.00, 1.80), (2.20, 1.90), (2.50, 2.00), (2.70, 2.00), (3.00, 2.00) V.

The 3.00 V ceiling applies only to VCC_SET during WB-PWM. VCC_WL is capped at 2.00 V by the experiment plan. Scan-debug forming is disabled for this campaign.

## Evidence and safety gates

Every PWM attempt retains:

- five immediate target pre-reads;
- five target post-reads in one FPGA bulk-capture transaction;
- four rotating sentinel reads before and after programming;
- WB acknowledgment and packed TDC validation;
- raw-frame CRC, address, epoch, clipping, and completeness checks;
- 10 µA maximum sentinel drift;
- verified restoration of the 0.5 V scan-read profile;
- a robust-forming criterion of post-read median ≥ 20 µS;
- a separate threshold-transient classification when only the post maximum reaches 20 µS.

The host stops a cell after robust formation, a threshold transient, or a safety/evidence failure. It never labels a safety-gated cell as successfully formed.

## Speed path

The prescreen supports up to ten contiguous columns per Vivado session; the host defaults to four. This amortizes JTAG startup while preserving one validated result per cell. PWM pre/post evidence already uses the FPGA bulk transport. The optimized PWM path removes duplicate CPU-hold transactions but does not remove reads or gates.

## Directory contents

- `array_4bit_live/`: experiment orchestration, parsing, durable checkpoints, and focused tests.
- `controller/runtime/`: Vivado/Tcl hardware controller and board wrappers.
- `controller/rtl/`: FPGA controller, bulk pager, ADC capture, DAC, and I²C sources.
- `controller/constraints/`: ZedBoard constraints.
- `controller/ip/`: Vivado IP configuration files.
- `controller/build/`: exact FPGA bitstream and probes file used by this snapshot.
- `controller/tests/`: bulk-transport and controller simulation sources.

## Environment

- Windows with Python 3.12 or compatible.
- Xilinx Vivado 2022.2.
- ZedBoard and the configured remote `hw_server` connection.
- The repository is expected to be checked out locally; no data files are required to inspect or test the plan.

Point the host at this controller snapshot before running:

```powershell
$env:SENSEEVAL_CONTROLLER_ROOT = (Resolve-Path .\controller).Path
$env:SENSEEVAL_VIVADO = 'C:\Xilinx\Vivado\2022.2\bin\vivado.bat'
```

## Validation

Run the focused non-hardware tests from this directory:

```powershell
python -m unittest array_4bit_live.tests.test_pwm_first_population -v
python -m unittest array_4bit_live.tests.test_bulk_controller -v
```

Before programming, the queued workflow performs a read-only maximum-profile check at VCC_SET = 3.00 V and VCC_WL = 2.00 V. It requires zero programming packets, five valid restoration reads, and an exact completion marker.

## Typical workflow

```powershell
python .\array_4bit_live\run_resumable_full_array_prescreen.py `
  --output-dir C:\experiment_data\prescreen `
  --forming-threshold-us 20 `
  --columns-per-job 4

python .\array_4bit_live\queue_pwm_after_prescreen.py `
  --prescreen-dir C:\experiment_data\prescreen `
  --output-dir C:\experiment_data\wb_pwm_population `
  --profile-check-dir C:\experiment_data\wb_pwm_profile_check `
  --timeout-seconds 14400
```

Keep all output paths outside this source directory.

## Released firmware hashes

SHA-256:

```text
5926F1F72710F043B9235044290645B33FA57135EBDFB533DCF6C3FD1F7DEA31  controller/build/zedboard_adc_capture.bit
985BEE207225D41BD83645E105E6C467F4C987945737CF9597933BB26AE70336  controller/build/zedboard_adc_capture.ltx
CB95ED449ECC8C9D1B63A60D3530EA33D145E5762020BA1202C8861AC8ACE4F0  controller/runtime/control.tcl
```

The bitstream/probes pair predates the host-only independent-rail and speed-path Tcl changes; those changes use the existing protected DAC and bulk-transport FPGA interfaces and do not require a new bitstream.
