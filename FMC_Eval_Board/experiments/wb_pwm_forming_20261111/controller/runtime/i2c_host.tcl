# Host-side VIO access. Defaults preserve the KC705 behavior; a board-specific
# wrapper may set the variables below before sourcing this file.
set here [file dirname [file normalize [info script]]]
if {![info exists hw_target_pattern]} { set hw_target_pattern localhost:3121/xilinx_tcf/Xilinx/00001f14c98801 }
if {![info exists hw_part]} { set hw_part xc7k325t }
if {![info exists probes_file]} { set probes_file [file join $here build kc705_adc_capture.ltx] }
if {![info exists fmc_mux_address]} { set fmc_mux_address 0x74 }
if {![info exists fmc_mux_value]} { set fmc_mux_value 0x04 }
open_hw_manager
if {[info exists hw_server_url]} {
    connect_hw_server -url $hw_server_url -allow_non_jtag
} else {
    connect_hw_server -allow_non_jtag
}
set target [get_hw_targets -quiet $hw_target_pattern]
if {[llength $target] != 1} { error "Expected exactly one JTAG target matching $hw_target_pattern" }
if {[info exists hw_target_frequency]} {
    set_property PARAM.FREQUENCY $hw_target_frequency $target
}
current_hw_target $target
open_hw_target
set dev [get_hw_devices -quiet -filter "PART == $hw_part"]
if {[llength $dev] != 1} { error "Expected exactly one FPGA part $hw_part" }
current_hw_device $dev
if {[info exists program_before_vio]} {
    set_property PROGRAM.FILE $program_before_vio $dev
    set_property PROBES.FILE $probes_file $dev
    set_property FULL_PROBES.FILE $probes_file $dev
    program_hw_devices $dev
    puts "FPGA_PROGRAMMED exact_bitstream=$program_before_vio"
    flush stdout
}
set_property PROBES.FILE $probes_file $dev
refresh_hw_device $dev
set vio [get_hw_vios -quiet -of_objects $dev -filter {CELL_NAME == "u_i2c_vio"}]
if {[llength $vio] != 1} { error "Expected one VIO in the I2C bridge" }
set cmdprobe [get_hw_probes -quiet -of_objects $vio -filter {TYPE == vio_output}]
set statprobe [get_hw_probes -quiet -of_objects $vio -filter {TYPE == vio_input}]
if {[llength $cmdprobe] != 1 || [llength $statprobe] != 1} { error "Unexpected VIO probes" }

proc read_status {} {
    global vio statprobe
    refresh_hw_vio $vio
    set hex [get_property INPUT_VALUE $statprobe]
    return [expr 0x$hex]
}
proc to_hex128 {value} {
    set result ""
    for {set n 0} {$n < 32} {incr n} {
        set result "[string index 0123456789abcdef [expr {$value & 15}]]$result"
        set value [expr {$value >> 4}]
    }
    if {$value != 0} { error "Command overflow" }
    return $result
}
set token [expr {[read_status] & 255}]

# One transaction. Address is seven bit; bytes and returned bytes are in bus order.
# An empty byte list with nr>0 performs an address-write ACK followed by a
# repeated-start read. This safely reads U49 without changing its control byte.
proc i2c_transfer {addr bytes nr} {
    global token cmdprobe
    if {$addr < 0 || $addr > 0x7f} { error "Bad 7-bit I2C address" }
    if {[llength $bytes] > 8 || $nr < 0 || $nr > 8 || ([llength $bytes] == 0 && $nr == 0)} {
        error "Bad transaction length"
    }
    set s [read_status]
    if {$s & 256} { error "I2C bridge is already busy" }
    set token [expr {($token % 255) + 1}]
    set cmd [expr {$token | ($addr << 8) | ([llength $bytes] << 15) | ($nr << 19)}]
    set shift 23
    foreach b $bytes {
        if {$b < 0 || $b > 255} { error "Bad byte" }
        set cmd [expr {$cmd | ($b << $shift)}]
        incr shift 8
    }
    set_property OUTPUT_VALUE [to_hex128 $cmd] $cmdprobe
    commit_hw_vio $cmdprobe
    set deadline [expr {[clock milliseconds] + 10000}]
    while {1} {
        set s [read_status]
        if {($s & 255) == $token && !($s & 256)} { break }
        if {[clock milliseconds] > $deadline} { error "I2C bridge completion timeout" }
        after 10
    }
    if {$s & 512} {
        error "I2C transaction failed: address=0x[format %02X $addr], code=[expr {($s >> 10) & 63}], status=[to_hex128 $s]"
    }
    set acked [expr {($s >> 80) & 255}]
    if {$acked != [llength $bytes]} { error "Incomplete write ACK count $acked" }
    set result {}
    for {set i 0} {$i < $nr} {incr i} {
        lappend result [expr {($s >> (16+8*$i)) & 255}]
    }
    return $result
}
proc mux_select_fmc {} {
    global fmc_mux_address fmc_mux_value fmc_mux_bypass
    if {[info exists fmc_mux_bypass] && $fmc_mux_bypass} { return }
    i2c_transfer $fmc_mux_address [list $fmc_mux_value] 0
}
proc mux_read {} {
    global fmc_mux_address fmc_mux_value fmc_mux_bypass
    if {[info exists fmc_mux_bypass] && $fmc_mux_bypass} { return $fmc_mux_value }
    return [lindex [i2c_transfer $fmc_mux_address [list] 1] 0]
}
proc si5351_read {reg} { return [lindex [i2c_transfer 0x60 [list $reg] 1] 0] }
proc si5351_write {reg value} { i2c_transfer 0x60 [list $reg $value] 0 }
# FPGA-local protected clock switch.  The I2C bridge performs all five writes
# (mask, CLK0 divider, CLK1 divider, PLL reset, unmask) and four readbacks in
# one token.  The bridge stops on the first I2C error and also rejects a final
# status/mask/divider mismatch before returning completion.
proc si5351_clock_profile {mhz} {
    global token cmdprobe
    if {$mhz ni {2 10}} { error "Unsupported FPGA clock profile $mhz MHz" }
    set s [read_status]
    if {$s & 256} { error "I2C bridge is already busy" }
    set token [expr {($token % 255) + 1}]
    set select10 [expr {$mhz == 10 ? 1 : 0}]
    set cmd [expr {$token | (0xC535 << 112) | ($select10 << 111)}]
    set_property OUTPUT_VALUE [to_hex128 $cmd] $cmdprobe
    commit_hw_vio $cmdprobe
    set deadline [expr {[clock milliseconds] + 10000}]
    while {1} {
        set s [read_status]
        if {($s & 255) == $token && !($s & 256)} { break }
        if {[clock milliseconds] > $deadline} { error "FPGA clock-profile completion timeout" }
        after 10
    }
    if {$s & 512} {
        error "FPGA clock-profile failed: code=[expr {($s >> 10) & 63}], status=[to_hex128 $s]"
    }
    set signature [expr {($s >> 48) & 0xffffffff}]
    set readback [expr {($s >> 16) & 0xffffffff}]
    set status0 [expr {$readback & 255}]
    set output_mask [expr {($readback >> 8) & 255}]
    set clk0_div [expr {($readback >> 16) & 255}]
    set clk1_div [expr {($readback >> 24) & 255}]
    set expected_div [expr {$mhz == 10 ? 43 : 223}]
    if {$signature != 0x434C4B31 || ($status0 & 0xA8) ||
        $output_mask != 252 || $clk0_div != $expected_div || $clk1_div != $expected_div} {
        error [format "FPGA clock-profile receipt mismatch sig=0x%08X rb=0x%08X" $signature $readback]
    }
    return [list $status0 $output_mask $clk0_div $clk1_div]
}
proc close_i2c_bridge {} {
    close_hw_target
    disconnect_hw_server
    close_hw_manager
}

