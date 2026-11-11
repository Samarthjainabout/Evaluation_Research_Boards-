set here [file dirname [file normalize [info script]]]
set out [file join $here build]
set dcp [file join $out zedboard_adc_capture.dcp]

if {![file exists $dcp]} {
    error "Routed checkpoint not found: $dcp"
}

open_checkpoint $dcp
report_bus_skew -file [file join $out bus_skew.rpt]
report_cdc -details -file [file join $out cdc.rpt]
report_methodology -file [file join $out methodology.rpt]

set setup_path [get_timing_paths -max_paths 1 -nworst 1 -setup]
set hold_path [get_timing_paths -max_paths 1 -nworst 1 -hold]
if {[llength $setup_path] == 0 || [llength $hold_path] == 0} {
    error "Routed checkpoint has no analyzed setup/hold path"
}
set setup_slack [get_property SLACK [lindex $setup_path 0]]
set hold_slack [get_property SLACK [lindex $hold_path 0]]
if {$setup_slack < 0 || $hold_slack < 0} {
    error "Routed checkpoint timing failed: setup=$setup_slack hold=$hold_slack"
}

set critical [get_drc_violations -quiet -filter {SEVERITY == "Error" || SEVERITY == "Critical Warning"}]
if {[llength $critical]} {
    error "Routed checkpoint has critical DRC violations: $critical"
}

puts "ZEDBOARD_CHECKPOINT_PASS setup_ns=$setup_slack hold_ns=$hold_slack"
exit
