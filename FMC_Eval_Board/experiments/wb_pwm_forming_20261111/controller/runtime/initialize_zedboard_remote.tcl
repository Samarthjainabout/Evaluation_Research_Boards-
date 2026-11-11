# Full ZedBoard startup through the remote hw_server used by the array runner.
# This wrapper intentionally shares initialize.tcl with the local-cable path so
# programming, VIO-safe defaults, clock verification, DAC scan profile, and ADC
# bring-up remain identical after a transport-image upgrade.
set here [file dirname [file normalize [info script]]]
set hw_server_url 100.86.99.88:3121
set hw_target_pattern 100.86.99.88:3121/xilinx_tcf/Digilent/210248AC8BF6
set hw_target_frequency 1000000
set hw_part xc7z020
set bitfile [file normalize [file join $here .. build zedboard_adc_capture.bit]]
set probes_file [file normalize [file join $here .. build zedboard_adc_capture.ltx]]
set fmc_mux_address 0x74
set fmc_mux_value 0x04
set fmc_mux_bypass 1
source [file join $here initialize.tcl]
