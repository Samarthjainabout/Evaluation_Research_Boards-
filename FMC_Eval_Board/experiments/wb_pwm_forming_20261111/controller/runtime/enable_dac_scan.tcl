# Requires the matching bitstream and the previously completed U1 REF/REFCMP
# hardware correction. A meter must confirm the +12 V rail at C3 pad 2.
if {$argv ne {DAC_12V_CONFIRMED}} { error "Usage: -tclargs DAC_12V_CONFIRMED" }
set here [file dirname [file normalize [info script]]]
source [file join $here i2c_host.tcl]
source [file join $here assert_clock.tcl]
assert_caravel_clock

set control_vio [get_hw_vios -quiet -of_objects $dev -filter {CELL_NAME == "u_run_vio"}]
if {[llength $control_vio] != 1} { error "ADC/DAC VIO missing" }
proc control_probe {name type} {
    global control_vio
    set p [get_hw_probes -quiet $name -of_objects $control_vio -filter "TYPE == $type"]
    if {[llength $p] != 1} { error "Expected exactly one $type probe $name" }
    return $p
}
proc control_input {name} {
    return [expr 0x[get_property INPUT_VALUE [control_probe $name vio_input]]]
}
refresh_hw_vio $control_vio
set run_probe [control_probe run_enable vio_output]
set dac_probe [control_probe dac_enable vio_output]
if {[get_property OUTPUT_VALUE $run_probe] != 0 || [control_input run_enable_2] != 0} {
    error "ADC must remain disabled for DAC startup"
}
if {[get_property OUTPUT_VALUE $dac_probe] != 0} {
    error "DAC already enabled; refusing to interrupt live supply rails"
}
set_property OUTPUT_VALUE 1 $dac_probe
commit_hw_vio $dac_probe
set deadline [expr {[clock milliseconds] + 2000}]
while {1} {
    refresh_hw_vio $control_vio
    if {[control_input dac_ready] == 1 && [control_input dac_frame_index] == 21 &&
        [control_input dac_enable_2] == 1 && [control_input dac_reset_n] == 1 &&
        [control_input dac_ncs] == 1 && [control_input dac_sclk] == 0} { break }
    if {[clock milliseconds] > $deadline} {
        set_property OUTPUT_VALUE 0 $dac_probe
        commit_hw_vio $dac_probe
        error "DAC startup timeout; DAC returned to reset"
    }
    after 10
}
foreach {name wanted} {
    run_enable_2 0 adc_clock_running 0 adc_reset_n 0 spi_ncs 1 spi_sclk 0
    dac_enable_2 1 dac_reset_n 1 dac_ready 1 dac_frame_index 21 dac_ncs 1 dac_sclk 0
} {
    set actual [control_input $name]
    puts "LIVE_STATUS $name=$actual"
    if {$actual != $wanted} { error "Unexpected controller state $name" }
}
puts "DAC_SCAN_SPI_SEQUENCE_COMPLETED"
puts "TARGETS: DAC0=0.5 DAC1=2.5 DAC2=0.5 DAC3=2.5 DAC5=2.3 DAC9=0.9 DAC10=0.9 DAC11=0.6 DAC12=1.6 DAC13=1.0 DAC14=3.0 DAC15=1.8 V"
puts "OFF: DAC4 DAC6 DAC7 DAC8; mask=0x01D0; internal reference enabled"
puts "U1 SDO is unconnected: completion confirms FPGA transmission, not DAC register readback or measured voltage."
assert_caravel_clock
close_i2c_bridge
exit
