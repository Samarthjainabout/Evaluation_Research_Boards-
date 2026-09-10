set script_dir [file dirname [file normalize [info script]]]
if {$argc < 2} {
    puts "ERROR: usage: vivado -mode batch -source runtime_vio_daemon.tcl -tclargs <bitstream.bit> <probes.ltx>"
    exit 2
}

set bit_file [file normalize [file join $script_dir [lindex $argv 0]]]
set probes_file [file normalize [file join $script_dir [lindex $argv 1]]]
set request_file [file join $script_dir "runtime_vio_request.txt"]
set invalid_response_file [file join $script_dir "runtime_vio_response.invalid.txt"]
set heartbeat_file [file join $script_dir "runtime_vio_daemon.heartbeat"]
set stop_file [file join $script_dir "runtime_vio_daemon.stop"]

proc write_atomic {path contents} {
    set tmp "$path.[pid].tmp"
    set handle [open $tmp w]
    puts -nonewline $handle $contents
    close $handle
    file rename -force $tmp $path
}

proc normalized_hex64 {value label} {
    set value [string toupper [string trim $value]]
    if {[string match -nocase "0x*" $value]} {
        set value [string range $value 2 end]
    }
    if {![regexp {^[0-9A-F]{16}$} $value]} {
        error "$label is not a 64-bit hexadecimal value: $value"
    }
    return $value
}

if {![file exists $bit_file]} {
    puts "ERROR: bitstream not found: $bit_file"
    exit 2
}
if {![file exists $probes_file]} {
    puts "ERROR: debug probes not found: $probes_file"
    exit 2
}

foreach stale [concat [list $request_file $heartbeat_file $stop_file] [glob -nocomplain [file join $script_dir "runtime_vio_response.*.txt"]]] {
    catch {file delete -force $stale}
}

open_hw
connect_hw_server
set targets [get_hw_targets]
if {[llength $targets] == 0} {
    puts "ERROR: no JTAG hardware targets found"
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
    puts "ERROR: no programmable xc7z020 device found in JTAG chain"
    exit 1
}

current_hw_device $dev
set_property PROGRAM.FILE $bit_file $dev
set_property PROBES.FILE $probes_file $dev
set_property FULL_PROBES.FILE $probes_file $dev
catch {refresh_hw_device $dev}

# Always load the requested runtime image when a new API operation starts.
# Reusing an old VIO core preserved the previous programming rail (for
# example 2.3 V after SET) until a runtime command completed.  A failed READ
# command could therefore capture at the stale destructive voltage.  Loading
# the current image re-runs the VDDIO-first sequence and establishes the safe
# read defaults (Vcc_set=0.5 V, Vcc_wl_set=2.5 V) before accepting commands.
set reused 0
program_hw_devices $dev
refresh_hw_device $dev
set vios [get_hw_vios -quiet -of_objects $dev]
if {[llength $vios] != 1} {
    puts "ERROR: expected one runtime VIO core after programming, found [llength $vios]"
    exit 1
}
set vio [lindex $vios 0]

set output_probes [get_hw_probes -of_objects $vio -filter {TYPE == vio_output}]
set input_probes [get_hw_probes -of_objects $vio -filter {TYPE == vio_input}]
if {[llength $output_probes] != 1 || [llength $input_probes] != 1} {
    puts "ERROR: expected one VIO input and one VIO output probe"
    exit 1
}
set command_probe [lindex $output_probes 0]
set status_probe [lindex $input_probes 0]

# A newly programmed device can retain a stale host-side OUTPUT_VALUE from the
# previous VIO core.  Establish a known zero command before accepting requests
# so the first trigger edge is generated exactly once.
set_property OUTPUT_VALUE 0000000000000000 $command_probe
commit_hw_vio $command_probe
refresh_hw_vio $vio

set last_request_id ""
set last_heartbeat_ms 0
write_atomic $heartbeat_file "READY [pid] [clock milliseconds]"
puts "RUNTIME_VIO_DAEMON_READY=1"
puts "DEVICE=$dev"
puts "BITSTREAM_REUSED=$reused"
flush stdout

while {![file exists $stop_file]} {
    set now_ms [clock milliseconds]
    if {$now_ms - $last_heartbeat_ms >= 1000} {
        write_atomic $heartbeat_file "READY [pid] $now_ms"
        set last_heartbeat_ms $now_ms
    }

    if {[file exists $request_file]} {
        set handle [open $request_file r]
        set request [string trim [read $handle]]
        close $handle
        if {[regexp {^([A-Za-z0-9_-]+)[ \t]+(0x)?([0-9A-Fa-f]{16})$} $request -> request_id ignored command_hex]} {
            if {$request_id ne $last_request_id} {
                # A response path unique to this request prevents the Windows
                # PowerShell poller from holding open a file that the daemon
                # must replace for the next command.
                set response_file [file join $script_dir "runtime_vio_response.$request_id.txt"]
                set started_ms [clock milliseconds]
                set rc [catch {
                    set command_hex [normalized_hex64 $command_hex "runtime command"]
                    refresh_hw_vio $vio
                    set before_hex [normalized_hex64 [get_property INPUT_VALUE $status_probe] "runtime status"]
                    scan $before_hex %llx before_value
                    set before_counter [expr {($before_value >> 3) & 1}]

                    set current_hex [normalized_hex64 [get_property OUTPUT_VALUE $command_probe] "VIO output"]
                    scan [string index $current_hex 0] %x current_nibble
                    scan [string index $command_hex 0] %x requested_nibble
                    set next_trigger [expr {((($current_nibble >> 3) & 1) ^ 1) << 3}]
                    set effective_first [format %X [expr {($requested_nibble & 7) | $next_trigger}]]
                    set effective_hex "$effective_first[string range $command_hex 1 end]"

                    # A full-array command carries 1024 packets. Scale the
                    # acknowledgement deadline with the requested count.
                    scan $command_hex %llx command_value
                    set packet_count [expr {($command_value >> 41) & 0x7FF}]
                    if {$packet_count == 0} {
                        set packet_count 1
                    }
                    # Scan packets retain the verified 2 MHz physical timing.
                    # Allow 20 ms per packet for JTAG/host margin.
                    set command_timeout_ms [expr {10000 + ($packet_count * 20)}]

                    set_property OUTPUT_VALUE $effective_hex $command_probe
                    commit_hw_vio $command_probe

                    set deadline_ms [expr {[clock milliseconds] + $command_timeout_ms}]
                    set completed 0
                    set status_hex $before_hex
                    while {[clock milliseconds] < $deadline_ms} {
                        after 1
                        refresh_hw_vio $vio
                        set status_hex [normalized_hex64 [get_property INPUT_VALUE $status_probe] "runtime status"]
                        scan $status_hex %llx status_value
                        set counter [expr {($status_value >> 3) & 1}]
                        set busy [expr {$status_value & 1}]
                        if {$counter != $before_counter && !$busy} {
                            set completed 1
                            break
                        }
                    }
                    if {!$completed} {
                        error "FPGA runtime command did not complete within ${command_timeout_ms} ms; status=$status_hex"
                    }
                } message options]

                set elapsed_ms [expr {[clock milliseconds] - $started_ms}]
                if {$rc == 0} {
                    write_atomic $response_file "$request_id OK command=$effective_hex status=$status_hex elapsed_ms=$elapsed_ms"
                } else {
                    set clean_message [string map [list "\n" " " "\r" " "] $message]
                    write_atomic $response_file "$request_id ERROR $clean_message"
                }
                set last_request_id $request_id
                catch {file delete -force $request_file}
            }
        } else {
            write_atomic $invalid_response_file "INVALID ERROR malformed_request"
            catch {file delete -force $request_file}
        }
    }
    after 5
}

catch {file delete -force $stop_file}
catch {file delete -force $heartbeat_file}
catch {close_hw_target}
catch {disconnect_hw_server}
catch {close_hw_manager}
puts "RUNTIME_VIO_DAEMON_STOPPED=1"
exit
