# Current FPGA Runtime Image

This directory intentionally contains one deployable FPGA image and its
matching Vivado debug-probes file. Cell addresses, scan/WB mode, packet count,
and scan voltages are runtime VIO inputs; they do not require separate
column-specific bitstreams.

| File | Purpose | SHA-256 |
|---|---|---|
| `caravel_scan_debug_runtime_dac81416_uart_wb_highz_v28_dualclk_iref0p9.bit` | Hardware-validated shared scan-debug/WB runtime with FPGA-controlled Si5351, DAC7-first startup, and Iref=0.9 V | `95ffdbfa5315c35f7fc64895e2a13c041859eca39c1d72ea57233321d15dd99e` |
| `caravel_scan_debug_runtime_dac81416_uart_wb_highz_v28_dualclk_iref0p9.ltx` | Matching VIO probes required by the API | `42b0fd8ae624955695a5d2b337b1f10cadfcee7b14e5f1756a9e26e0f35fd065` |

The v28 image configures a fixed 900 MHz Si5351 PLL and selects a 2 MHz
multisynth output for scan-debug or a 10 MHz output for WB. Scan reads hold the
selected cell for 2400 clocks (1.2 ms). In WB mode, TM and ScanInDR are
high-impedance; ScanInDL carries the checked startup command and then becomes
high-impedance before Wishbone access. Runtime WB commands apply the dedicated
read-bias profile on DAC4/5 and DAC9-13. At startup DAC7/VDDIO is programmed
first, followed by a 10 ms delay and the remaining rails; flash Caravel
firmware only after this completes.

To rebuild:

```powershell
vivado -mode batch -source ..\build_runtime_bitstream.tcl
```

Normal API/GUI operations automatically upload and reuse this image.
