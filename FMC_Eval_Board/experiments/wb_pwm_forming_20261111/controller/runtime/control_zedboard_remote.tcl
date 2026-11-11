# Remote ZedBoard wrapper for the shared cell-control runtime.
set here [file dirname [file normalize [info script]]]
set hw_server_url 100.86.99.88:3121
set hw_target_pattern 100.86.99.88:3121/xilinx_tcf/Digilent/210248AC8BF6
set hw_target_frequency 1000000
set hw_part xc7z020
set probes_file [file normalize [file join $here .. build zedboard_adc_capture.ltx]]
set fmc_mux_address 0x74
set fmc_mux_value 0x04
set fmc_mux_bypass 1
source [file join $here control.tcl]
