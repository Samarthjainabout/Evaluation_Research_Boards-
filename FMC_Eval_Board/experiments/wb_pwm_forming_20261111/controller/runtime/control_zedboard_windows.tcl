# Run Vivado on the Windows host that owns the ZedBoard USB cable.
# The separately launched hw_server is local to this Vivado process.
set here [file dirname [file normalize [info script]]]
set hw_server_url localhost:3121
set hw_target_pattern localhost:3121/xilinx_tcf/Digilent/210248AC8BF6
set hw_target_frequency 1000000
set hw_part xc7z020
set probes_file [file normalize [file join $here .. build zedboard_adc_capture.ltx]]
set fmc_mux_address 0x74
set fmc_mux_value 0x04
set fmc_mux_bypass 1
source [file join $here control.tcl]
