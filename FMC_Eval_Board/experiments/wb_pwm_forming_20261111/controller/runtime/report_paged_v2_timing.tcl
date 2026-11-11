set root [file dirname [file dirname [file normalize [info script]]]]
set checkpoint [file join $root build zedboard_adc_capture.dcp]
set report_dir [file join $root build paged_v2_timing]
file mkdir $report_dir

open_checkpoint $checkpoint

set control_nets [get_nets -hier -quiet -regexp {.*bulk_page_control.*}]
set descriptor_nets [get_nets -hier -quiet -regexp {.*bulk_page_descriptor.*}]
set lane_nets [get_nets -hier -quiet -regexp {.*bulk_page_lane.*}]
set pager_cells [get_cells -hier -quiet -regexp {.*u_bulk_vio_pager.*}]
set vio_cells [get_cells -hier -quiet -regexp {.*u_bulk_vio.*}]

puts "PAGED_V2_TIMING_OBJECTS control_nets=[llength $control_nets] descriptor_nets=[llength $descriptor_nets] lane_nets=[llength $lane_nets] pager_cells=[llength $pager_cells] vio_cells=[llength $vio_cells]"
if {[llength $control_nets] == 0 ||
    [llength $descriptor_nets] == 0 ||
    [llength $lane_nets] == 0 ||
    [llength $pager_cells] == 0 ||
    [llength $vio_cells] == 0} {
    error "Paged-v2 timing objects are missing"
}

report_timing -through $control_nets -delay_type max -max_paths 20 -nworst 20 \
    -file [file join $report_dir selector_setup.rpt]
report_timing -through $control_nets -delay_type min -max_paths 20 -nworst 20 \
    -file [file join $report_dir selector_hold.rpt]
report_timing -through $descriptor_nets -delay_type max -max_paths 20 -nworst 20 \
    -file [file join $report_dir descriptor_setup.rpt]
report_timing -through $lane_nets -delay_type max -max_paths 20 -nworst 20 \
    -file [file join $report_dir lanes_setup.rpt]

foreach report_name {selector_setup selector_hold descriptor_setup lanes_setup} {
    set report_path [file join $report_dir ${report_name}.rpt]
    set channel [open $report_path r]
    set text [read $channel]
    close $channel
    if {[string first "Slack (VIOLATED)" $text] >= 0} {
        error "Paged-v2 timing violation in $report_name"
    }
    if {[string first "Slack (MET)" $text] < 0} {
        error "No analyzed paged-v2 timing path in $report_name"
    }
}

puts "PAGED_V2_TARGETED_TIMING_PASS"
exit
