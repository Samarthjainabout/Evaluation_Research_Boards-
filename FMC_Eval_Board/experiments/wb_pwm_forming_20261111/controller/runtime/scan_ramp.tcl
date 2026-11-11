# Scan ramp adapted from Evaluation_Research_Boards-/api_v1/cell_api.py
# _ramp_until / confirm_reads. No hardware access occurs when sourcing this file.
# Current uses this PCB's ADC3 / R10: active-minus-idle, 56 ohm, nominal gain 7.
# Read-stage labels carry actual step rails through the existing API protocol.
# SET uses the measured three-point min/mean/max grid. RESET uses the complete
# 96-point safe grid because the earlier three-point run did not establish HRS.
proc ramp_grid {op maxv maxwl} {
 if {$op ni {set reset}} { error "Unknown ramp operation" }
 if {$op eq "set"} {
  foreach v [list $maxv $maxwl] {
   if {![string is double -strict $v] || $v<=0 || $v>3.0} { error "SET ramp limit must be >0 and <=3.0 V" }
  }
  set grid {{2300 440} {2300 1090} {2300 1750}}
 } else {
  # Reference RESET ramp. Hardware A/B testing restored the proven Kintex/Zynq
  # path: DAC2/DAC3 drive the programming rails and OP_SET=0 selects RESET.
  # The 3.1/3.5-V reference levels are deliberately omitted so neither
  # programmed rail exceeds the 3.0-V cap.
  if {![string is double -strict $maxv] || $maxv<2.3 || $maxv>3.0 ||
      ![string is double -strict $maxwl] || $maxwl<0.94 || $maxwl>3.0} {
   error "RESET ramp limits require VCC 2.3..3.0 V and WL 0.94..3.0 V"
  }
  set vs {2300 2700 3000}
  set ws {940 1010 1070 1130 1190 1260 1320 1380 1440 1510 1570 1630 1690 1760 1820 1880 1940 2010 2070 2130 2190 2260 2320 2380 2440 2500 2570 2630 2690 2750 2820 2880}
  set grid {}
  foreach v $vs {
   if {$v>round($maxv*1000)} continue
   foreach w $ws {
    if {$w<=round($maxwl*1000)} { lappend grid [list $v $w] }
   }
  }
 }
 set bounded {}
 foreach pair $grid {
  lassign $pair v w
  if {$v<=round($maxv*1000) && $w<=round($maxwl*1000)} { lappend bounded $pair }
 }
 if {![llength $bounded]} { error "Ramp limits exclude every ramp step" }
 return $bounded
}
proc ramp_current {pair r c} {
 lassign $pair h0 h1
 set a [expr 0x$h0]; set b [expr 0x$h1]
 set packet [expr {($r<<10)|($c<<5)|$r}]
 if {($b>>224)!=0x53435232 || !($a&512) || ($a&1280) ||
     (($a>>16)&65535)!=$packet || (($a>>37)&1)} { error "Invalid ramp ADC feedback" }
 set nb [expr {($a>>64)&65535}]; set na [expr {($a>>80)&65535}]
 # The deployed ZedBoard control image uses a 160-sample baseline window,
 # while older images used 500--800 samples.  Both use roughly 12k active
 # samples and the sums below are normalized by the reported counts.
 if {$nb<128 || $nb>800 || $na<11900 || $na>12100} { error "Invalid ramp ADC sample count" }
 set base [expr {double(($a>>160)&0xffffffff)/$nb}]
 set active [expr {double(($b>>64)&0xffffffff)/$na}]
 if {$base<0 || $base>4095 || $active<0 || $active>4095} { error "Ramp ADC sum outside range" }
 return [expr {($active-$base)/2048.0/(7*56)*1e6}]
}
proc ramp_passes {op current} {
 # The stronger column-2 RESET policy continues the established 2.3..3.0-V
 # scan ramp until 52 uS (26 uA at 0.5 V).  This matches HRS_THRESHOLD in
 # cell_control_ramp_wrapper.v and intentionally uses more voltage pulses.
 return [expr {$op eq "set" ? $current>=35.0 : $current<=26.0}]
}
proc ramp_read {r c label} {
 set pair [scan_capture $r $c 0 2400 $label 0]
 set current [ramp_current $pair $r $c]
 return [list $pair $current]
}
proc program_step {r c op mv mw label} {
 global hold
 set setop [expr {$op eq "set"}]
 set vcc_code [expr {min(39321,round($mv/1000.0*65536.0/5.0))}]
 set wl_code [expr {min(39321,round($mw/1000.0*65536.0/5.0))}]
 set payload [expr {($setop<<57)|($vcc_code<<41)|($wl_code<<25)|
                         ($hold<<10)|($c<<5)|$r}]
 lassign [command 8 $payload] h0 h1
 set pair [list $h0 $h1]
 if {([expr 0x$h0]>>44)&1} {
  puts "EVENT PROGRAM_STEP pulse clipped at r$r/c$c; post-pulse READ is clean"; flush stdout
 }
 return [list $pair [ramp_current $pair $r $c]]
}
proc program_ramp {r c op maxv maxwl} {
 global hold
 set setop [expr {$op eq "set"}]
 set vcc_code [expr {min(39321,round($maxv*65536.0/5.0))}]
 set wl_code [expr {min(39321,round($maxwl*65536.0/5.0))}]
 set payload [expr {($setop<<57)|($vcc_code<<41)|($wl_code<<25)|
                         ($hold<<10)|($c<<5)|$r}]
 # The fabric executes all bounded ramp steps and ten-read confirmation.
 # Thirty seconds is a fault timeout, not an intentional delay.
 # SET uses command 11 (three points). RESET uses command 9 (all 96 bounded
 # points) because the three-point proxy was not a reliable transition grid.
 set fpga_op [expr {$op eq "set" ? 11 : 9}]
 lassign [command $fpga_op $payload [expr {$op eq "set" ? 30000 : 180000}]] h0 h1
 set a [expr 0x$h0]; set b [expr 0x$h1]
 if {($b>>224)!=0x53435232} { error "Invalid FPGA ramp result" }
 set pulses [expr {($b>>216)&255}]
 set met [expr {($b>>215)&1}]
 set pulse_clipped [expr {($b>>214)&1}]
 set last_index [expr {($b>>207)&127}]
 set confirmations [expr {($b>>203)&15}]
 set max_pulses [llength [ramp_grid $op $maxv $maxwl]]
 if {$pulses<1 || $pulses>$max_pulses || $last_index>=$max_pulses || ($met && $confirmations!=10)} {
  error "Invalid FPGA ramp summary: pulses=$pulses met=$met last_index=$last_index confirmations=$confirmations"
 }
 set currents {}
 for {set page 0} {$page*28<$pulses} {incr page} {
  lassign [command 10 $page] p0 p1
  set x [expr 0x$p0]; set y [expr 0x$p1]
  if {($y>>224)!=0x524D5032 || (($x>>16)&255)!=$pulses || (($x>>26)&3)!=$page} {
   error "Invalid FPGA ramp trace page $page"
  }
  for {set k 0} {$k<28 && [llength $currents]<$pulses} {incr k} {
   set raw [expr {$k<14 ? (($x>>(32+16*$k))&65535) : (($y>>(16*($k-14)))&65535)}]
   if {$raw&32768} { set raw [expr {$raw-65536}] }
   # FPGA trace words hold ramp_delta/2^14; ramp_delta is 48000 times
   # the average ADC-code difference.  The ramp pass/fail comparison in
   # fabric still uses the full-precision ramp_delta value.
   lappend currents [expr {$raw*(16384.0/48000.0)/2048.0/392.0*1e6}]
  }
 }
 return [dict create pair [list $h0 $h1] pulses $pulses target_met $met \
         pulse_clipped $pulse_clipped last_index $last_index currents $currents]
}
proc scan_array_program {op start count} {
 global hold
 if {$op ni {set reset} || ![string is integer -strict $start] ||
     ![string is integer -strict $count] || $start<0 || $count<1 ||
     $start+$count>1024} { error "Invalid FPGA array-program range" }
 set setop [expr {$op eq "set"}]
 set reliable [expr {$op eq "reset"}]
 # Two additional complete RESET sweeps (three total) maximize transition
 # probability while retaining the characterized 3.0-V hard ceiling. Fabric
 # escalates the default hold from 1.2 ms to 2.4 ms and then 6.0 ms.
 set retry_limit [expr {$reliable ? 2 : 0}]
 set payload [expr {($setop<<57)|($reliable<<56)|($start<<46)|
                    (($count-1)<<36)|($hold<<21)|($retry_limit<<19)}]
 puts "EVENT FPGA array scheduler: [string toupper $op] cells $start..[expr {$start+$count-1}], reliable=$reliable, max_sweeps=[expr {$retry_limit+1}]"; flush stdout
 lassign [command 12 $payload 28800000] h0 h1
 set b [expr 0x$h1]
 if {($b>>224)!=0x41525232} { error "Invalid FPGA array-program summary" }
 set completed [expr {($b>>213)&0x7ff}]
 set failures [expr {($b>>202)&0x7ff}]
 set total_pulses [expr {($b>>183)&0x7ffff}]
 set reported_start [expr {($b>>161)&0x3ff}]
 set reported_end [expr {($b>>151)&0x3ff}]
 if {$completed!=$count || $reported_start!=$start || $reported_end!=$start+$count-1 || $failures>$completed} {
  error "Invalid FPGA array-program counts/range: completed=$completed failures=$failures range=$reported_start..$reported_end"
 }
 set scale [expr {(16384.0/48000.0)/2048.0/392.0*1e6}]
 for {set page 0} {$page*14<$count} {incr page} {
  lassign [command 13 $page] p0 p1
  set x [expr 0x$p0]; set y [expr 0x$p1]
  if {($y>>224)!=0x41524732 || (($x>>16)&0xffff)!=$page*14} {
   error "Invalid FPGA array result page $page"
  }
  for {set k 0} {$k<14 && $page*14+$k<$count} {incr k} {
   set word [expr {$k<7 ? (($x>>(32+32*$k))&0xffffffff) : (($y>>(32*($k-7)))&0xffffffff)}]
   set raw [expr {$word&0xffff}]
   if {$raw&0x8000} { set raw [expr {$raw-0x10000}] }
   set pulses [expr {($word>>16)&0x3ff}]
   set met [expr {($word>>26)&1}]
   set clipped [expr {($word>>27)&1}]
   set index [expr {$start+$page*14+$k}]
   set r [expr {$index>>5}]; set c [expr {$index&31}]
   set current [expr {$raw*$scale}]
   puts "MAC_PROGRAM_RESULT $r $c $op $met [format %.6f $current]"
   puts "ARRAY_PROGRAM_DETAIL $r $c $op $pulses $met $clipped [format %.6f $current]"
  }
  flush stdout
 }
 puts "ARRAY_PROGRAM_SUMMARY $op $completed $failures $total_pulses"; flush stdout
 return [dict create operation $op completed $completed failures $failures total_pulses $total_pulses]
}
proc ramp_confirm {r c op suffix last_step} {
 set readings {}
 for {set k 1} {$k<=10} {incr k} {
  lassign [ramp_read $r $c "confirm_${op}_${k}$suffix"] pair current
  set pass [ramp_passes $op $current]
  set outcome [expr {!$pass && $last_step ? "_limit" : ($pass && $k==10 ? "_met" : "")}]
  emit_scan $r $c "confirm_${op}_${k}${outcome}$suffix" 2400 $pair
  lappend readings $current
  if {!$pass} { return [dict create target_met 0 readings $readings last_current $current] }
 }
 return [dict create target_met 1 readings $readings last_current $current]
}
proc ramp_finish {op pulses met current} {
 set verdict [expr {$met ? "target confirmed by 10 reads" : "limit reached; target NOT met"}]
 puts "EVENT RAMP [string toupper $op]: $verdict; pulses=$pulses; last=[format %.3f $current] uA nominal"
 puts "RAMP_COMPLETE $op $pulses $met [format %.6f $current]"; flush stdout
 return [dict create operation $op pulses $pulses target_met $met last_current $current]
}
proc scan_program {r c op} {
 global setv setwl resetv resetwl hold
 set maxv [expr {$op eq "set" ? $setv : $resetv}]
 set maxwl [expr {$op eq "set" ? $setwl : $resetwl}]
 set grid [ramp_grid $op $maxv $maxwl]
 set step 0; set pulses 0
 # Establish the state once before the first pulse.  Every compound
 # PROGRAM_STEP below already restores the read rails and returns a clean
 # post-pulse capture, so repeating the same standalone read before the next
 # ramp point adds a VIO round trip without adding a new safety decision.
 lassign [lindex $grid 0] first_mv first_mw
 set first_suffix "__s1_v${first_mv}_wl${first_mw}"
 lassign [ramp_read $r $c "before_$op$first_suffix"] pair current
 emit_scan $r $c "before_$op$first_suffix" 2400 $pair
 if {[ramp_passes $op $current]} {
  set result [ramp_confirm $r $c $op $first_suffix 0]
  if {[dict get $result target_met]} {
   return [ramp_finish $op $pulses 1 [dict get $result last_current]]
  }
 }
 cancel_check
 puts "EVENT PROGRAM_RAMP [string toupper $op] r$r/c$c: FPGA owns up to [llength $grid] serial single-cell steps"; flush stdout
 set result [program_ramp $r $c $op $maxv $maxwl]
 set pulses [dict get $result pulses]
 set met [dict get $result target_met]
 set currents [dict get $result currents]
 for {set k 0} {$k<$pulses} {incr k} {
  lassign [lindex $grid $k] mv mw
  puts "RAMP_POINT $r $c $op [expr {$k+1}] $mv $mw [format %.6f [lindex $currents $k]]"
 }
 lassign [lindex $grid [expr {$pulses-1}]] mv mw
 set suffix "__s${pulses}_v${mv}_wl${mw}"
 set outcome [expr {$met ? "_met" : "_limit"}]
 set pair [dict get $result pair]
 emit_scan $r $c "after_${op}${outcome}$suffix" 2400 $pair
 set current [ramp_current $pair $r $c]
 if {[dict get $result pulse_clipped]} {
  puts "EVENT PROGRAM_RAMP pulse clipping observed; restored READ result is clean"
 }
 return [ramp_finish $op $pulses $met $current]
}
