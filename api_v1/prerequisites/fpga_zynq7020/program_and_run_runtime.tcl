set script_dir [file dirname [file normalize [info script]]]
if {$argc < 3} {
    puts "ERROR: usage: vivado -mode batch -source program_and_run_runtime.tcl -tclargs <bitstream.bit> <probes.ltx> <command_hex>"
    exit 2
}

set bit_file [file normalize [file join $script_dir [lindex $argv 0]]]
set probes_file [file normalize [file join $script_dir [lindex $argv 1]]]
set command_hex [string trim [lindex $argv 2]]
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
# The runtime VIO debug hub is clocked at 10 MHz.  Keep JTAG comfortably
# slower so XSDB traffic is sampled reliably on the AX7020/Digilent link.
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
if {[llength $output_probes] != 1} {
    puts "ERROR: expected one VIO output probe, found [llength $output_probes]"
    exit 1
}
set command_probe [lindex $output_probes 0]

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

set_property OUTPUT_VALUE $effective_hex $command_probe
commit_hw_vio $command_probe
puts "DEVICE=$dev"
puts "BITSTREAM_REUSED=$reused"
puts "RUNTIME_COMMAND_REQUESTED=$command_hex"
puts "RUNTIME_COMMAND=$effective_hex"
exit
