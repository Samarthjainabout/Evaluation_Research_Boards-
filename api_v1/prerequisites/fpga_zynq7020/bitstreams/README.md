# Current FPGA Runtime Image

This directory intentionally contains one deployable FPGA image and its
matching Vivado debug-probes file. Cell addresses, scan/WB mode, packet count,
and scan voltages are runtime VIO inputs; they do not require separate
column-specific bitstreams.

| File | Purpose | SHA-256 |
|---|---|---|
| `caravel_scan_debug_runtime_dac81416_uart_wb_highz_v35_wb_read_repair.bit` | Shared scan-debug/WB runtime with WB-only high impedance on TM/DR/DL, VIO on the stable 50 MHz PL clock, FPGA-controlled Si5351, DAC7-first startup, DAC13 dc_bias=1.5 V, and runtime bias skew | `db1a440159e1d99e85279cd3ee342e95aa59e7a787ecc3bb361f88540a9f58b0` |
| `caravel_scan_debug_runtime_dac81416_uart_wb_highz_v35_wb_read_repair.ltx` | Matching VIO probes required by the API | `45943f443fbe039b7790e6a5cc68af00f5bea5ee95ab6c67a87417eb1cc98809` |
| `../../caravel_wishbone/gui_wb_mode.hex` | Permanent Caravel firmware shared by scan-debug and runtime WB READ/SET/RESET | `0998ad922f6a982a113c43b696e03f73651c2cb9ea6e475a06f1b24ac7c3abae` |

The v35 image configures a fixed 900 MHz Si5351 PLL and selects a 2 MHz
multisynth output for scan-debug or a 10 MHz output for WB. Scan reads hold the
selected cell for 100 clocks (50 us at 2 MHz). For every WB read, SET, or RESET,
TM and ScanInDR are high-impedance; ScanInDL carries the checked command and then
becomes high-impedance before Wishbone access. WB read uses DAC0/1, SET uses
DAC2/3, and RESET uses DAC4/5; DAC9-13 keep the configured WB biases. At startup DAC7/VDDIO is programmed
first, followed by a 10 ms delay and the remaining rails; flash Caravel
firmware only after this completes. The VIO core and synchronized status run
from the uninterrupted 50 MHz PL clock, so 2/10 MHz switching does not detach
the Vivado debug core.

The matching permanent Caravel image is
`../../caravel_wishbone/gui_wb_mode.hex`. It accepts runtime WB READ, SET, and
RESET packets through the shared FPGA image, including the duplicated final
packet required by the current chip. Scan-debug operations remain on the same
firmware/bitstream pair, so changing GUI modes does not require reflashing.

To rebuild:

```powershell
vivado -mode batch -source ..\build_runtime_bitstream.tcl
```

Normal API/GUI operations automatically upload and reuse this image.
