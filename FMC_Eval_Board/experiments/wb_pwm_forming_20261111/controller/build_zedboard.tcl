set here [file dirname [file normalize [info script]]]
set out [file join $here build]
set project_dir [file join $here vivado_project]
set part xc7z020clg484-1
file mkdir $out

create_project -force zedboard_cell_control $project_dir -part $part
set_property target_language Verilog [current_project]

# Recreate the debug IP with the installed Vivado release. The source XCI
# snapshots came from a newer JSON schema, so the explicit definitions below
# are the portable source of truth for this build.
create_ip -name vio -vendor xilinx.com -library ip -version 3.0 -module_name adc_run_vio
set_property -dict [list \
    CONFIG.C_NUM_PROBE_IN {1} CONFIG.C_NUM_PROBE_OUT {2} \
    CONFIG.C_PROBE_IN0_WIDTH {32} \
    CONFIG.C_PROBE_OUT0_WIDTH {1} CONFIG.C_PROBE_OUT1_WIDTH {1} \
    CONFIG.C_PROBE_OUT0_INIT_VAL {0x0} CONFIG.C_PROBE_OUT1_INIT_VAL {0x0}] \
    [get_ips adc_run_vio]

create_ip -name ila -vendor xilinx.com -library ip -version 6.2 -module_name adc_capture_ila
set_property -dict [list \
    CONFIG.C_NUM_OF_PROBES {5} CONFIG.C_DATA_DEPTH {4096} \
    CONFIG.C_PROBE0_WIDTH {60} CONFIG.C_PROBE1_WIDTH {60} \
    CONFIG.C_PROBE2_WIDTH {12} CONFIG.C_PROBE3_WIDTH {32} \
    CONFIG.C_PROBE4_WIDTH {4}] [get_ips adc_capture_ila]

create_ip -name vio -vendor xilinx.com -library ip -version 3.0 -module_name vadj_vio
set_property -dict [list \
    CONFIG.C_NUM_PROBE_IN {1} CONFIG.C_NUM_PROBE_OUT {1} \
    CONFIG.C_PROBE_IN0_WIDTH {128} CONFIG.C_PROBE_OUT0_WIDTH {128} \
    CONFIG.C_PROBE_OUT0_INIT_VAL {0x00000000000000000000000000000000}] \
    [get_ips vadj_vio]

create_ip -name vio -vendor xilinx.com -library ip -version 3.0 -module_name scan_read_vio
set_property -dict [list \
    CONFIG.C_NUM_PROBE_IN {2} CONFIG.C_NUM_PROBE_OUT {1} \
    CONFIG.C_PROBE_IN0_WIDTH {256} CONFIG.C_PROBE_IN1_WIDTH {256} \
    CONFIG.C_PROBE_OUT0_WIDTH {64} \
    CONFIG.C_PROBE_OUT0_INIT_VAL {0x0000000000000000}] \
    [get_ips scan_read_vio]

# A separate paged VIO keeps bulk evidence out of ordinary command polling
# while fitting XC7Z020 with margin. Ten 256-bit payload lanes plus descriptor
# and header expose two pre pages and two post pages. Output bits [2:0] select
# read-only evidence; bit 3 is the zero-initialized fail-closed interlock.
create_ip -name vio -vendor xilinx.com -library ip -version 3.0 -module_name bulk_result_vio
set_property -dict [list \
    CONFIG.C_NUM_PROBE_IN {12} CONFIG.C_NUM_PROBE_OUT {1} \
    CONFIG.C_PROBE_OUT0_WIDTH {4} \
    CONFIG.C_PROBE_OUT0_INIT_VAL {0x0} \
    CONFIG.C_PROBE_IN0_WIDTH {256} \
    CONFIG.C_PROBE_IN1_WIDTH {256} \
    CONFIG.C_PROBE_IN2_WIDTH {256} \
    CONFIG.C_PROBE_IN3_WIDTH {256} \
    CONFIG.C_PROBE_IN4_WIDTH {256} \
    CONFIG.C_PROBE_IN5_WIDTH {256} \
    CONFIG.C_PROBE_IN6_WIDTH {256} \
    CONFIG.C_PROBE_IN7_WIDTH {256} \
    CONFIG.C_PROBE_IN8_WIDTH {256} \
    CONFIG.C_PROBE_IN9_WIDTH {256} \
    CONFIG.C_PROBE_IN10_WIDTH {256} \
    CONFIG.C_PROBE_IN11_WIDTH {256}] \
    [get_ips bulk_result_vio]

# The VIO power-up values are safety interlocks. Never permit ADC/DAC activity
# immediately after configuration.
foreach {ip prop} {
    adc_run_vio CONFIG.C_PROBE_OUT0_INIT_VAL
    adc_run_vio CONFIG.C_PROBE_OUT1_INIT_VAL
    vadj_vio CONFIG.C_PROBE_OUT0_INIT_VAL
    bulk_result_vio CONFIG.C_PROBE_OUT0_INIT_VAL
} {
    if {![regexp -nocase {^(0x)?0+$} [get_property $prop [get_ips $ip]]]} {
        error "$ip $prop must remain zero"
    }
}
generate_target all [get_ips]
set ip_runs {}
foreach ip [get_ips] {
    create_ip_run $ip
    lappend ip_runs [get_runs ${ip}_synth_1]
}
launch_runs $ip_runs -jobs 4
foreach run $ip_runs {
    wait_on_run $run
    if {[get_property PROGRESS $run] ne "100%"} {
        error "IP synthesis failed: $run status=[get_property STATUS $run]"
    }
}

add_files [glob -nocomplain [file join $here rtl *.v]]
read_xdc [file join $here constraints zedboard_adc_test.xdc]
read_xdc [file join $here constraints snapshot_timing.xdc]

synth_design -top zedboard_adc_test -part $part
if {[llength [get_debug_cores -quiet dbg_hub]] == 0} {
    create_debug_core dbg_hub dbg_hub
}
set_property C_CLK_INPUT_FREQ_HZ 50000000 [get_debug_cores dbg_hub]
set_property C_ENABLE_CLK_DIVIDER false [get_debug_cores dbg_hub]
set_property C_USER_SCAN_CHAIN 1 [get_debug_cores dbg_hub]
connect_debug_port dbg_hub/clk [get_nets sysclk]

opt_design
place_design
phys_opt_design
route_design

report_timing_summary -file [file join $out timing_summary.rpt]
report_bus_skew -file [file join $out bus_skew.rpt]
report_cdc -details -file [file join $out cdc.rpt]
report_drc -file [file join $out drc.rpt]
report_route_status -file [file join $out route_status.rpt]
report_io -file [file join $out io.rpt]
report_clock_utilization -file [file join $out clock_utilization.rpt]
report_utilization -file [file join $out utilization.rpt]

set bad [get_drc_violations -quiet -filter {SEVERITY == "Error" || SEVERITY == "Critical Warning"}]
if {[llength $bad]} {
    error "Critical DRC: $bad"
}
foreach type {setup hold} {
    set paths [get_timing_paths -max_paths 1 -nworst 1 -$type]
    if {[llength $paths] == 0} {
        error "No $type timing path was analyzed"
    }
    set slack [get_property SLACK [lindex $paths 0]]
    if {$slack < 0} {
        error "$type timing failed with slack $slack ns"
    }
}

set unconstrained [check_timing -override_defaults {no_clock no_input_delay no_output_delay} -return_string]
set check_file [open [file join $out check_timing.rpt] w]
puts $check_file $unconstrained
close $check_file

set io_file [open [file join $out port_properties.tsv] w]
puts $io_file "PORT\tPACKAGE_PIN\tBANK\tIOSTANDARD\tDIRECTION\tDRIVE\tSLEW\tDIFF_TERM\tPULLTYPE"
foreach p [lsort [get_ports]] {
    set row [list $p]
    foreach property {PACKAGE_PIN IOBANK IOSTANDARD DIRECTION DRIVE SLEW DIFF_TERM PULLTYPE} {
        lappend row [get_property $property $p]
    }
    puts $io_file [join $row "\t"]
}
close $io_file

write_debug_probes -force [file join $out zedboard_adc_capture.ltx]
write_checkpoint -force [file join $out zedboard_adc_capture.dcp]
write_bitstream -force [file join $out zedboard_adc_capture.bit]
puts "ZEDBOARD_BUILD_PASS_ADC_DAC_INIT_ZERO"
exit
