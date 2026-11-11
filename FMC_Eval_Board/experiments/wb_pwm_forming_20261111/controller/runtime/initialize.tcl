# Explicit full-image startup, preserving the FMC electrical configuration.
set here [file dirname [file normalize [info script]]]
source [file join $here i2c_host.tcl]
if {![info exists bitfile]} { set bitfile [file join $here build kc705_adc_capture.bit] }
set_property PROGRAM.FILE $bitfile $dev
set_property PROBES.FILE $probes_file $dev
set_property FULL_PROBES.FILE $probes_file $dev
program_hw_devices $dev
refresh_hw_device $dev
if {[get_property REGISTER.CONFIG_STATUS.BIT13_DONE_INTERNAL_SIGNAL_STATUS $dev]!=1} { error "FPGA DONE failed" }
# A reprogrammed VIO starts at INIT=0, but Vivado can retain OUTPUT_VALUE=1
# from the previous image. Reassert the safe values after this explicit
# programming operation, before checking or enabling the new controllers.
set fresh_run_vio [get_hw_vios -quiet -of_objects $dev -filter {CELL_NAME == "u_run_vio"}]
set fresh_probes {}
foreach name {run_enable dac_enable} {
    set p [get_hw_probes -quiet $name -of_objects $fresh_run_vio -filter {TYPE == vio_output}]
    if {[llength $p]!=1} { error "Missing fresh startup probe $name" }
    set_property OUTPUT_VALUE 0 $p
    lappend fresh_probes $p
}
commit_hw_vio $fresh_probes
# The paged bulk-evidence VIO has one read-only page selector. Bits[2:0]
# select evidence pages and bit3 is an independent fail-closed interlock. A
# Vivado session can retain the prior OUTPUT_VALUE across reprogramming, so
# establish page zero/keepalive zero before any controller startup command.
set fresh_bulk_vio [get_hw_vios -quiet -of_objects $dev -filter {CELL_NAME == "u_bulk_vio"}]
if {[llength $fresh_bulk_vio]!=1} { error "Missing fresh bulk evidence VIO" }
set fresh_bulk_outputs [get_hw_probes -quiet -of_objects $fresh_bulk_vio -filter {TYPE == vio_output}]
if {[llength $fresh_bulk_outputs]!=1} { error "Bulk evidence VIO must have one page-control output" }
set fresh_bulk_control [lindex $fresh_bulk_outputs 0]
set_property OUTPUT_VALUE 0 $fresh_bulk_control
commit_hw_vio $fresh_bulk_control
if {[expr 0x[get_property OUTPUT_VALUE $fresh_bulk_control]]!=0} {
    error "Bulk page selector/keepalive did not initialize to zero"
}
after 10
refresh_hw_vio $fresh_run_vio
set vio [get_hw_vios -quiet -of_objects $dev -filter {CELL_NAME == "u_i2c_vio"}]
set cmdprobe [get_hw_probes -quiet -of_objects $vio -filter {TYPE == vio_output}]
set statprobe [get_hw_probes -quiet -of_objects $vio -filter {TYPE == vio_input}]
set token [expr {[read_status] & 255}]
source [file join $here assert_clock.tcl]
mux_select_fmc
if {[mux_read] != $fmc_mux_value} { error "FMC channel selection failed" }
puts [format "FMC_MUX_VERIFIED address=0x%02X value=0x%02X" $fmc_mux_address $fmc_mux_value]
set status0 [si5351_read 0]
puts [format "SI5351_INITIAL_STATUS=0x%02X" $status0]
set deadline [expr {[clock milliseconds] + 1000}]
while {$status0 & 0x80} {
    if {[clock milliseconds] > $deadline} { error "Si5351 SYS_INIT stuck" }
    after 10
    set status0 [si5351_read 0]
}
if {$status0 & 0x08} { error "Si5351 reports loss of crystal" }
file mkdir [file join $here evidence]
set backup [open [file join $here evidence si5351_before_2mhz.txt] w]
foreach reg {0 1 2 3 9 15 16 17 18 24 26 27 28 29 30 31 32 33 42 43 44 45 46 47 48 49 50 51 52 53 54 55 56 57 149 165 166 177 183} {
    puts $backup [format "%d 0x%02X" $reg [si5351_read $reg]]
}
close $backup
proc write_verify {reg value} {
    si5351_write $reg $value
    set actual [si5351_read $reg]
    if {$actual != $value} { error [format "Verify reg %d: wanted %02X actual %02X" $reg $value $actual] }
}
write_verify 3 0xFF
foreach reg {16 17 18} { write_verify $reg 0x80 }
# Preserve unused register fields while choosing crystal input and disabling SS.
write_verify 15 [expr {[si5351_read 15] & 0xF3}]
write_verify 149 [expr {[si5351_read 149] & 0x7F}]
# Retain the current crystal-load setting; C22/C24 are also fitted on this PCB.
set xtal [si5351_read 183]
if {($xtal & 0xC0) == 0} { error "Reserved crystal load setting; inspect before proceeding" }
set pll_bytes {0x00 0x01 0x00 0x10 0x00 0x00 0x00 0x00}
set ms_bytes  {0x00 0x01 0x00 0xDF 0x00 0x00 0x00 0x00}
for {set i 0} {$i < 8} {incr i} {
    write_verify [expr {26+$i}] [lindex $pll_bytes $i]
    write_verify [expr {42+$i}] [lindex $ms_bytes $i]
    write_verify [expr {50+$i}] [lindex $ms_bytes $i]
}
write_verify 165 0
write_verify 166 0
# Power up, integer division, PLLA, own multisynth, same 8 mA drive as prior profile.
write_verify 16 0x4F
write_verify 17 0x4F
# Disable-state LOW on CLK0/1, preserve other output disable-state fields.
write_verify 24 [expr {[si5351_read 24] & 0xF0}]
si5351_write 177 0x20
after 20
set deadline [expr {[clock milliseconds] + 1000}]
while {1} {
    set status0 [si5351_read 0]
    if {($status0 & 0xA8) == 0} { break }
    if {[clock milliseconds] > $deadline} { error [format "PLLA/crystal not ready: %02X" $status0] }
    after 10
}
write_verify 3 0xFC
puts [format "SI5351_2MHZ_REGISTER_READBACK_PASS status=0x%02X crystal_load=0x%02X CLK0=2000000 CLK1=2000000 CLK2=disabled" $status0 $xtal]
puts "Frequency is nominal from the 25 MHz crystal; physical waveform not measured by this script."
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
    puts "RETURN_CLOCK_WINDOW $n lclk_60m=[adc_status ret_lclk_60m] adclk_10m=[adc_status ret_adclk_10m]"
}
assert_caravel_clock

for {set i 0} {$i<10} {incr i} {
 after 20
 refresh_hw_vio $control_vio
 if {![adc_status ret_lclk_60m] || ![adc_status ret_adclk_10m]} { error "ADC return clock validation failed" }
}
set sv [get_hw_vios -quiet -of_objects $dev -filter {CELL_NAME == "u_scan_vio"}]
set cp [get_hw_probes -quiet scan_command -of_objects $sv -filter {TYPE == vio_output}]
set rp0 [get_hw_probes -quiet scan_result0 -of_objects $sv -filter {TYPE == vio_input}]
set rp1 [get_hw_probes -quiet scan_result1 -of_objects $sv -filter {TYPE == vio_input}]
# Programming recreates the VIO core and controller toggle at zero, while
# Vivado can retain the previous session's OUTPUT_VALUE property.  Drive the
# known first edge explicitly; do not derive it from that stale property.
set toggle 1
set_property OUTPUT_VALUE [format %016llX [expr {($toggle<<63)|(1<<62)|(4<<58)}]] $cp
commit_hw_vio $cp
set deadline [expr {[clock milliseconds]+2000}]
while {1} {
 refresh_hw_vio $sv
 set h0 [get_property INPUT_VALUE $rp0]
 set h1 [get_property INPUT_VALUE $rp1]
 set status [expr 0x$h0]
 if {(($status>>15)&1)==$toggle && ($status&512) && !($status&256)} { break }
 if {[clock milliseconds]>$deadline} { error "Full controller startup command timeout: $h0/$h1" }
 after 10
}
if {([expr 0x$h1]>>224)!=0x53435232 || !(([expr 0x$h0]>>36)&1) || !(([expr 0x$h0]>>32)&1)} { error "Full controller startup failed: $h0/$h1" }
puts "FULL_CONTROL_STARTUP_PASS $h0 $h1"
puts "Targets: VDDA1=3.0 V VCCD2=1.8 V; ADC 10 MSPS; CPU held until operation"
close_i2c_bridge
exit
