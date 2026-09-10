set script_dir [file dirname [file normalize [info script]]]
if {$argc < 3} {
    puts "ERROR: usage: vivado -mode batch -source program_and_run_runtime.tcl -tclargs <bitstream.bit> <probes.ltx> <command_hex> ?wait_uart|wait_uart_nonzero? ?timeout_ms?"
    exit 2
}

set bit_file [file normalize [file join $script_dir [lindex $argv 0]]]
set probes_file [file normalize [file join $script_dir [lindex $argv 1]]]
set command_hex [string trim [lindex $argv 2]]
set wait_mode [expr {$argc >= 4 ? [string tolower [lindex $argv 3]] : ""}]
set wait_uart [expr {$wait_mode eq "wait_uart" || $wait_mode eq "wait_uart_nonzero"}]
set wait_uart_nonzero [expr {$wait_mode eq "wait_uart_nonzero"}]
set uart_timeout_ms [expr {$argc >= 5 ? int([lindex $argv 4]) : 15000}]
if {[string match -nocase "0x*" $command_hex]} {
    set command_hex [string range $command_hex 2 end]
}
if {![regexp {^[0-9A-Fa-f]{16}$} $command_hex]} {
    puts "ERROR: runtime command must contain exactly 16 hexadecimal digits"
    exit 2
}
if {![file exists $bit_file]} {
    puts "ERROR: bitstream not found: $bit_file"
    exit 2
}
if {![file exists $probes_file]} {
    puts "ERROR: debug probes not found: $probes_file"
    exit 2
}

open_hw
connect_hw_server
set targets [get_hw_targets]
if {[llength $targets] == 0} {
    puts "ERROR: no JTAG hardware targets found"
    exit 1
}
set target [lindex $targets 0]
# The runtime VIO debug hub follows the selected 2/10 MHz Si5351 XCLK. Keep JTAG
# below that so XSDB traffic is sampled reliably on the AX7020/Digilent link.
set_property PARAM.FREQUENCY 1000000 $target
open_hw_target $target
puts "JTAG_FREQUENCY=[get_property PARAM.FREQUENCY $target]"
set dev ""
foreach candidate [get_hw_devices] {
    set part [get_property PART $candidate]
    if {[regexp -nocase {(xc7z|7z020)} "$candidate $part"]} {
        set dev $candidate
        break
    }
}
if {$dev eq ""} {
    puts "ERROR: no programmable xc7z020 device found in JTAG chain"
    exit 1
}

current_hw_device $dev
set_property PROGRAM.FILE $bit_file $dev
set_property PROBES.FILE $probes_file $dev
set_property FULL_PROBES.FILE $probes_file $dev
catch {refresh_hw_device $dev}

# Reuse the programmed universal image when its 64-bit runtime command probe
# is already present.  Cell/rail changes should only require a VIO commit.
set vio ""
foreach candidate [get_hw_vios -quiet -of_objects $dev] {
    set outputs [get_hw_probes -quiet -of_objects $candidate -filter {TYPE == vio_output}]
    if {
        [llength $outputs] == 1
        && [get_property WIDTH [lindex $outputs 0]] == 64
        && [get_property NAME [lindex $outputs 0]] eq "runtime_command"
    } {
        set vio $candidate
        break
    }
}
set reused [expr {$vio ne ""}]
if {$reused} {
    # Width alone cannot distinguish this permanent-WB image from earlier
    # 64-bit runtime images. Version 23 publishes 3'b011 in bits 21:19.
    set candidate_inputs [get_hw_probes -quiet -of_objects $vio -filter {TYPE == vio_input}]
    if {[llength $candidate_inputs] != 1 || [catch {refresh_hw_vio $vio}]} {
        set reused 0
        set vio ""
    } else {
        set candidate_status [string toupper [string trim [get_property INPUT_VALUE [lindex $candidate_inputs 0]]]]
        if {[string match -nocase "0x*" $candidate_status]} {
            set candidate_status [string range $candidate_status 2 end]
        }
        if {![regexp {^[0-9A-F]{16}$} $candidate_status]} {
            set reused 0
            set vio ""
        } else {
            scan $candidate_status %llx candidate_status_value
            if {[expr {($candidate_status_value >> 19) & 7}] != 3} {
                puts "BITSTREAM_SIGNATURE_MISMATCH=$candidate_status"
                set reused 0
                set vio ""
            }
        }
    }
}
if {!$reused} {
    program_hw_devices $dev
    refresh_hw_device $dev
    set vios [get_hw_vios -quiet -of_objects $dev]
    if {[llength $vios] != 1} {
        puts "ERROR: expected one runtime VIO core after programming, found [llength $vios]"
        exit 1
    }
    set vio [lindex $vios 0]
}

set output_probes [get_hw_probes -of_objects $vio -filter {TYPE == vio_output}]
set input_probes [get_hw_probes -of_objects $vio -filter {TYPE == vio_input}]
if {[llength $output_probes] != 1} {
    puts "ERROR: expected one VIO output probe, found [llength $output_probes]"
    exit 1
}
if {[llength $input_probes] != 1} {
    puts "ERROR: expected one VIO input probe, found [llength $input_probes]"
    exit 1
}
set command_probe [lindex $output_probes 0]
set status_probe [lindex $input_probes 0]

# Programming a new image can leave the host-side OUTPUT_VALUE property from
# the previous VIO core. Read the value actually present in the new core before
# toggling bit 63, otherwise the first command after programming may be lost.
refresh_hw_vio $vio

# A command starts when bit 63 changes.  Read the persistent VIO output and
# invert its trigger bit while preserving the requested payload's lower bits.
set current_hex [string toupper [string trim [get_property OUTPUT_VALUE $command_probe]]]
if {[string match -nocase "0x*" $current_hex]} {
    set current_hex [string range $current_hex 2 end]
}
if {![regexp {^[0-9A-F]{16}$} $current_hex]} {
    puts "ERROR: VIO returned an invalid 64-bit output value: $current_hex"
    exit 1
}
scan [string index $current_hex 0] %x current_nibble
scan [string index $command_hex 0] %x requested_nibble
set next_trigger [expr {((($current_nibble >> 3) & 1) ^ 1) << 3}]
set effective_first [format %X [expr {($requested_nibble & 7) | $next_trigger}]]
set effective_hex "$effective_first[string toupper [string range $command_hex 1 end]]"

set before_counter 0
if {$wait_uart} {
    refresh_hw_vio $vio
    set before_status [string toupper [string trim [get_property INPUT_VALUE $status_probe]]]
    if {[string match -nocase "0x*" $before_status]} {
        set before_status [string range $before_status 2 end]
    }
    if {![regexp {^[0-9A-Fa-f]{16}$} $before_status]} {
        puts "ERROR: VIO returned an invalid runtime status: $before_status"
        exit 1
    }
    scan $before_status %llx before_value
    set before_counter [expr {($before_value >> 3) & 1}]
}

set_property OUTPUT_VALUE $effective_hex $command_probe
commit_hw_vio $command_probe
puts "DEVICE=$dev"
puts "BITSTREAM_REUSED=$reused"
puts "RUNTIME_COMMAND_REQUESTED=$command_hex"
puts "RUNTIME_COMMAND=$effective_hex"

if {$wait_uart} {
    set deadline_ms [expr {[clock milliseconds] + $uart_timeout_ms}]
    set command_seen 0
    set uart_valid 0
    set status_hex $before_status
    while {[clock milliseconds] < $deadline_ms} {
        after 2
        refresh_hw_vio $vio
        set status_hex [string toupper [string trim [get_property INPUT_VALUE $status_probe]]]
        if {[string match -nocase "0x*" $status_hex]} {
            set status_hex [string range $status_hex 2 end]
        }
        if {![regexp {^[0-9A-Fa-f]{16}$} $status_hex]} {
            continue
        }
        scan $status_hex %llx status_value
        set counter [expr {($status_value >> 3) & 1}]
        set busy [expr {$status_value & 1}]
        if {$counter != $before_counter} {
            set command_seen 1
        }
        set valid [expr {($status_value >> 63) & 1}]
        set uart_value_preview [expr {($status_value >> 22) & 0xFFFFFFFF}]
        if {$command_seen && !$busy && $valid && (!$wait_uart_nonzero || $uart_value_preview != 0)} {
            set uart_valid 1
            break
        }
    }
    if {!$uart_valid} {
        if {$wait_uart_nonzero} {
            puts "ERROR: timed out waiting for nonzero framed Caravel UART result; status=$status_hex"
        } else {
            puts "ERROR: timed out waiting for framed Caravel UART result; status=$status_hex"
        }
        exit 1
    }
    set uart_error [expr {($status_value >> 62) & 1}]
    set uart_tag [expr {($status_value >> 54) & 0xFF}]
    set uart_value [expr {($status_value >> 22) & 0xFFFFFFFF}]
    puts "RUNTIME_STATUS=$status_hex"
    puts "WB_UART_VALID=1"
    puts [format "WB_UART_ERROR=%d" $uart_error]
    puts [format "WB_UART_TAG=0x%02X" $uart_tag]
    puts [format "WB_UART_VALUE=0x%08X" $uart_value]
}
exit
