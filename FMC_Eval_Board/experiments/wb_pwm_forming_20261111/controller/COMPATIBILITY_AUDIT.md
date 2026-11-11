# ZedBoard FMC compatibility audit

## Decision

**Implementation pass; conditional hardware go.** The main LA bus does not need
wholesale level shifting when ZedBoard J18 is set and measured at 1.8 V. The
repository's documented active bench assembly is also reworked to a 3.3 V
management-I2C bus, which matches ZedBoard bank 13. Before power-up, physically
confirm that rework and confirm the seven external 100 ohm LVDS receiver
terminations used by the current KC705 setup. An untouched
schematic-populated v1.2F board still needs I2C translation.

## Evidence

### Carrier

- The official ZedBoard hardware guide states that its LPC FMC exposes 68
  single-ended I/Os / 34 differential LA pairs, split between banks 34 and 35.
- J18 selects VADJ for those banks. The expansion section calls 1.8 V the
  default, while the power and bank tables call 2.5 V the default. This
  contradiction is why an actual jumper inspection and voltage measurement is
  mandatory.
- The official Digilent/Avnet master XDC assigns LA00-LA16 and CLK0_M2C to bank
  34, LA17-LA33 and CLK1_M2C to bank 35, and sets both banks to LVCMOS18 in its
  default constraint example.
- The master XDC assigns FMC SCL/SDA to R7/U7 in fixed-3.3 V bank 13.

### Daughtercard

- The checked-in KiCad schematic and PCB use LVCMOS18 for the single-ended
  FPGA-facing controls and returns on LA01 and LA07-LA16.
- ADC `LCLK`, `ADCLK`, and `OUT1`-`OUT5` are LVDS outputs from the daughtercard
  and inputs at the FPGA.
- In the original design, R53 and R54 are 10 kohm pull-ups from FMC management
  SDA/SCL to +1V8, on the low-voltage side of U18 PCA9306.
- The top-level package README records the active assembly rework: R53, R54,
  and U18 removed, R53-2 bridged to R58-2, and R54-2 bridged to R57-2. That
  makes C30/C31 a 3.3 V management bus and is compatible with ZedBoard bank 13.
- The daughtercard does not use FMC VADJ as its local 1.8 V source, so changing
  J18 controls the FPGA bank voltage rather than repowering all daughtercard
  components.

## Interface-by-interface result

| Interface | Result | Required action |
|---|---|---|
| LA01 and LA07-LA16 single-ended | Compatible | J18=1.8 V; use LVCMOS18 |
| LA00, LA02-LA06, CLK0_M2C differential inputs | Conditional | LVDS_25, DIFF_TERM=FALSE, add 100 ohm across all 7 pairs |
| FMC SCL/SDA C30/C31, original population | Incompatible | Add open-drain level translator or apply the documented rework |
| FMC SCL/SDA C30/C31, documented active rework | Compatible | Verify R53/R54/U18 removal and both bridges physically |
| FMC mechanical LPC connector | Compatible | Observe power-off insertion/removal |
| FPGA main clock | Different but solvable | Replace ZCU106 differential clock with ZedBoard Y9 100 MHz LVCMOS33 input |
| Existing ZCU106 bitstream/XDC | Not portable | Rebuild for xc7z020clg484-1 and the new pin map |

## Why the I2C exception matters

The XC7Z020 LVCMOS33 minimum guaranteed high input voltage is 2.0 V. An
unmodified daughter's 1.8 V pull-up therefore cannot guarantee a high at
ZedBoard bank 13. Pulling that original node to 3.3 V is not a fix because it
is connected to the daughtercard's 1.8 V PCA9306 side. Either retain the
documented bench rework that bypasses/removes that 1.8 V side, or separate the
domains with a proper open-drain level translator.

## Why external LVDS termination is required

ZedBoard bank 34 is an HR bank. AMD specifies LVDS_25 for HR banks. Differential
inputs may be used with VCCO other than 2.5 V only when internal differential
termination is disabled and the input voltage limits are satisfied. The
receiver still needs a 100 ohm differential load, so it must be supplied
externally at the receiving end. Setting VADJ to 2.5 V just to enable internal
termination is not acceptable because the many single-ended paths require
1.8 V.

## Go/no-go checklist

Proceed to implementation only after all are true:

- [ ] J18 is visibly in the 1V8 position.
- [ ] Measured VADJ with the FMC card removed is within the 1.8 V rail tolerance.
- [ ] No 3.3 V J18 modification is populated.
- [ ] C30/C31 have the documented R53/R54/U18 removal and bridges, or are
      isolated and translated between 3.3 V and 1.8 V.
- [ ] Seven 100 ohm differential terminations are confirmed at the ZedBoard or
      removable-interposer receiver end; KC705-only modifications do not count.
- [ ] The new XDC uses LVCMOS18 for all single-ended bank-34 paths.
- [ ] The new XDC uses LVDS_25 and explicitly disables DIFF_TERM for all seven
      differential receive pairs.
- [ ] The design uses Y9 as a 100 MHz LVCMOS33 system clock and has regenerated
      PLL/MMCM parameters.
- [x] Vivado DRC, implementation timing, bus-skew timing, and an
      outputs-disabled bitstream pass.
- [ ] First power-up uses current limiting and verifies all rails before enabling
      ADC/DAC/scan activity.

## Implementation evidence

- Target part: `xc7z020clg484-1`, Vivado 2022.2.
- All 38 FMC mapping rows match the routed port report exactly; there are no
  missing rows, pin mismatches, bank mismatches, or I/O-standard mismatches.
- All 36 LA/CLK pins are in VADJ bank 34; management I2C is on fixed-3.3 V bank
  13 at R7/U7.
- Final routed timing: WNS +0.057 ns, TNS 0; WHS +0.024 ns, THS 0.
- The 60-bit ADC snapshot crossing has 1.151 ns actual skew against a 2.000 ns
  constraint, for +0.849 ns slack.
- Route status: zero routing errors and no unrouted nets.
- DRC: zero errors and zero critical warnings. Eight non-blocking warnings are
  retained in `build/drc.rpt`: DSP pipelining suggestions, generated debug-hub
  warnings, a no-routable-load debug warning, and the expected pure-PL Zynq
  PS7 warning.
- ADC/DAC run VIO bits and the 128-bit I2C command VIO are asserted by the build
  script to initialize to zero. The generated image therefore does not start
  ADC/DAC traffic at configuration.
- External ADC data input/output delays are not claimed because the existing
  source project and daughtercard material do not provide a characterized
  post-route board timing budget. Capture uses the returned LCLK with
  BUFIO/BUFR and IDDR; functional margin must be measured during staged
  hardware bring-up.
- `report_cdc` includes critical classifications from intentionally probing
  the LCLK domain with a sysclk ILA, the deliberate held-bus/toggle snapshot
  crossing, and generated debug-hub logic. The functional snapshot bus has a
  three-stage toggle synchronizer and the explicit passing bus-skew bound above.
  These classifications are recorded in `build/cdc.rpt`; they are not presented
  as a clean CDC signoff.

The final JTAG scan on 2026-10-31 found `xczu7_0` (`part=xczu7`) and an ARM DAP,
which is the connected ZCU106, not a ZedBoard `xc7z020`. The ZedBoard
programmer was therefore intentionally not run. This is a target-identity
blocker, not a Vivado installation or bitstream-generation failure.

## Primary sources

- ZedBoard hardware guide:
  https://digilent.com/reference/_media/zedboard/zedboard_ug.pdf
- ZedBoard Rev E.1 schematic:
  https://digilent.com/reference/_media/reference/programmable-logic/zedboard/zedboard-schematic-rev-e1-public.pdf
- Digilent/Avnet ZedBoard master XDC:
  https://github.com/Digilent/digilent-xdc/blob/master/Zedboard-Master.xdc
- AMD 7-series SelectIO guide UG471:
  https://docs.amd.com/api/khub/documents/IbGcnPFe6eF19RHma_Y~IA/content
- AMD XC7Z020 electrical data DS187:
  https://docs.amd.com/api/khub/documents/uaBd8Qxmf_K1~~tiVnOxrw/content

