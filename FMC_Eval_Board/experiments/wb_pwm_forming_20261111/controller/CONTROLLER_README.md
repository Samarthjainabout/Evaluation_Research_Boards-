# ZedBoard compatibility for CaravelReRAM-SenseEval v1.2F

Audit date: 2026-10-31

Status: **the complete ZedBoard RTL/XDC port builds and routes for
`xc7z020clg484-1`. Electrical use remains conditional on three physical facts
that Vivado cannot measure: J18/VADJ must be 1.8 V, the documented 3.3 V I2C
rework must be present, and seven receiver-end 100-ohm LVDS terminations must
be present. An original schematic-populated v1.2F board is not directly
compatible on management I2C.**

The ZedBoard is a substantially better electrical match than the AXE5-Eagle.
All daughtercard signals carried on FMC `LA00` through `LA16`, plus
`CLK0_M2C`, reach Zynq-7000 PL bank 34. ZedBoard bank 34 is powered by VADJ and
can be set to 1.8 V with jumper J18. That matches the daughtercard's LVCMOS18
single-ended interface.

Two assembly details still require attention:

1. FMC management I2C pins C30/C31 reach Zynq bank 13, which is fixed at
   3.3 V. In the original schematic population, the daughtercard pulls those
   pins to 1.8 V with R54/R53 and presents them to the 1.8 V side of U18. That
   population is incompatible. However, the repository records that the active
   bench assembly has R53, R54, and U18 removed, with R53-2 bridged to R58-2
   and R54-2 bridged to R57-2, making the FMC management bus 3.3 V. If that
   exact rework is physically present, ZedBoard bank 13 is compatible. If it is
   absent, add an external/interposer I2C translator before use.
2. `LCLK`, `ADCLK`, and `OUT1` through `OUT5` are LVDS inputs to the FPGA.
   ZedBoard banks 34/35 are 7-series HR banks, so the correct receive standard
   is `LVDS_25`. AMD permits an LVDS_25 input with bank VCCO at 1.8 V only when
   `DIFF_TERM=FALSE` and the input limits are met. Therefore add an external
   100 ohm differential termination across each of the seven receive pairs,
   placed at the ZedBoard receiver/interposer end. The current KC705 files
   already require these external terminations and set `DIFF_TERM=FALSE`.
   Confirm whether the bench resistors are on the removable FMC assembly; any
   resistors fitted only to the KC705 carrier will not transfer to ZedBoard.

## Mandatory physical preflight

- Power the ZedBoard off and remove the FMC card before moving J18.
- Put J18 in the board's **1V8** position. The historical ZedBoard guide is
  internally inconsistent about the shipped default, so never rely on the
  current jumper position.
- With no FMC card attached, power the ZedBoard and measure VADJ at J18 or FMC
  H40. It must be approximately 1.8 V. Power off again before attaching the
  card.
- Confirm no 3.3 V modification has been fitted to the unpopulated J18 pads.
- Visually verify the documented R53/R54/U18 management-I2C rework. If it is
  not present, install an I2C translator before programming the full system.
- Verify that all seven receiver-end 100 ohm LVDS terminations used by the
  current KC705 setup transfer with the removable assembly; otherwise install
  them at the ZedBoard/interposer receiver end.
- Start all FPGA outputs in a safe state. In particular, keep the translated
  controls disabled until configuration and reset sequencing are established.

## ZedBoard FMC details used for this decision

- Device: XC7Z020-1CSG484 Zynq-7000.
- Connector: one LPC FMC, 34 LA differential pairs / 68 single-ended LA pins.
- LA00-LA16: PL bank 34.
- LA17-LA33: PL bank 35.
- Both banks use jumper-selectable VADJ: 1.8 V or 2.5 V normally; the 3.3 V
  option requires a board modification and must not be used here.
- Used dedicated clock: `CLK0_M2C_P/N` on bank 34 pins L18/L19.
- Management I2C: C30/C31 on fixed-3.3 V bank 13 pins R7/U7. This matches only
  the documented reworked 3.3 V daughtercard assembly. FPGA I2C outputs must
  use open-drain behavior (drive low or high-impedance), never push-pull high.
- PL reference clock: single-ended 100 MHz oscillator on bank 13 pin Y9. The
  ZCU106 differential system-clock wrapper and PLL settings must be replaced.
- The guide identifies VADJ capacity as 2 A, FMC 12 V capacity as 1 A, and the
  board input as 12 V. Actual daughtercard current must still be checked during
  staged bring-up.

## Implementation consequence

The existing Xilinx HDL is portable in principle and the 7-series branches of
the clock, ODDR, IDDR, VIO, and ILA logic can be used. A new ZedBoard XDC,
100 MHz single-ended clock wrapper, and Zynq-7000 Vivado build are required.
The ZCU106 bitstream and package pins are not reusable.

See `fmc_mapping.csv` for the complete used-signal mapping,
`COMPATIBILITY_AUDIT.md` for electrical evidence and go/no-go conditions, and
`BUILD_VALIDATION.md` for the completed implementation results.

## Completed implementation

The new folder is self-contained:

- `rtl/`: ZedBoard top level plus copied control, scan, I2C, UART, DAC and ADC
  capture RTL.
- `constraints/`: exact ZedBoard FMC/package mapping and CDC bus-skew
  constraint.
- `runtime/`: Hardware Manager I2C, ADC, scan and initialization scripts.
- `app/`: the host application and firmware sources copied with the port.
- `build_zedboard.tcl`: reproducible Vivado 2022.2 project, synthesis,
  implementation, signoff reports and bitstream generation.
- `program_zedboard_safe.tcl`: programmer with an exact `xc7z020` target guard
  and two explicit physical-preflight interlocks.
- `build/zedboard_adc_capture.bit`: generated PL bitstream.
- `build/zedboard_adc_capture.ltx`: matching ILA/VIO probes.
- `build/zedboard_adc_capture.dcp`: routed checkpoint.
- `ngspice/`: runnable voltage-mismatch, back-power, I2C-population and
  contention screens with recorded ngspice 47 results.

The port replaces the ZCU106 differential system clock with ZedBoard Y9
100 MHz LVCMOS33, regenerates 50 MHz control, 10 MHz ADC and 200 MHz monitor
clocks with a 7-series PLL, uses 7-series ODDR/IDDR/BUFIO/BUFR resources, and
scales all cycle-counted 200 MHz control delays to preserve their real-time
durations at 50 MHz. The debug IP is regenerated by the installed Vivado
release rather than relying on newer-schema XCI snapshots.

## WB shunt observation

Normal Wishbone operations now retain an independent ADC observation without
changing the command echo or any of the 15 cached TDC words. The schematic and
tested-board routing are:

| Operation | DAC rail | Shunt sense nets | ADC path |
| --- | --- | --- | --- |
| READ | DAC0 / VCC_READ | R9 / AIN1_P,N | ADC1 |
| RESET | DAC5 / VCC_RESET | R11 / AIN3_P,N | ADC2 |
| SET | DAC2 / VCC_SET | R10 / AIN2_P,N | ADC3 |

For each WB command, the controller takes 4096 ADC samples after the firmware
boot marker and immediately before the packet. It then records the selected
shunt's extrema, clipping and maximum consecutive positive/negative excursions
from the full baseline envelope through the final packet bit and TDC return.
`runtime/control.tcl` emits these fields as `WB_SHUNT`; the existing nonzero
`WB_TDC_RETURN_GATE` remains mandatory and scan-debug reads remain the cell-state
arbiter. Coarse PWM `0x10` is approximately one 10-MHz clock and may be missed
by the 10-MSPS ADC depending on phase, so an absent `0x10` observation is marked
sampling-limited rather than interpreted as proof of no pulse.

The PDF schematic labels R9/R10/R11 as 470 ohms. The tested assembly is recorded
as reworked to 56 ohms on all three shunts, so logs preserve raw ADC codes as
the primary evidence and label current conversion as nominal 56-ohm, gain-7.

Build from this directory with:

```powershell
& 'C:\Xilinx\Vivado\2022.2\bin\vivado.bat' -mode batch -source build_zedboard.tcl
```

After the physical checklist is complete and an actual ZedBoard is on JTAG,
program with:

```powershell
$env:ZEDBOARD_VADJ_MEASURED_1V8='YES'
$env:DAUGHTERCARD_I2C_REWORK_VERIFIED='YES'
& 'C:\Xilinx\Vivado\2022.2\bin\vivado.bat' -mode batch -source program_zedboard_safe.tcl
```

Do not set those variables without measuring/inspecting the hardware. The
programmer refuses any target other than one `xc7z020`.

