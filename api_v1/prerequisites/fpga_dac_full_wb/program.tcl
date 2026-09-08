open_hw
connect_hw_server
set target [lindex [get_hw_targets] 0]
set_property PARAM.FREQUENCY 1000000 $target
open_hw_target $target
set dev [lindex [get_hw_devices -filter {PART =~ "xc7z020*"}] 0]
if {$dev eq ""} { error "No xc7z020 device" }
current_hw_device $dev
set_property PROGRAM.FILE [file join [file dirname [info script]] dac_full_wb.bit] $dev
program_hw_devices $dev
after 100
puts "DAC_FULL_WB_PROGRAMMED"
close_hw_target
disconnect_hw_server
close_hw
exit
