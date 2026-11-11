# Programs only an actual XC7Z020 after the operator explicitly records the
# two physical checks that JTAG cannot measure. Example:
#   set ::env(ZEDBOARD_VADJ_MEASURED_1V8) YES
#   set ::env(DAUGHTERCARD_I2C_REWORK_VERIFIED) YES
#   vivado -mode batch -source program_zedboard_safe.tcl
set here [file dirname [file normalize [info script]]]
set bitfile [file join $here build zedboard_adc_capture.bit]

foreach name {ZEDBOARD_VADJ_MEASURED_1V8 DAUGHTERCARD_I2C_REWORK_VERIFIED} {
    if {![info exists ::env($name)] || $::env($name) ne "YES"} {
        error "$name must equal YES after a physical inspection/measurement"
    }
}
if {![file exists $bitfile]} {
    error "Bitstream not found: $bitfile"
}

open_hw_manager
connect_hw_server -allow_non_jtag
open_hw_target
set candidates [get_hw_devices -quiet -filter {PART == "xc7z020"}]
if {[llength $candidates] != 1} {
    set found {}
    foreach d [get_hw_devices -quiet] {
        lappend found "[get_property NAME $d]:[get_property PART $d]"
    }
    error "Expected exactly one xc7z020; found [join $found {, }]"
}
set dev [lindex $candidates 0]
current_hw_device $dev
refresh_hw_device -update_hw_probes false $dev
set_property PROGRAM.FILE $bitfile $dev
program_hw_devices $dev
refresh_hw_device $dev
puts "ZEDBOARD_PROGRAM_PASS [get_property NAME $dev] [get_property PART $dev]"
close_hw_manager
exit
