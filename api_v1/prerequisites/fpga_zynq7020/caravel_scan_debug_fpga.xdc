# AX7020 / XC7Z020-2CLG400I constraints for Caravel scan-debug driver.
# Caravel-facing signals use J10:
#   J10-16/U14/IO1_7P -> wb_clk_i
#   J10-3 /W19/IO1_1N -> caravel_resetb_o
#   J10-4 /W18/IO1_1P -> caravel_ready_i
#   J10-5 /R14/IO1_2N -> caravel_tm_o
#   J10-9 /W15/IO1_4N -> caravel_scan_se_o / ScanInDR, active low
#   J10-10/V15/IO1_4P -> caravel_uart_tx_i / Caravel GPIO6 UART TX
#   J10-7 /Y17/IO1_3N -> caravel_scan_si_o / ScanInDL
#   J10-8 /Y16/IO1_3P -> caravel_scan_cc_o

# The bench XCLK is 2 MHz (500 ns), confirmed on Saleae D8.
create_clock -name wb_clk_i -period 500.000 [get_ports wb_clk_i]

set_property PACKAGE_PIN U14 [get_ports wb_clk_i]
set_property PACKAGE_PIN W19 [get_ports caravel_resetb_o]
set_property PACKAGE_PIN W18 [get_ports caravel_ready_i]
set_property PACKAGE_PIN R14 [get_ports caravel_tm_o]
set_property PACKAGE_PIN W15 [get_ports caravel_scan_se_o]
set_property PACKAGE_PIN V15 [get_ports caravel_uart_tx_i]
set_property PACKAGE_PIN Y17 [get_ports caravel_scan_si_o]
set_property PACKAGE_PIN Y16 [get_ports caravel_scan_cc_o]

# DAC81416 control on the free upper J10 pins.  J10-7/Y17 cannot be used for
# DAC CS because it remains the active Caravel ScanInDL connection.
#   J10-28 / V12 -> DAC SCLK
#   J10-29 / U12 -> DAC SDI
#   J10-30 / T12 -> DAC CS/SYNC (active low)
#   J10-31 / T10 -> DAC LDAC (active low; held inactive in async mode)
set_property PACKAGE_PIN V12 [get_ports dac_sclk_o]
set_property PACKAGE_PIN U12 [get_ports dac_sdi_o]
set_property PACKAGE_PIN T12 [get_ports dac_cs_n_o]
set_property PACKAGE_PIN T10 [get_ports dac_ldac_n_o]

set_property PACKAGE_PIN M14 [get_ports busy_o]
set_property PACKAGE_PIN M15 [get_ports done_o]

set_property IOSTANDARD LVCMOS33 [get_ports wb_clk_i]
set_property IOSTANDARD LVCMOS33 [get_ports caravel_resetb_o]
set_property IOSTANDARD LVCMOS33 [get_ports caravel_ready_i]
set_property IOSTANDARD LVCMOS33 [get_ports caravel_tm_o]
set_property IOSTANDARD LVCMOS33 [get_ports caravel_scan_se_o]
set_property IOSTANDARD LVCMOS33 [get_ports caravel_uart_tx_i]
set_property IOSTANDARD LVCMOS33 [get_ports caravel_scan_si_o]
set_property IOSTANDARD LVCMOS33 [get_ports caravel_scan_cc_o]
set_property IOSTANDARD LVCMOS33 [get_ports busy_o]
set_property IOSTANDARD LVCMOS33 [get_ports done_o]
set_property IOSTANDARD LVCMOS33 [get_ports dac_sclk_o]
set_property IOSTANDARD LVCMOS33 [get_ports dac_sdi_o]
set_property IOSTANDARD LVCMOS33 [get_ports dac_cs_n_o]
set_property IOSTANDARD LVCMOS33 [get_ports dac_ldac_n_o]

set_property DRIVE 8 [get_ports caravel_tm_o]
set_property DRIVE 8 [get_ports caravel_resetb_o]
set_property DRIVE 8 [get_ports caravel_scan_se_o]
set_property DRIVE 8 [get_ports caravel_scan_si_o]
set_property DRIVE 8 [get_ports caravel_scan_cc_o]
set_property SLEW SLOW [get_ports caravel_tm_o]
set_property SLEW SLOW [get_ports caravel_resetb_o]
set_property SLEW SLOW [get_ports caravel_scan_se_o]
set_property SLEW SLOW [get_ports caravel_scan_si_o]
set_property SLEW SLOW [get_ports caravel_scan_cc_o]
set_property DRIVE 8 [get_ports dac_sclk_o]
set_property DRIVE 8 [get_ports dac_sdi_o]
set_property DRIVE 8 [get_ports dac_cs_n_o]
set_property DRIVE 8 [get_ports dac_ldac_n_o]
set_property SLEW SLOW [get_ports dac_sclk_o]
set_property SLEW SLOW [get_ports dac_sdi_o]
set_property SLEW SLOW [get_ports dac_cs_n_o]
set_property SLEW SLOW [get_ports dac_ldac_n_o]

# TM, ScanInDR, and ScanInDL are true high-impedance in WB mode. Do not add
# FPGA pull resistors to these ports: physically isolating these three test
# pins restored the native WB readback during bench verification.
# Caravel actively drives UART TX.  Do not bias J10-10 from the FPGA: bench
# A-B-A testing showed that the powered pull-up suppresses WB readback while
# the same firmware returns non-zero data with J10-10 disconnected.
set_property PULLDOWN true [get_ports caravel_scan_cc_o]
set_property PULLDOWN true [get_ports dac_sclk_o]
set_property PULLDOWN true [get_ports dac_sdi_o]
set_property PULLUP true [get_ports dac_cs_n_o]
set_property PULLUP true [get_ports dac_ldac_n_o]
