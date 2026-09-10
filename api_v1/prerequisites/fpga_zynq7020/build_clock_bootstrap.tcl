set script_dir [file dirname [file normalize [info script]]]
set project_name "vivado_project_clock_bootstrap"
set project_dir [file join $script_dir $project_name]
set part_name "xc7z020clg400-2"
set bit_name "caravel_clock_bootstrap_si5351_2mhz_v1.bit"

if {[file exists $project_dir]} {
    file delete -force $project_dir
}
create_project $project_name $project_dir -part $part_name -force
add_files [file join $script_dir "caravel_clock_bootstrap.v"]
add_files [file join $script_dir "si5351_mode_controller.v"]
set_property top caravel_clock_bootstrap [current_fileset]

synth_design -top caravel_clock_bootstrap -part $part_name
set_property PACKAGE_PIN U18 [get_ports sys_clk_50m_i]
set_property PACKAGE_PIN Y14 [get_ports si5351_sda_io]
set_property PACKAGE_PIN W14 [get_ports si5351_scl_io]
set_property PACKAGE_PIN W19 [get_ports caravel_resetb_o]
set_property PACKAGE_PIN M14 [get_ports busy_o]
set_property PACKAGE_PIN M15 [get_ports done_o]
set_property IOSTANDARD LVCMOS33 [get_ports *]
set_property DRIVE 4 [get_ports {si5351_sda_io si5351_scl_io}]
set_property SLEW SLOW [get_ports {si5351_sda_io si5351_scl_io caravel_resetb_o}]
create_clock -name sys_clk_50m_i -period 20.000 [get_ports sys_clk_50m_i]
opt_design
place_design
route_design
report_timing_summary -delay_type min_max -report_unconstrained -check_timing_verbose \
    -max_paths 10 -file [file join $script_dir "clock_bootstrap_timing_summary.rpt"]
report_drc -file [file join $script_dir "clock_bootstrap_drc.rpt"]
write_bitstream -force [file join $script_dir $bit_name]
puts "BUILT $bit_name"
exit
