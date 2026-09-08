set script_dir [file dirname [file normalize [info script]]]
if {$argc < 2} {
    puts "ERROR: usage: program_runtime_only.tcl <bitstream.bit> <probes.ltx>"
    exit 2
}

set bit_file [file normalize [file join $script_dir [lindex $argv 0]]]
set probes_file [file normalize [file join $script_dir [lindex $argv 1]]]
if {![file exists $bit_file] || ![file exists $probes_file]} {
    puts "ERROR: runtime bitstream or probes file is missing"
    exit 2
}

open_hw
connect_hw_server
set target [lindex [get_hw_targets] 0]
if {$target eq ""} { error "No JTAG hardware target" }
set_property PARAM.FREQUENCY 1000000 $target
open_hw_target $target

set dev [lindex [get_hw_devices -filter {PART =~ "xc7z020*"}] 0]
if {$dev eq ""} { error "No xc7z020 device" }
current_hw_device $dev
set_property PROGRAM.FILE $bit_file $dev
set_property PROBES.FILE $probes_file $dev
set_property FULL_PROBES.FILE $probes_file $dev
program_hw_devices $dev
refresh_hw_device $dev

set vios [get_hw_vios -quiet -of_objects $dev]
if {[llength $vios] != 1} { error "Expected one runtime VIO after programming" }
set status_probe [lindex [get_hw_probes -of_objects [lindex $vios 0] -filter {TYPE == vio_input}] 0]
refresh_hw_vio [lindex $vios 0]
set status_hex [string toupper [string trim [get_property INPUT_VALUE $status_probe]]]
if {[string match -nocase "0x*" $status_hex]} { set status_hex [string range $status_hex 2 end] }
scan $status_hex %llx status_value
set signature [expr {($status_value >> 19) & 7}]
if {$signature != 5} { error "Expected WB-high-Z v9 signature 5, got $signature" }

puts "RUNTIME_PROGRAM_ONLY_OK=1"
puts "WB_UART_SIGNATURE=$signature"
puts "VIO_COMMAND_COMMITTED=0"
puts "CARAVEL_RESET_COMMAND=0"
puts "DAC_INITIALIZATION=0"
close_hw_target
disconnect_hw_server
close_hw
exit
