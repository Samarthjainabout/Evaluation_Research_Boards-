# Requires an open i2c_host.tcl session. Register reads only.
proc assert_caravel_clock {} {
    global fmc_mux_value
    if {[mux_read] != $fmc_mux_value} { error "FMC I2C mux channel is not selected" }
    foreach {reg wanted} {
        3 0xFC 16 0x4F 17 0x4F 18 0x80
        26 0 27 1 28 0 29 0x10 30 0 31 0 32 0 33 0
        42 0 43 1 44 0 45 0xDF 46 0 47 0 48 0 49 0
        50 0 51 1 52 0 53 0xDF 54 0 55 0 56 0 57 0
    } {
        set actual [si5351_read $reg]
        if {$actual != $wanted} { error "Unexpected Si5351 register $reg=$actual" }
    }
    set status0 [si5351_read 0]
    if {$status0 & 0xA8} { error "Si5351 initialization, PLLA lock or crystal fault" }
    puts [format "SI5351_2MHZ_CONFIGURATION_AND_PLLA_LOCK_PASS status=0x%02X" $status0]
}
