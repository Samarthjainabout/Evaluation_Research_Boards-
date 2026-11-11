# Si5351A 10-MSOP: 25 MHz crystal x36 PLLA /450 = 2 MHz CLK0 and CLK1.
# CLK0 -> NT24 -> Caravel xclk. CLK1 -> U20 -> optional FPGA observation.
# CLK2 is unconnected and remains powered down. No VADJ/PMBus transactions.
set here [file dirname [file normalize [info script]]]
source [file join $here i2c_host.tcl]
mux_select_fmc
if {[mux_read] != 0x04} { error "U49 FMC channel selection failed" }
puts "KC705_MUX_CHANNEL_2_VERIFIED"
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
close_i2c_bridge
exit
