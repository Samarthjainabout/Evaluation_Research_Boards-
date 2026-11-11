# Bench profile selected after the user confirmed no external LVDS resistors.
set here [file dirname [file normalize [info script]]]
source [file join $here i2c_host.tcl]
source [file join $here assert_clock.tcl]
assert_caravel_clock
set control_vio [get_hw_vios -quiet -of_objects $dev -filter {CELL_NAME == "u_run_vio"}]
if {[llength $control_vio] != 1} { error "ADC/DAC VIO missing" }
proc adc_probe {name type} {
    global control_vio
    set p [get_hw_probes -quiet $name -of_objects $control_vio -filter "TYPE == $type"]
    if {[llength $p] != 1} { error "Missing unique $type probe $name" }
    return $p
}
proc adc_status {name} { return [expr 0x[get_property INPUT_VALUE [adc_probe $name vio_input]]] }
refresh_hw_vio $control_vio
if {[adc_status dac_enable_2] != 1 || [adc_status dac_ready] != 1 || [adc_status dac_frame_index] != 21} {
    error "Expected DAC scan profile is not ready"
}
set run_probe [adc_probe run_enable vio_output]
if {[get_property OUTPUT_VALUE $run_probe] != 0} { error "ADC already running; refusing an implicit reset" }
set_property OUTPUT_VALUE 1 $run_probe
commit_hw_vio $run_probe
set deadline [expr {[clock milliseconds] + 2000}]
while {1} {
    refresh_hw_vio $control_vio
    if {[adc_status run_enable_2] == 1 && [adc_status pll_locked] == 1 &&
        [adc_status adc_clock_running] == 1 && [adc_status adc_reset_n] == 1 &&
        [adc_status config_done] == 1 && [adc_status sequence_index] == 7 &&
        [adc_status control_state] == 11 && [adc_status spi_ncs] == 1 && [adc_status spi_sclk] == 0} { break }
    if {[clock milliseconds] > $deadline} {
        set_property OUTPUT_VALUE 0 $run_probe
        commit_hw_vio $run_probe
        error "ADC initialization timeout; conversion clock stopped"
    }
    after 10
}
foreach name {run_enable_2 pll_locked adc_clock_running adc_reset_n config_done sequence_index control_state spi_ncs spi_sclk dac_enable_2 dac_ready dac_frame_index} {
    puts "LIVE_ADC_STATUS $name=[adc_status $name]"
}
puts "ADS5282_10MSPS_INTERNAL94_STARTUP_SEQUENCE_COMPLETED"
puts "SPI: 12=4333 03=0002 01=0010 C7=8001 DE=01C0 E3=0060 42=8001 25=0000"
puts "Normal ADC data; offset binary; LSB first; DDR; nominal sample/bit clocks 10/60 MHz."
puts "This confirms FPGA command transmission and clock gating, not ADC register readback."
for {set n 0} {$n < 10} {incr n} {
    after 20
    refresh_hw_vio $control_vio
    puts "RETURN_CLOCK_WINDOW $n valid=[adc_status ret_window_valid] lclk_seen=[adc_status ret_lclk_seen] adclk_seen=[adc_status ret_adclk_seen] lclk_60m=[adc_status ret_lclk_60m] adclk_10m=[adc_status ret_adclk_10m]"
}
assert_caravel_clock
close_i2c_bridge
exit
