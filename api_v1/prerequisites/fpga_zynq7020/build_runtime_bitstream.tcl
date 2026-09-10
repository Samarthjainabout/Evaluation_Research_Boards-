set script_dir [file dirname [file normalize [info script]]]
set project_name "vivado_project_scan_debug_runtime"
set project_dir [file join $script_dir $project_name]
set part_name "xc7z020clg400-2"
set bit_name "caravel_scan_debug_runtime_dac81416_uart_wb_highz_v28_dualclk_iref0p9.bit"
set probes_name "caravel_scan_debug_runtime_dac81416_uart_wb_highz_v28_dualclk_iref0p9.ltx"

if {[file exists $project_dir]} {
    file delete -force $project_dir
}

create_project $project_name $project_dir -part $part_name -force
add_files [file join $script_dir "caravel_scan_debug_runtime.v"]
add_files [file join $script_dir "si5351_mode_controller.v"]
add_files [file join $script_dir "dac81416_runtime_spi.v"]
add_files [file join $script_dir "uart_rx_8n1.v"]
set_property top caravel_scan_debug_runtime [current_fileset]
add_files -fileset constrs_1 [file join $script_dir "caravel_scan_debug_fpga.xdc"]

create_ip -name vio -vendor xilinx.com -library ip -module_name vio_runtime_command
set_property -dict [list \
    CONFIG.C_NUM_PROBE_IN {1} \
    CONFIG.C_NUM_PROBE_OUT {1} \
    CONFIG.C_PROBE_IN0_WIDTH {64} \
    CONFIG.C_PROBE_OUT0_WIDTH {64} \
    CONFIG.C_PROBE_OUT0_INIT_VAL {0x0000000000000000} \
] [get_ips vio_runtime_command]
generate_target all [get_ips vio_runtime_command]
create_ip_run [get_ips vio_runtime_command]
launch_runs vio_runtime_command_synth_1 -jobs 4
wait_on_run vio_runtime_command_synth_1

synth_design -top caravel_scan_debug_runtime -part $part_name
# Runtime-only clock-management ports are absent from the legacy generated
# scan top, so bind them here after elaboration instead of forcing them into
# the shared XDC.
set_property PACKAGE_PIN U18 [get_ports sys_clk_50m_i]
set_property PACKAGE_PIN Y14 [get_ports si5351_sda_io]
set_property PACKAGE_PIN W14 [get_ports si5351_scl_io]
set_property IOSTANDARD LVCMOS33 [get_ports sys_clk_50m_i]
set_property IOSTANDARD LVCMOS33 [get_ports si5351_sda_io]
set_property IOSTANDARD LVCMOS33 [get_ports si5351_scl_io]
if {![llength [get_clocks -quiet sys_clk_50m_i]]} {
    create_clock -name sys_clk_50m_i -period 20.000 [get_ports sys_clk_50m_i]
}
set_clock_groups -asynchronous -group [get_clocks sys_clk_50m_i] -group [get_clocks wb_clk_i]
opt_design
place_design
route_design
report_timing_summary -delay_type min_max -report_unconstrained -check_timing_verbose \
    -max_paths 10 -file [file join $script_dir "runtime_timing_summary.rpt"]
report_drc -file [file join $script_dir "runtime_drc.rpt"]
write_debug_probes -force [file join $script_dir $probes_name]
write_bitstream -force [file join $script_dir $bit_name]
puts "BUILT $bit_name"
puts "PROBES $probes_name"
exit
