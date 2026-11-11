set here [file dirname [file normalize [info script]]]
set out [file join $here build]
file mkdir $out
set report [open [file join $out connected_hardware.txt] w]

proc record {handle message} {
    puts $message
    puts $handle $message
}

open_hw_manager
connect_hw_server -allow_non_jtag
set targets [get_hw_targets]
record $report "TARGET_COUNT=[llength $targets]"
foreach target $targets {
    record $report "TARGET name=$target frequency=[get_property PARAM.FREQUENCY $target]"
}
if {[llength $targets] == 0} {
    close $report
    error "No JTAG targets detected"
}
open_hw_target [lindex $targets 0]
set devices [get_hw_devices]
if {[llength $devices] == 0} {
    close $report
    error "No JTAG devices detected"
}
foreach dev $devices {
    record $report [format "DETECTED name=%s part=%s" $dev [get_property PART $dev]]
}
close $report
close_hw_target
close_hw_manager
exit
