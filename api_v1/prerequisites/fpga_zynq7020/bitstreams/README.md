# Current FPGA Runtime Image

This directory intentionally contains one deployable FPGA image and its
matching Vivado debug-probes file. Cell addresses, scan/WB mode, packet count,
and scan voltages are runtime VIO inputs; they do not require separate
column-specific bitstreams.

| File | Purpose | SHA-256 |
|---|---|---|
| `caravel_scan_debug_runtime_dac81416_uart_wb_highz_v24.bit` | Validated shared scan-debug and WB runtime with the updated bias profile | `7698305b963262bb302904c0b78580eefc80b8ce5cd544c0da5223f092b58160` |
| `caravel_scan_debug_runtime_dac81416_uart_wb_highz_v24.ltx` | Matching VIO probes required by the API | `88c661873bdb70fa0b4d876c000e20e3ac79d7bbef4efda72ba3ebf0a70e325e` |

The v24 image uses the externally supplied 2 MHz clock. Scan reads hold the
selected cell for 2400 clocks (1.2 ms). In WB mode, TM and ScanInDR are
high-impedance; ScanInDL carries the checked startup command and then becomes
high-impedance before Wishbone access. Runtime WB commands preserve every DAC
register and do not change the PLL.

To rebuild:

```powershell
vivado -mode batch -source ..\build_runtime_bitstream.tcl
```

Normal API/GUI operations automatically upload and reuse this image.
