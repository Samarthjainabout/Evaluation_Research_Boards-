# One-shot deployment wrapper for the remote ZedBoard.  The shared I2C/VIO
# bootstrap programs the exact adjacent bitstream before discovering VIOs, then
# the ordinary control action runs with the same 1 MHz JTAG safety setting.
set here [file dirname [file normalize [info script]]]
set program_before_vio [file normalize [file join $here .. build zedboard_adc_capture.bit]]
source [file join $here control_zedboard_remote.tcl]
