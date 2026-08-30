# AX7020 / XC7Z020-2CLG400I constraints for Caravel scan-debug driver.
# Caravel-facing signals use J10:
#   J10-16/U14/IO1_7P -> wb_clk_i
#   J10-3 /W19/IO1_1N -> caravel_resetb_o
#   J10-4 /W18/IO1_1P -> caravel_ready_i
#   J10-5 /R14/IO1_2N -> caravel_tm_o
#   J10-9 /W15/IO1_4N -> caravel_scan_se_o / ScanInDR, active low
#   J10-7 /Y17/IO1_3N -> caravel_scan_si_o / ScanInDL
#   J10-8 /Y16/IO1_3P -> caravel_scan_cc_o

create_clock -name wb_clk_i -period 100.000 [get_ports wb_clk_i]

set_property PACKAGE_PIN U14 [get_ports wb_clk_i]
set_property PACKAGE_PIN W19 [get_ports caravel_resetb_o]
set_property PACKAGE_PIN W18 [get_ports caravel_ready_i]
set_property PACKAGE_PIN R14 [get_ports caravel_tm_o]
set_property PACKAGE_PIN W15 [get_ports caravel_scan_se_o]
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

# Weak defaults are kept as a board-level guard, but the RTL now actively
# drives the required idle waveform: TM=0, ScanInDR=1, ScanInDL=0, ScanInCC=0.
set_property PULLDOWN true [get_ports caravel_tm_o]
set_property PULLUP true [get_ports caravel_scan_se_o]
set_property PULLDOWN true [get_ports caravel_scan_si_o]
set_property PULLDOWN true [get_ports caravel_scan_cc_o]
set_property PULLDOWN true [get_ports dac_sclk_o]
set_property PULLDOWN true [get_ports dac_sdi_o]
set_property PULLUP true [get_ports dac_cs_n_o]
set_property PULLUP true [get_ports dac_ldac_n_o]
