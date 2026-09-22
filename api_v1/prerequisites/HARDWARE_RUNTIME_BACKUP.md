# Shared Caravel runtime backup

This backup contains the permanent Caravel firmware and matching FPGA runtime
used for the current scan-debug and Wishbone bench workflow.

- Scan-debug runs from the 2 MHz clock path.
- Wishbone READ, SET, and RESET run from the 10 MHz clock path.
- The FPGA controls the Si5351 clock selection, DAC81416 rails, Caravel reset,
  and UART return capture on J10-10.
- WB SET/RESET use the three configuration words followed by the same program
  packet twice, then collect the operation's automatic TDC response.
- TM, DR, and DL are high impedance during WB activity; scan drive is restored
  for scan-debug operations.
- DAC-driven Caravel pads remain configured as analog inputs.

Artifacts:

- `fpga_zynq7020/bitstreams/caravel_scan_debug_runtime_dac81416_uart_wb_highz_v35_wb_read_repair.bit`
- matching `caravel_scan_debug_runtime_dac81416_uart_wb_highz_v35_wb_read_repair.ltx`
- `caravel_wishbone/gui_wb_mode.c`
- `caravel_wishbone/gui_wb_mode.hex`

The publication-characterization baseline, hash verifier, and physical rail
measurement checklist are in `../publication/`. The API selects this v35 image
and uses the frozen nominal WB biases Iref=1.0 V, Vcomp=0.9 V,
Bias_comp2=0.6 V, VBIAS=1.6 V, and dc_bias=1.5 V.

SHA-256:

- BIT: `db1a440159e1d99e85279cd3ee342e95aa59e7a787ecc3bb361f88540a9f58b0`
- LTX: `45943f443fbe039b7790e6a5cc68af00f5bea5ee95ab6c67a87417eb1cc98809`
- C: `f69c09010b468d645c1c198325cf6179ab97c5f36fe8341ad0670aef6bbc5bc1`
- HEX: `0998ad922f6a982a113c43b696e03f73651c2cb9ea6e475a06f1b24ac7c3abae`
