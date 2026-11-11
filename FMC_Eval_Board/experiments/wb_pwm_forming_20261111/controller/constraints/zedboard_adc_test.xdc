# ZedBoard Rev E FMC-LPC port of the CaravelReRAM-SenseEval design.
# Pin names were cross-checked against Digilent's Zedboard-Master.xdc and the
# Rev E.1 carrier schematic.  Do not mate the FMC card until VADJ measures
# 1.8 V at H40 with a DMM.

# Fixed 100 MHz oscillator and LED0 (both fixed 3.3 V banks).
set_property PACKAGE_PIN Y9 [get_ports sysclk_100mhz]
set_property IOSTANDARD LVCMOS33 [get_ports sysclk_100mhz]
create_clock -name sysclk_100mhz -period 10.000 [get_ports sysclk_100mhz]

set_property PACKAGE_PIN T22 [get_ports led0]
set_property IOSTANDARD LVCMOS33 [get_ports led0]
set_property DRIVE 4 [get_ports led0]
set_property SLEW SLOW [get_ports led0]

# ADS5282 serial-LVDS inputs.  ZedBoard FMC-LPC bank 34 is an HR bank at
# VADJ=1.8 V. LVDS_25 is legal for input-only use, but internal termination is
# unavailable in this configuration. Fit 100 ohms differentially at the FPGA
# receiver/interposer for all seven pairs.
set_property PACKAGE_PIN M19 [get_ports lclk_p]
set_property PACKAGE_PIN M20 [get_ports lclk_n]
set_property PACKAGE_PIN P17 [get_ports out1_p]
set_property PACKAGE_PIN P18 [get_ports out1_n]
set_property PACKAGE_PIN N22 [get_ports out2_p]
set_property PACKAGE_PIN P22 [get_ports out2_n]
set_property PACKAGE_PIN M21 [get_ports out3_p]
set_property PACKAGE_PIN M22 [get_ports out3_n]
set_property PACKAGE_PIN J18 [get_ports out5_p]
set_property PACKAGE_PIN K18 [get_ports out5_n]
set_property PACKAGE_PIN L21 [get_ports out4_p]
set_property PACKAGE_PIN L22 [get_ports out4_n]
set_property PACKAGE_PIN L18 [get_ports adclk_p]
set_property PACKAGE_PIN L19 [get_ports adclk_n]
set_property IOSTANDARD LVDS_25 [get_ports {
    lclk_p lclk_n out1_p out1_n out2_p out2_n out3_p out3_n
    out4_p out4_n out5_p out5_n adclk_p adclk_n
}]
set_property DIFF_TERM FALSE [get_ports {
    lclk_p lclk_n out1_p out1_n out2_p out2_n out3_p out3_n
    out4_p out4_n out5_p out5_n adclk_p adclk_n
}]
create_clock -name adc_lclk -period 16.667 [get_ports lclk_p]
set_clock_groups -asynchronous \
    -group [get_clocks -include_generated_clocks sysclk_100mhz] \
    -group [get_clocks adc_lclk]

# LA01 is intentionally used as two single-ended 1.8 V outputs: D8 carries
# the forwarded clock and D9 is a driven-low reference.
set_property PACKAGE_PIN N19 [get_ports fpga_mclkin_p]
set_property PACKAGE_PIN N20 [get_ports fpga_mclkin_n]
set_property IOSTANDARD LVCMOS18 [get_ports {fpga_mclkin_p fpga_mclkin_n}]
set_property DRIVE 24 [get_ports {fpga_mclkin_p fpga_mclkin_n}]
set_property SLEW SLOW [get_ports {fpga_mclkin_p fpga_mclkin_n}]

# Daughtercard -> FPGA and bidirectional FMC signals.
set_property PACKAGE_PIN R20 [get_ports ready_i]
set_property PACKAGE_PIN R21 [get_ports reset_b]
set_property PACKAGE_PIN R19 [get_ports fpga_rx]
set_property PACKAGE_PIN K19 [get_ports scan_si]
set_property PACKAGE_PIN J16 [get_ports si5351_clk1_1v8]
set_property IOSTANDARD LVCMOS18 [get_ports {
    ready_i reset_b fpga_rx scan_si si5351_clk1_1v8
}]
set_property PULLTYPE NONE [get_ports {
    ready_i reset_b fpga_rx scan_si si5351_clk1_1v8
}]
set_property DRIVE 4 [get_ports {reset_b scan_si}]
set_property SLEW SLOW [get_ports {reset_b scan_si}]

# FPGA -> daughtercard controls.
set_property PACKAGE_PIN T16 [get_ports aq1_ncs]
set_property PACKAGE_PIN T17 [get_ports aq1_nldac]
set_property PACKAGE_PIN J21 [get_ports aq1_sck]
set_property PACKAGE_PIN J22 [get_ports aq1_mosi]
set_property PACKAGE_PIN T19 [get_ports fpga_tx]
set_property PACKAGE_PIN N17 [get_ports fpga_ncs]
set_property PACKAGE_PIN N18 [get_ports fpga_nadcrst]
set_property PACKAGE_PIN P20 [get_ports fpga_sck]
set_property PACKAGE_PIN P21 [get_ports fpga_sdata]
set_property PACKAGE_PIN L17 [get_ports scan_se]
set_property PACKAGE_PIN M17 [get_ports scan_tm]
set_property PACKAGE_PIN K20 [get_ports scan_cc]
set_property PACKAGE_PIN J17 [get_ports aq1_nrst]
set_property PACKAGE_PIN J20 [get_ports adc_pd]
set_property PACKAGE_PIN K21 [get_ports fpga_loe]
set_property IOSTANDARD LVCMOS18 [get_ports {
    aq1_ncs aq1_nldac aq1_sck aq1_mosi fpga_tx fpga_ncs fpga_nadcrst
    fpga_sck fpga_sdata scan_se scan_tm scan_cc aq1_nrst adc_pd fpga_loe
}]
set_property DRIVE 4 [get_ports {
    aq1_ncs aq1_nldac aq1_sck aq1_mosi fpga_tx fpga_ncs fpga_nadcrst
    fpga_sck fpga_sdata scan_se scan_tm scan_cc aq1_nrst adc_pd fpga_loe
}]
set_property SLEW SLOW [get_ports {
    aq1_ncs aq1_nldac aq1_sck aq1_mosi fpga_tx fpga_ncs fpga_nadcrst
    fpga_sck fpga_sdata scan_se scan_tm scan_cc aq1_nrst adc_pd fpga_loe
}]
set_property PULLTYPE NONE [get_ports {
    aq1_ncs aq1_nldac aq1_sck aq1_mosi fpga_tx fpga_ncs fpga_nadcrst
    fpga_sck fpga_sdata scan_se scan_tm scan_cc aq1_nrst adc_pd fpga_loe
}]

# Fixed ZedBoard management I2C in bank 13. This is electrically compatible
# only when the daughtercard has its documented 3.3 V bypass rework: remove
# R53, R54 and U18; bridge SDA R53-2/R58-2 and SCL R54-2/R57-2. The RTL drives
# only 0 or Z, never a logic high.
set_property PACKAGE_PIN R7 [get_ports iic_scl_main]
set_property PACKAGE_PIN U7 [get_ports iic_sda_main]
set_property IOSTANDARD LVCMOS33 [get_ports {iic_scl_main iic_sda_main}]
set_property DRIVE 4 [get_ports {iic_scl_main iic_sda_main}]
set_property SLEW SLOW [get_ports {iic_scl_main iic_sda_main}]
set_property PULLTYPE NONE [get_ports {iic_scl_main iic_sda_main}]

set_false_path -to [get_cells -hier -filter {NAME =~ *input_sync_1_reg*}]
set_false_path -to [get_cells -hier -filter {NAME =~ scan_reader/*_sync_reg[0]}]
set_property BITSTREAM.CONFIG.UNUSEDPIN Pullnone [current_design]
set_property CFGBVS VCCO [current_design]
set_property CONFIG_VOLTAGE 3.3 [current_design]
