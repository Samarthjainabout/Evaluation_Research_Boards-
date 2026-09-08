set script_dir [file dirname [file normalize [info script]]]
set project_name "vivado_project_dac_full_wb"
set project_dir [file join $script_dir $project_name]
if {[file exists $project_dir]} { error "Use a fresh build directory" }
create_project $project_name $project_dir -part xc7z020clg400-2 -force
add_files [file join $script_dir dac_full_wb_top.v]
set_property top dac_full_wb_top [current_fileset]
add_files -fileset constrs_1 [file join $script_dir dac_full_wb.xdc]
synth_design -top dac_full_wb_top -part xc7z020clg400-2
opt_design
place_design
route_design
report_timing_summary -delay_type min_max -report_unconstrained -check_timing_verbose \
  -max_paths 10 -file [file join $script_dir timing.rpt]
report_drc -file [file join $script_dir drc.rpt]
write_bitstream -force [file join $script_dir dac_full_wb.bit]
puts "BUILT dac_full_wb.bit"
exit
