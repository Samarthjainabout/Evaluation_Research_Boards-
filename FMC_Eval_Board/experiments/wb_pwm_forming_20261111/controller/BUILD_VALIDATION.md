# ZedBoard build validation

Date: 2026-10-31  
Tool: Vivado 2022.2 build 3671981  
Target: `xc7z020clg484-1`  
Top: `zedboard_adc_test`

## Result

The complete port synthesized, placed, routed, passed the scripted timing and
DRC gates, and generated a bitstream. ADC and DAC enable controls initialize to
zero. This validates the FPGA implementation; it does not substitute for the
three physical electrical checks in `COMPATIBILITY_AUDIT.md`.

| Check | Result |
|---|---:|
| Mapping CSV rows | 38 |
| Mapping mismatches against routed ports | 0 |
| Setup WNS / TNS | +0.057 ns / 0 ns |
| Hold WHS / THS | +0.024 ns / 0 ns |
| Snapshot bus-skew slack | +0.849 ns |
| Routing errors / unrouted nets | 0 / 0 |
| DRC errors / critical warnings | 0 / 0 |

## Generated artifacts

| Artifact | Bytes | SHA-256 |
|---|---:|---|
| `build/zedboard_adc_capture.bit` | 4,045,677 | `48B97542D6A0B02B5A6121B58F24B381E5998EF32E97CF72304D1DC878C8E8F2` |
| `build/zedboard_adc_capture.ltx` | 245,803 | `FFD0D423D51194C8575D1514BDC9EFD628A0A00511CCFFFFB94A82AC7FAAE229` |
| `build/zedboard_adc_capture.dcp` | 9,187,514 | `7888CB72FBC1C15A0BA00C04CF8281CEE9900F031AD84A352BF5DF2C62CE3405` |

Supporting reports are in `build/`: `timing_summary.rpt`, `bus_skew.rpt`,
`drc.rpt`, `route_status.rpt`, `io.rpt`, `port_properties.tsv`, `cdc.rpt`,
`methodology.rpt`, `clock_utilization.rpt`, `utilization.rpt`, and
`check_timing.rpt`.

The earlier requested connection-failure simulations are in `ngspice/`.
Recorded cases confirm that the original 1.8 V I2C population misses the
ZedBoard 2.0 V minimum high threshold, a mixed 1.8/3.3 V pull-up workaround is
not valid, 2.5 V VADJ can inject clamp current, an unpowered bank can be
back-powered, and output-direction mistakes can create tens of milliamps of
contention. See `ngspice/SIMULATION_RESULTS.md` for values and model limits.

## Connected-target check

`detect_hardware.tcl` recorded:

```text
TARGET_COUNT=1
TARGET name=localhost:3121/xilinx_tcf/Xilinx/11672 frequency=15000000
DETECTED name=xczu7_0 part=xczu7
DETECTED name=arm_dap_1 part=arm_dap
```

This is not the target part for this bitstream. No programming attempt was
made. `program_zedboard_safe.tcl` additionally refuses to program unless it
sees exactly one `xc7z020` and the operator has recorded the VADJ measurement
and daughtercard-I2C inspection.

## Remaining physical validation

1. With the FMC card removed, set J18 to 1V8 and measure approximately 1.8 V
   at VADJ/H40.
2. Verify R53, R54 and U18 are removed and the documented R53-2/R58-2 and
   R54-2/R57-2 bridges are present, or add a proper open-drain translator.
3. Verify 100-ohm receiver termination across LCLK, ADCLK and OUT1 through
   OUT5 at the ZedBoard/interposer end.
4. Connect the actual ZedBoard; rerun `detect_hardware.tcl` and require
   `part=xc7z020` before using the safe programming script.
5. Use staged, current-limited bring-up and measure ADC source-synchronous
   timing/eye margin because the source project contains no characterized
   board-level input-delay budget.
