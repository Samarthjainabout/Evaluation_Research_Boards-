set script_dir [file dirname [file normalize [info script]]]
if {$argc < 2} {
    puts "ERROR: usage: read_wb_uart_passive.tcl <probes.ltx> snapshot|wait|collect ?expected_tag? ?timeout_ms? ?require_nonzero_or_count?"
    exit 2
}

set probes_file [file normalize [file join $script_dir [lindex $argv 0]]]
set mode [string tolower [string trim [lindex $argv 1]]]
if {![file exists $probes_file]} {
    puts "ERROR: probes file not found: $probes_file"
    exit 2
}
if {$mode ne "snapshot" && $mode ne "wait" && $mode ne "collect"} {
    puts "ERROR: mode must be snapshot, wait, or collect"
    exit 2
}

set expected_tag 0
set timeout_ms 1000
set require_nonzero 0
set collect_count 0
if {$mode eq "wait" || $mode eq "collect"} {
    if {$argc < 3} {
        puts "ERROR: wait mode requires expected_tag"
        exit 2
    }
    set expected_text [string toupper [string trim [lindex $argv 2]]]
    if {[string match -nocase "0x*" $expected_text]} {
        set expected_text [string range $expected_text 2 end]
    }
    if {![regexp {^[0-9A-F]{1,2}$} $expected_text]} {
        puts "ERROR: invalid expected UART tag"
        exit 2
    }
    scan $expected_text %x expected_tag
    set timeout_ms [expr {$argc >= 4 ? int([lindex $argv 3]) : 15000}]
    if {$mode eq "wait"} {
        set require_nonzero [expr {$argc >= 5 ? int([lindex $argv 4]) : 0}]
    } else {
        set collect_count [expr {$argc >= 5 ? int([lindex $argv 4]) : 15}]
        if {$collect_count < 1 || $collect_count > 32 || $expected_tag + $collect_count > 256} {
            puts "ERROR: collect count/tag range is invalid"
            exit 2
        }
    }
}

proc decode_status {raw} {
    set status_hex [string toupper [string trim $raw]]
    if {[string match -nocase "0x*" $status_hex]} {
        set status_hex [string range $status_hex 2 end]
    }
    if {![regexp {^[0-9A-F]{16}$} $status_hex]} {
        error "invalid 64-bit runtime status: $status_hex"
    }
    scan $status_hex %llx status_value
    set valid [expr {($status_value >> 63) & 1}]
    set uart_error [expr {($status_value >> 62) & 1}]
    set uart_tag [expr {($status_value >> 54) & 0xFF}]
    set uart_value [expr {($status_value >> 22) & 0xFFFFFFFF}]
    set signature [expr {($status_value >> 19) & 7}]
    return [list $status_hex $valid $uart_error $uart_tag $uart_value $signature]
}

proc emit_status {decoded} {
    lassign $decoded status_hex valid uart_error uart_tag uart_value signature
    puts "RUNTIME_STATUS=$status_hex"
    puts [format "WB_UART_VALID=%d" $valid]
    puts [format "WB_UART_ERROR=%d" $uart_error]
    puts [format "WB_UART_TAG=0x%02X" $uart_tag]
    puts [format "WB_UART_VALUE=0x%08X" $uart_value]
    puts [format "WB_UART_SIGNATURE=%d" $signature]
}

open_hw
connect_hw_server
set targets [get_hw_targets]
if {[llength $targets] == 0} {
    puts "ERROR: no JTAG hardware target found"
    exit 1
}
set target [lindex $targets 0]
set_property PARAM.FREQUENCY 1000000 $target
open_hw_target $target

set dev ""
foreach candidate [get_hw_devices] {
    set part [get_property PART $candidate]
    if {[regexp -nocase {(xc7z|7z020)} "$candidate $part"]} {
        set dev $candidate
        break
    }
}
if {$dev eq ""} {
    puts "ERROR: no programmable xc7z020 device found"
    exit 1
}
current_hw_device $dev
set_property PROBES.FILE $probes_file $dev
refresh_hw_device $dev

set vio ""
foreach candidate [get_hw_vios -quiet -of_objects $dev] {
    set inputs [get_hw_probes -quiet -of_objects $candidate -filter {TYPE == vio_input}]
    if {[llength $inputs] == 1 && [get_property WIDTH [lindex $inputs 0]] == 64} {
        set vio $candidate
        break
    }
}
if {$vio eq ""} {
    puts "ERROR: UART runtime VIO is not loaded; program the UART bitstream first"
    exit 1
}
set status_probe [lindex [get_hw_probes -of_objects $vio -filter {TYPE == vio_input}] 0]

refresh_hw_vio $vio
set decoded [decode_status [get_property INPUT_VALUE $status_probe]]
if {[lindex $decoded 5] != 5 && [lindex $decoded 5] != 6 && [lindex $decoded 5] != 7} {
    emit_status $decoded
    puts "ERROR: UART-capable FPGA image signature is missing"
    exit 1
}

if {$mode eq "snapshot"} {
    emit_status $decoded
    exit 0
}

if {$mode eq "collect"} {
    array set collected {}
    set deadline [expr {[clock milliseconds] + $timeout_ms}]
    while {[clock milliseconds] < $deadline && [array size collected] < $collect_count} {
        refresh_hw_vio $vio
        if {[catch {set decoded [decode_status [get_property INPUT_VALUE $status_probe]]}]} {
            after 2
            continue
        }
        lassign $decoded status_hex valid uart_error uart_tag uart_value signature
        set offset [expr {$uart_tag - $expected_tag}]
        if {$signature == 5 && $valid && !$uart_error && $offset >= 0 && $offset < $collect_count} {
            set collected($offset) $uart_value
        }
        after 2
    }
    emit_status $decoded
    for {set index 0} {$index < $collect_count} {incr index} {
        if {[info exists collected($index)]} {
            puts [format "WB_UART_READBACK_%02d=0x%08X" [expr {$index + 1}] $collected($index)]
        } else {
            puts [format "WB_UART_READBACK_%02d=MISSING" [expr {$index + 1}]]
        }
    }
    puts [format "WB_UART_COLLECTED=%d" [array size collected]]
    if {[array size collected] == $collect_count} {
        puts "WB_UART_COLLECT_MATCH=1"
        exit 0
    }
    puts "WB_UART_COLLECT_TIMEOUT=1"
    exit 3
}

set deadline [expr {[clock milliseconds] + $timeout_ms}]
set matched 0
while {[clock milliseconds] < $deadline} {
    refresh_hw_vio $vio
    if {[catch {set decoded [decode_status [get_property INPUT_VALUE $status_probe]]}]} {
        after 2
        continue
    }
    lassign $decoded status_hex valid uart_error uart_tag uart_value signature
    if {($signature == 5 || $signature == 6 || $signature == 7) && $valid && !$uart_error && $uart_tag == $expected_tag
        && (!$require_nonzero || $uart_value != 0)} {
        set matched 1
        break
    }
    after 2
}

emit_status $decoded
if {$matched} {
    puts "WB_UART_PASSIVE_MATCH=1"
    exit 0
}
if {$require_nonzero && [lindex $decoded 1] && ![lindex $decoded 2]
    && [lindex $decoded 3] == $expected_tag && [lindex $decoded 4] == 0} {
    puts "WB_UART_NONZERO_TIMEOUT=1"
    exit 3
}
puts "ERROR: timed out waiting for a fresh passive Caravel UART frame"
exit 1
