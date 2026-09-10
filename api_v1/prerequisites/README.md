# Prerequisites

This folder contains the source files needed to recreate the current scan-debug hardware setup.

## Folders

- `fpga_zynq7020/`: AX7020/Zynq-7020 scan RTL plus direct DAC81416 SPI control, constraints, and Vivado programming TCL.
- `teensy_dac_adc/`: legacy DAC/ADC Teensy firmware retained only for `--legacy-teensy-dac` fallback.
- `saleae_ubuntu/`: Logic 2 automation capture helper used on the Ubuntu Saleae host.
- `caravel_wishbone/`: GUI-driven native Wishbone read/write firmware with a framed UART response.

## Deployment Targets

Copy `fpga_zynq7020/*` to:

```text
C:/Users/geethika/zynq_scan_debug
```

Copy `saleae_ubuntu/run_fpga_scan0000_la12_15_capture.py` to:

```text
/home/ubuntu-24-04/saleae-api
```

The default path does not require Teensy firmware. The API uses
`caravel_scan_debug_runtime.v`, `si5351_mode_controller.v`,
`dac81416_runtime_spi.v`, the XDC, and the runtime build/program TCL files to
create and control one universal bitstream.

The permanent v28 FPGA image configures the Si5351 from the independent FPGA
50 MHz clock. It uses 2 MHz for scan-debug and 10 MHz for WB while retaining
the fixed 900 MHz PLL setting. WB read/write applies the dedicated read-bias
profile on DAC4/5 and DAC9-13. The image holds TM and DR high-impedance in WB mode, returns DL to
high-impedance after the runtime command, applies the Caravel reset pulse, and
captures GPIO6 UART. There is no separate WB DAC image.

Power-up order is mandatory: program the full FPGA runtime, wait while it sets
DAC7/VDDIO to 4.0 V first and delays 10 ms before the remaining DAC channels,
then flash the permanent Caravel firmware. Caravel housekeeping access is not
valid before DAC7 has powered VDDIO.

WB read firmware always sends the three setup writes `0x00036472`,
`0x462B000B`, and `0x43201405`. The default/legacy read adds `0x4002AAFF` and
`0x4002AA82`. The r31c30 request adds `0x7FE2AA82` and then r31c31
`0x7FF2AA82`. Fifteen readback values are framed on UART for API/GUI display.

For WB return capture, connect Caravel `GPIO6/UART TX` to AX7020 `J10-10`
(`V15`) and connect board grounds. Keep Caravel `J2` removed while flashing;
the FPGA receives the 9600-baud framed result directly and exposes its 32-bit
value through VIO to the API/GUI.

## Important Defaults

- Read verify rails: `Vcc_set=0.5 V`, `Vcc_wl_set=2.5 V`.
- Set ramp default: `Vcc_set=1.6,2.0,2.3,2.4,2.5,2.8,3.0 V`; `Vcc_wl_set=0.5..2.0 V` in `0.1 V` steps.
- Reset ramp default: `Vcc_set=3.3,3.4,3.5,3.6,3.7 V`; `Vcc_wl_set=1.0,1.2,1.4,1.6,1.8,2.0,2.2,2.3 V`.
- Shunt: `470 ohms`, override with `--shunt-ohms`.
- The universal FPGA bitstream drives DAC81416 channels 6 (`Vcc_set`) and 3 (`Vcc_wl_set`), powers DAC7/VDDIO first, restores static support rails at startup, and accepts all cell/operation/voltage changes through VIO/JTAG.
