# One Vivado session per GUI/API job. No FMC voltage or PMBus writes.
set here [file dirname [file normalize [info script]]]
if {$argc!=10} { error "action row col setV setWL resetV resetWL hold cycles jobID" }
lassign $argv action row col setv setwl resetv resetwl hold cycles jobid
if {$action ni {check hold restore_scan read read_repeat set reset scan_reset_guarded scan_set_form_guarded cycle column columns array array_set array_reset scan_mac diagnostic_reset_setrails wb_read wb_set wb_reset wb_cycle wb_column wb_array tdc_conductance_repeat tdc_conductance_repeat5 vbias_diag vbias_tdc_repeat5 vcomp_diag vcomp_tdc_repeat5 biascomp2_diag biascomp2_tdc_repeat5 bulk_profile_check pwm_profile_check pwm_skew_profile_check pwm_doe_set pwm_doe_reset pwm_doe_batch}} { error "Unknown operation" }
foreach v [list $row $col] { if {![string is integer -strict $v] || $v<0 || $v>31} { error "Cell outside 32x32" } }
set ispwmskewcheck [expr {$action eq "pwm_skew_profile_check"}]
set ispwmdoe [expr {$action in {bulk_profile_check pwm_profile_check pwm_doe_set pwm_doe_reset pwm_doe_batch}}]
if {$ispwmskewcheck} {
 foreach v [list $setv $setwl] {
  if {![string is double -strict $v] || $v<0 || $v>3.0} { error "PWM skew profile rail outside 0..3.0 V" }
 }
 foreach v [list $resetv $resetwl] {
  if {![string is double -strict $v] || $v<0 || $v>3.0} { error "PWM skew profile placeholder outside 0..3.0 V" }
 }
} elseif {$ispwmdoe} {
 foreach v [list $setv $resetv] {
  if {![string is double -strict $v] || $v<0 || $v>3.0} { error "Program voltage outside 0..3.0 V" }
 }
 foreach v [list $setwl $resetwl] {
  if {![string is integer -strict $v] || $v<16 || $v>240 || ($v&15)!=0} {
   error "PWM code must be a zero-low-nibble integer from 16 (0x10) through 240 (0xF0)"
  }
 }
} else {
 foreach v [list $setv $setwl $resetv $resetwl] {
  if {![string is double -strict $v] || $v<0 || $v>3.0} { error "Analog setting outside 0..3.0 V" }
 }
 if {$resetv<2.3 || $resetv>3.0 || $resetwl<0.94} {
  error "RESET ramp limits require VCC 2.3..3.0 V and WL 0.94..3.0 V"
 }
}
if {![string is integer -strict $hold] || $hold<20 || $hold>12000 ||
    ![string is integer -strict $cycles] || $cycles<1 || $cycles>10 ||
    ![regexp {^[a-f0-9]{32}$} $jobid]} { error "Invalid pulse, cycle count or job ID" }
set stopfile [file join $here runs "$jobid.stop"]
source [file join $here i2c_host.tcl]
set sv [get_hw_vios -quiet -of_objects $dev -filter {CELL_NAME == "u_scan_vio"}]
set cp [get_hw_probes -quiet scan_command -of_objects $sv -filter {TYPE == vio_output}]
set rp0 [get_hw_probes -quiet scan_result0 -of_objects $sv -filter {TYPE == vio_input}]
set rp1 [get_hw_probes -quiet scan_result1 -of_objects $sv -filter {TYPE == vio_input}]
if {[llength $sv]!=1 || [llength $cp]!=1 || [llength $rp0]!=1 || [llength $rp1]!=1} { error "Control image/VIO missing" }

# The paged bulk evidence VIO is intentionally separate from u_scan_vio,
# keeping normal command polling at 512 bits. Ten raw 256-bit payload lanes
# move every pre bank in two selector cycles and every post bank in two more.
# The runtime fails closed if the exact v2 capability is missing.
set bv [get_hw_vios -quiet -of_objects $dev -filter {CELL_NAME == "u_bulk_vio"}]
if {[llength $bv]!=1} { error "Bulk evidence VIO missing or ambiguous" }
set bulk_inputs [get_hw_probes -quiet -of_objects $bv -filter {TYPE == vio_input}]
if {[llength $bulk_inputs]!=12} { error "Bulk evidence VIO must expose exactly 12 input probes" }
set bulk_outputs [get_hw_probes -quiet -of_objects $bv -filter {TYPE == vio_output}]
if {[llength $bulk_outputs]!=1} { error "Bulk evidence VIO must expose exactly one page-control output probe" }
proc bulk_probe {name} {
 global bv
 set probe [get_hw_probes -quiet $name -of_objects $bv -filter {TYPE == vio_input}]
 if {[llength $probe]!=1} { error "Bulk evidence probe missing or ambiguous: $name" }
 return $probe
}
set bulk_header_probe [bulk_probe bulk_header]
set bulk_page_descriptor_probe [bulk_probe bulk_page_descriptor]
set bulk_page_lane_probes {}
for {set i 0} {$i<10} {incr i} {
 lappend bulk_page_lane_probes [bulk_probe "bulk_page_lane${i}"]
}
# Exact cardinality is the capability gate; connected output display names are
# not stable across Vivado releases, so select the sole 4-bit control output.
set bulk_page_control_probe [lindex $bulk_outputs 0]
set bulk_selected_page -1

proc bulk_header_decode {hex} {
 set h [expr 0x$hex]
 return [dict create \
  signature [expr {($h>>224)&0xFFFFFFFF}] version [expr {($h>>216)&0xFF}] \
  wb_protocol_error [expr {($h>>215)&1}] wb_echo [expr {($h>>214)&1}] \
  job_mismatch [expr {($h>>213)&1}] any_clip [expr {($h>>212)&1}] \
  any_fault [expr {($h>>211)&1}] post_done [expr {($h>>210)&1}] \
  wb_done [expr {($h>>209)&1}] pre_done [expr {($h>>208)&1}] \
  sequence [expr {($h>>176)&0xFFFFFFFF}] job_tag [expr {($h>>160)&0xFFFF}] \
  pre_crc [expr {($h>>128)&0xFFFFFFFF}] wb_crc [expr {($h>>96)&0xFFFFFFFF}] \
  post_crc [expr {($h>>64)&0xFFFFFFFF}] pre_valid [expr {($h>>55)&0x1FF}] \
  post_valid [expr {($h>>46)&0x1FF}] tdc_valid [expr {($h>>31)&0x7FFF}] \
  pre_clip [expr {($h>>22)&0x1FF}] post_clip [expr {($h>>13)&0x1FF}] \
  pre_sentinel_first [expr {($h>>12)&1}] post_sentinel_first [expr {($h>>11)&1}] \
  row [expr {($h>>6)&0x1F}] column [expr {($h>>1)&0x1F}] valid [expr {$h&1}]]
}
proc bulk_page_descriptor_decode {hex} {
 set d [expr 0x$hex]
 return [dict create \
  signature [expr {($d>>224)&0xFFFFFFFF}] version [expr {($d>>216)&0xFF}] \
  page [expr {($d>>208)&0xFF}] sequence [expr {($d>>176)&0xFFFFFFFF}] \
  job_tag [expr {($d>>160)&0xFFFF}] header_version [expr {($d>>152)&0xFF}] \
  header_valid [expr {($d>>151)&1}] page_valid [expr {($d>>150)&1}] \
  lane_valid [expr {($d>>140)&0x3FF}] row [expr {($d>>135)&0x1F}] \
  column [expr {($d>>130)&0x1F}] pre_valid [expr {($d>>121)&0x1FF}] \
  post_valid [expr {($d>>112)&0x1FF}] tdc_valid [expr {($d>>97)&0x7FFF}] \
  pre_done [expr {($d>>96)&1}] wb_done [expr {($d>>95)&1}] \
  post_done [expr {($d>>94)&1}] any_fault [expr {($d>>93)&1}] \
  job_mismatch [expr {($d>>92)&1}] wb_protocol_error [expr {($d>>91)&1}] \
  wb_echo [expr {($d>>90)&1}] pre_clip [expr {($d>>81)&0x1FF}] \
  post_clip [expr {($d>>72)&0x1FF}] pre_sentinel_first [expr {($d>>71)&1}] \
  post_sentinel_first [expr {($d>>70)&1}] reserved [expr {$d&((1<<70)-1)}]]
}
proc bulk_page_read {stage page {commit_selector 1}} {
 global bv bulk_header_probe bulk_page_descriptor_probe bulk_page_lane_probes
 global bulk_page_control_probe bulk_selected_page
 if {![string is integer -strict $page] || $page<0 || $page>3} {
  error "Invalid bulk evidence page: $page"
 }
 if {$commit_selector} {
  set_property OUTPUT_VALUE [format %X $page] $bulk_page_control_probe
  commit_hw_vio $bulk_page_control_probe
  set bulk_selected_page $page
  # page_control and the pager synchronizer use the same sysclk. JTAG commit
  # latency is already much longer than two clocks; the descriptor echo below
  # is the authoritative publication boundary.
  after 1
 } elseif {$bulk_selected_page!=$page} {
  error "Bulk page $page refresh requested while page $bulk_selected_page is selected"
 }
 if {([expr 0x[get_property OUTPUT_VALUE $bulk_page_control_probe]]&0xF)!=$page} {
  error "Bulk page control/keepalive output mismatch"
 }
 refresh_hw_vio $bv
 set header_raw [string toupper [get_property INPUT_VALUE $bulk_header_probe]]
 set descriptor_raw [string toupper [get_property INPUT_VALUE $bulk_page_descriptor_probe]]
 foreach {name value} [list header $header_raw descriptor $descriptor_raw] {
  if {[string length $value]>64} { error "Bulk $name wider than 256 bits" }
  set value "[string repeat 0 [expr {64-[string length $value]}]]$value"
  if {$name eq "header"} { set header_raw $value } else { set descriptor_raw $value }
 }
 set lanes {}
 set marker "BULK_PAGE_RAW stage=$stage page=$page header=$header_raw descriptor=$descriptor_raw"
 for {set lane 0} {$lane<10} {incr lane} {
  set raw [string toupper [get_property INPUT_VALUE [lindex $bulk_page_lane_probes $lane]]]
  if {[string length $raw]>64} { error "Bulk page lane$lane wider than 256 bits" }
  set raw "[string repeat 0 [expr {64-[string length $raw]}]]$raw"
  lappend lanes $raw
  append marker " lane$lane=$raw"
 }
 puts $marker; flush stdout
 set header [bulk_header_decode $header_raw]
 set descriptor [bulk_page_descriptor_decode $descriptor_raw]
 set errors {}
 if {[dict get $header signature]!=0x42554C4B || [dict get $header version]!=2} {
  lappend errors header_protocol
 }
 if {[dict get $descriptor signature]!=0x42504732 ||
     [dict get $descriptor version]!=2 || [dict get $descriptor page]!=$page ||
     ![dict get $descriptor page_valid] || [dict get $descriptor lane_valid]!=0x3FF ||
     [dict get $descriptor reserved]!=0} { lappend errors descriptor_protocol }
 foreach field {sequence job_tag row column pre_valid post_valid tdc_valid pre_done wb_done post_done any_fault job_mismatch wb_protocol_error wb_echo pre_clip post_clip pre_sentinel_first post_sentinel_first} {
  if {[dict get $descriptor $field]!=[dict get $header $field]} {
   lappend errors "descriptor_$field"
  }
 }
 if {[dict get $descriptor header_version]!=[dict get $header version] ||
     [dict get $descriptor header_valid]!=[dict get $header valid]} {
  lappend errors descriptor_header_snapshot
 }
 if {[llength $errors]} {
  error "Bulk page $page descriptor/header validation failed: [join $errors {, }]"
 }
 return [dict create header_raw $header_raw header $header \
  descriptor_raw $descriptor_raw descriptor $descriptor lanes $lanes]
}
proc bulk_job_tag {id} {
 # Fold the complete 128-bit identifier instead of trusting one substring.
 set tag 0
 for {set i 0} {$i<32} {incr i 4} {
  scan [string range $id $i [expr {$i+3}]] %x chunk
  set tag [expr {$tag ^ $chunk}]
 }
 return [expr {$tag&0xFFFF}]
}
proc bulk_crc32_word {crc word} {
 set crc [expr {($crc ^ ($word&0xFFFFFFFF))&0xFFFFFFFF}]
 for {set bit 0} {$bit<32} {incr bit} {
  if {$crc&1} {
   set crc [expr {(($crc>>1)^0xEDB88320)&0xFFFFFFFF}]
  } else {
   set crc [expr {($crc>>1)&0xFFFFFFFF}]
  }
 }
 return $crc
}
proc bulk_scan_crc {descriptor0 descriptor1 pairs} {
 set crc 0xFFFFFFFF
 set crc [bulk_crc32_word $crc $descriptor0]
 set crc [bulk_crc32_word $crc $descriptor1]
 foreach pair $pairs {
  lassign $pair h0 h1
  set a [expr 0x$h0]
  set b [expr 0x$h1]
  for {set word 0} {$word<8} {incr word} {
   set crc [bulk_crc32_word $crc [expr {($a>>(32*$word))&0xFFFFFFFF}]]
  }
  for {set word 0} {$word<8} {incr word} {
   set crc [bulk_crc32_word $crc [expr {($b>>(32*$word))&0xFFFFFFFF}]]
  }
 }
 return [expr {($crc^0xFFFFFFFF)&0xFFFFFFFF}]
}
proc bulk_tdc_crc {words} {
 set crc 0xFFFFFFFF
 foreach word $words { set crc [bulk_crc32_word $crc $word] }
 return [expr {($crc^0xFFFFFFFF)&0xFFFFFFFF}]
}
# Establish page zero explicitly. Output bit3 is the fail-closed interlock and
# must remain zero; bits2:0 select read-only evidence only.
set_property OUTPUT_VALUE 0 $bulk_page_control_probe
commit_hw_vio $bulk_page_control_probe
set bulk_selected_page 0
after 1
if {[expr 0x[get_property OUTPUT_VALUE $bulk_page_control_probe]]!=0} {
 error "Bulk page selector/keepalive did not commit zero"
}
set bulk_startup_page0 [bulk_page_read startup 0 0]
set bulk_initial_header_raw [dict get $bulk_startup_page0 header_raw]
set bulk_initial_header [dict get $bulk_startup_page0 header]
if {[dict get $bulk_initial_header signature]!=0x42554C4B ||
    [dict get $bulk_initial_header version]!=2} {
 error "Bulk evidence capability/version mismatch"
}
# An interrupted prior op14 deliberately latches any_fault.  A fresh phase-0
# op14 or standalone open-epoch op3 clears the entire evidence epoch before it
# can authorize programming; matched WB/post remain locally unreachable until
# a complete new pre bank is validated.  Keepalive padding is checked below.
if {[dict get $bulk_initial_header any_fault]} {
 puts "BULK_STALE_FAULT_RECOVERY_REQUIRED initial_any_fault=1"
 flush stdout
}
set bulk_job_tag16 [bulk_job_tag $jobid]
set bulk_epoch [dict get $bulk_initial_header sequence]
set bulk_epoch_open 0
set bulk_epoch_row -1
set bulk_epoch_col -1
set bulk_last_tdc_low ""
set bulk_last_tdc_high ""
set bulk_last_tdc_sequence -1
set bulk_pre_pages_validated 0
set bulk_pre_token_sequence -1
set bulk_pre_token_job -1
set bulk_pre_token_row -1
set bulk_pre_token_col -1
set bulk_pre_token_crc -1
set bulk_pre_token_header_raw ""
set bulk_pre_token_tail {}
set bulk_pre_token_order -1
set bulk_committed_pre_valid 0
set bulk_committed_pre_sequence -1
set bulk_committed_pre_job -1
set bulk_committed_pre_row -1
set bulk_committed_pre_col -1
set bulk_committed_pre_crc -1
set bulk_committed_pre_tail {}
set bulk_committed_pre_order -1
proc bulk_pre_token_invalidate {reason} {
 global bulk_pre_pages_validated bulk_pre_token_sequence bulk_pre_token_job
 global bulk_pre_token_row bulk_pre_token_col bulk_pre_token_crc bulk_pre_token_header_raw
 global bulk_pre_token_tail bulk_pre_token_order
 if {$bulk_pre_pages_validated} {
  puts "BULK_PRE_TOKEN_INVALIDATED reason=$reason"; flush stdout
 }
 set bulk_pre_pages_validated 0
 set bulk_pre_token_sequence -1; set bulk_pre_token_job -1
 set bulk_pre_token_row -1; set bulk_pre_token_col -1
 set bulk_pre_token_crc -1; set bulk_pre_token_header_raw ""
 set bulk_pre_token_tail {}
 set bulk_pre_token_order -1
}
proc bulk_pre_token_publish {sequence job row col pre_crc header_raw tail_lanes order} {
 global bulk_pre_pages_validated bulk_pre_token_sequence bulk_pre_token_job
 global bulk_pre_token_row bulk_pre_token_col bulk_pre_token_crc bulk_pre_token_header_raw
 global bulk_pre_token_tail bulk_pre_token_order
 set bulk_pre_pages_validated 1
 set bulk_pre_token_sequence $sequence; set bulk_pre_token_job $job
 set bulk_pre_token_row $row; set bulk_pre_token_col $col
 set bulk_pre_token_crc $pre_crc; set bulk_pre_token_header_raw $header_raw
 set bulk_pre_token_tail $tail_lanes
 set bulk_pre_token_order $order
 puts [format "BULK_PRE_PAGES_VALIDATED sequence=0x%08X job16=0x%04X row=%d column=%d pre_crc=0x%08X pass=1" \
       $sequence $job $row $col $pre_crc]
 flush stdout
}
proc bulk_committed_pre_clear {} {
 global bulk_committed_pre_valid bulk_committed_pre_sequence bulk_committed_pre_job
 global bulk_committed_pre_row bulk_committed_pre_col bulk_committed_pre_crc
 global bulk_committed_pre_tail bulk_committed_pre_order
 set bulk_committed_pre_valid 0; set bulk_committed_pre_sequence -1
 set bulk_committed_pre_job -1; set bulk_committed_pre_row -1
 set bulk_committed_pre_col -1; set bulk_committed_pre_crc -1
 set bulk_committed_pre_tail {}
 set bulk_committed_pre_order -1
}
proc bulk_pre_token_consume {reason} {
 global bulk_pre_pages_validated bulk_pre_token_sequence bulk_pre_token_job
 global bulk_pre_token_row bulk_pre_token_col bulk_pre_token_crc bulk_pre_token_tail
 global bulk_pre_token_order
 global bulk_committed_pre_valid bulk_committed_pre_sequence bulk_committed_pre_job
 global bulk_committed_pre_row bulk_committed_pre_col bulk_committed_pre_crc
 global bulk_committed_pre_tail bulk_committed_pre_order
 if {!$bulk_pre_pages_validated} { error "Cannot consume an invalid bulk pre token" }
 set bulk_committed_pre_valid 1
 set bulk_committed_pre_sequence $bulk_pre_token_sequence
 set bulk_committed_pre_job $bulk_pre_token_job
 set bulk_committed_pre_row $bulk_pre_token_row
 set bulk_committed_pre_col $bulk_pre_token_col
 set bulk_committed_pre_crc $bulk_pre_token_crc
 set bulk_committed_pre_tail $bulk_pre_token_tail
 set bulk_committed_pre_order $bulk_pre_token_order
 bulk_pre_token_invalidate $reason
 puts "BULK_PRE_TOKEN_CONSUMED reason=$reason"; flush stdout
}
proc bulk_pre_token_require {r c} {
 global bulk_pre_pages_validated bulk_pre_token_sequence bulk_pre_token_job
 global bulk_pre_token_row bulk_pre_token_col bulk_pre_token_crc bulk_pre_token_header_raw
 global bulk_pre_token_tail bulk_pre_token_order
 global bulk_epoch bulk_epoch_open bulk_epoch_row bulk_epoch_col bulk_job_tag16
 if {!$bulk_pre_pages_validated || !$bulk_epoch_open ||
     $bulk_pre_token_row!=$r || $bulk_pre_token_col!=$c ||
     $bulk_epoch_row!=$r || $bulk_epoch_col!=$c ||
     $bulk_pre_token_sequence!=$bulk_epoch || $bulk_pre_token_job!=$bulk_job_tag16 ||
     $bulk_pre_token_crc<0 || $bulk_pre_token_header_raw eq "" ||
     [llength $bulk_pre_token_tail]!=8 || $bulk_pre_token_order ni {0 1}} {
  bulk_pre_token_invalidate token_identity
  error "No fully validated bulk pre-page token for r$r/c$c"
 }
 # No extra page refresh here: p0+p1 were fully validated before publishing
 # this token. The FPGA clean-pre predicate is the atomic final gate against
 # any intervening op2 fault; this software token is consumed immediately
 # before the matched WB/op15 command commit.
 puts [format "BULK_PRE_TOKEN_REQUIRED sequence=0x%08X job16=0x%04X row=%d column=%d pre_crc=0x%08X pass=1" \
       $bulk_pre_token_sequence $bulk_pre_token_job $r $c $bulk_pre_token_crc]
 flush stdout
}
puts [format "BULK_TRANSPORT_CAPABILITY signature=BPG2 version=2 input_probes=12 output_probes=1 page_count=4 page_lanes=10 selector_bits=3 keepalive_bit=3 selector_cycles=4 refreshes=5 scan_frames=18 tdc_words=15 job16=0x%04X initial_sequence=0x%08X pass=1" \
      $bulk_job_tag16 $bulk_epoch]
flush stdout
proc status_read {} {
 global sv rp0 rp1
 refresh_hw_vio $sv
 return [list [get_property INPUT_VALUE $rp0] [get_property INPUT_VALUE $rp1]]
}
proc cancel_check {} {
 global stopfile restoring
 if {!$restoring && [file exists $stopfile]} {
  if {[llength [info commands bulk_pre_token_invalidate]]} {
   bulk_pre_token_invalidate cancel
  }
  error "CANCELLED"
 }
}
set restoring 0
set pwm_scan_restored 0
set last_wb_shunt [dict create]
proc command {op payload {timeout 6000}} {
 global cp
 if {$op!=4} { cancel_check }
 set old [expr 0x[get_property OUTPUT_VALUE $cp]]
 set toggle [expr {1-(($old>>63)&1)}]
 set value [expr {($toggle<<63)|(1<<62)|($op<<58)|$payload}]
 if {$op!=4} { cancel_check }
 set_property OUTPUT_VALUE [format %016llX $value] $cp
 if {$op!=4 && [catch {cancel_check} cancel_message cancel_options]} {
  # OUTPUT_VALUE is only staged at this point. Restore the exact prior value
  # so cleanup op4 observes and inverts the hardware's still-current toggle.
  set_property OUTPUT_VALUE [format %016llX $old] $cp
  return -options $cancel_options $cancel_message
 }
 commit_hw_vio $cp
 set deadline [expr {[clock milliseconds]+$timeout}]
 set last_array_completed -1
 while {1} {
  if {$op!=4} { cancel_check }
  lassign [status_read] h0 h1
  set s [expr 0x$h0]
  if {$op==12} {
   set q [expr 0x$h1]
   if {($q>>224)==0x41525032} {
    set completed [expr {($q>>213)&0x7ff}]
    if {$completed!=$last_array_completed} {
     set failed [expr {($q>>202)&0x7ff}]
     set index [expr {($q>>173)&0x3ff}]
     puts "ARRAY_PROGRESS $completed $failed [expr {$index>>5}] [expr {$index&31}]"; flush stdout
     set last_array_completed $completed
    }
   }
  }
  if {(($s>>15)&1)==$toggle && ($s&512) && !($s&256)} { break }
  if {[clock milliseconds]>$deadline} { error "Controller timeout op=$op state=[expr {$s&255}] status=$h0/$h1" }
  after 10
 }
 if {$s&1024} { error "Controller fault [expr {($s>>11)&15}]: $h0 / $h1" }
 return [list $h0 $h1]
}
proc hold_cpu {{wb 0}} { command 4 [expr {$wb<<57}] }
proc dac {ch voltage} {
 if {$ch ni {0 1 2 3 4 5 6 9 10 11 12 13} || $voltage<0 || $voltage>3} { error "Protected DAC channel/value" }
 # Floor the maximum code so even rounding cannot command above 3.0 V.
 command 2 [expr {(0x10+$ch)<<16 | min(39321,round($voltage*65536.0/5.0))}]
}
proc power_mask {wb} {
 # The reference disables DAC6, its physical VCC_SET output. This PCB uses
 # DAC2 for VCC_SET, so disable bit2 as well as WL_SET/unused outputs in WB.
 command 2 [expr {$wb ? 0x0901CC : 0x0901D0}]
}
proc power_mask_value {value} {
 if {$value ni {0x01D0 0x01F3 0x01CF 0x01CC}} { error "Protected DAC power mask" }
 command 2 [expr {(0x09<<16)|$value}]
}
proc dac_profile_local {kind {voltage 0.0}} {
 if {$kind eq "scan"} {
  set profile 0; set code 0
 } elseif {$kind eq "set"} {
  set profile 1
  if {$voltage<0 || $voltage>3} { error "Protected SET profile voltage" }
  set code [expr {min(39321,round($voltage*65536.0/5.0))}]
 } elseif {$kind eq "reset"} {
  set profile 2
  if {$voltage<0 || $voltage>3} { error "Protected RESET profile voltage" }
  set code [expr {min(39321,round($voltage*65536.0/5.0))}]
 } else {
  error "Unknown local DAC profile $kind"
 }
 set payload [expr {(0x2AD<<48)|($profile<<46)|($code<<30)}]
 lassign [command 2 $payload 10000] h0 h1
 set receipt [expr 0x$h1]
 if {($receipt>>224)!=0x44504631 || ($receipt&3)!=$profile ||
     (($receipt>>2)&15)!=12 || (($receipt>>6)&0xffff)!=$code ||
     (($receipt>>22)&1) || !(($receipt>>23)&1)} {
  error "FPGA-local DAC profile receipt mismatch kind=$kind h1=$h1"
 }
 puts [format "DAC_PROFILE_MACRO kind=%s voltage=%.6f code=0x%04X frames=13 verified=1" $kind $voltage $code]
 flush stdout
}
proc scan_profile {{cpu_already_held 0}} {
 if {$cpu_already_held ni {0 1}} { error "Invalid scan-profile CPU-held flag" }
 if {!$cpu_already_held} { hold_cpu }
 # Explicit evaluation-board scan-read profile for DAC0..6.
 # DAC0/1 are READ, DAC2/3 mirror the proven scan path, DAC4 is WL_RESET,
 # DAC5 is VDDA2/RESET, and DAC6 remains unused and powered down.
 dac_profile_local scan
 puts "DAC_PROFILE scan_read d0=0.5 d1=2.5 d2=0.5 d3=2.5 d4=0 d5=2.3 d6=0 mask=0x01D0"
 flush stdout
}
proc phase_p_profile {op voltage {wl_voltage {}} {cpu_already_held 0}} {
 if {$cpu_already_held ni {0 1}} { error "Invalid Phase-P CPU-held flag" }
 if {!$cpu_already_held} { hold_cpu 1 }
 if {$wl_voltage eq {}} { set wl_voltage $voltage }
 foreach value [list $voltage $wl_voltage] {
  if {![string is double -strict $value] || $value<0 || $value>3.0} {
   error "Protected Phase-P rail outside 0..3.0 V"
  }
 }
 if {$op eq "set"} {
  # SET: DAC2=VCC_SET and DAC3=WL_SET.  READ/RESET paths are zero and off.
  # The FPGA-local macro is retained for the common tied-rail path.  A skewed
  # profile is programmed while the CPU is held, before any WB packet can be
  # emitted, so independent rails do not require a new bitstream protocol.
  if {abs($voltage-$wl_voltage)<1.0e-12} {
   dac_profile_local set $voltage
  } else {
   dac 0 0; dac 1 0; dac 2 $voltage; dac 3 $wl_voltage
   dac 4 0; dac 5 0; dac 6 0
   power_mask_value 0x01F3
  }
  set mask 0x01F3
  set d2 $voltage; set d3 $wl_voltage; set d4 0; set d5 0
 } elseif {$op eq "reset"} {
  # RESET: DAC4=WL_RESET and DAC5=VDDA2/RESET.  READ/SET paths are zero and off.
  if {abs($voltage-$wl_voltage)<1.0e-12} {
   dac_profile_local reset $voltage
  } else {
   dac 0 0; dac 1 0; dac 2 0; dac 3 0
   dac 4 $wl_voltage; dac 5 $voltage; dac 6 0
   power_mask_value 0x01CF
  }
  set mask 0x01CF
  set d2 0; set d3 0; set d4 $wl_voltage; set d5 $voltage
 } else {
  error "Unknown Phase-P profile $op"
 }
 puts "DAC_PROFILE phase_p_$op program_v=$voltage program_wl_v=$wl_voltage d0=0 d1=0 d2=$d2 d3=$d3 d4=$d4 d5=$d5 d6=0 mask=$mask"
 flush stdout
}
proc wb_profile {{vbias 1.6} {dc_bias 1.5} {vcomp 0.9} {bias_comp2 0.6}} {
 hold_cpu 1
 dac 2 0.5; dac 3 0; dac 4 0.5; dac 5 2.5
 # Evaluation-board WB bias profile, encoded for this PCB's 0..5 V ranges.
 dac 9 1.0; dac 10 $vcomp; dac 11 $bias_comp2; dac 12 $vbias; dac 13 $dc_bias
 power_mask 1
 puts "WB_BIAS_PROFILE vbias=$vbias iref=1.0 vcomp=$vcomp bias_comp2=$bias_comp2 dc_bias=$dc_bias dac0_6_isolation=physical_disconnection"
 flush stdout
}
proc write_verify {reg value} {
 si5351_write $reg $value
 if {[si5351_read $reg]!=$value} { error "Si5351 readback mismatch register $reg" }
}
proc clock_profile {mhz} {
 global fmc_mux_value
 # CPU must already be held. PLLA stays 900 MHz; CLK0/1 match.
 if {$mhz ni {2 10}} { error "Unsupported clock profile $mhz MHz" }
 mux_select_fmc
 if {[mux_read]!=$fmc_mux_value} { error "FMC I2C mux mismatch" }
 lassign [si5351_clock_profile $mhz] status0 output_mask clk0_div clk1_div
 puts [format "EVENT clock_mhz=%d fpga_macro_verified=1 status0=0x%02X mask=0x%02X clk0_div=%d clk1_div=%d" \
       $mhz $status0 $output_mask $clk0_div $clk1_div]
 flush stdout
}
proc emit_scan {r c label clocks pair} {
 lassign $pair h0 h1
 puts "CELL_RESULT $r $c $label $clocks $h0 $h1"; flush stdout
}
proc scan_capture {r c op clocks label {emit 1} {allow_program_clip 0} {mac_input {}}} {
 cancel_check
 set mac_payload 0
 if {$mac_input ne {}} {
  if {![string is integer -strict $mac_input] || $mac_input<-128 || $mac_input>127 || $op} {
   error "MAC input must be signed 8-bit and use a READ packet"
  }
  set mac_payload [expr {(1<<56)|(($mac_input&255)<<48)}]
 }
 lassign [command 1 [expr {($op<<57)|$mac_payload|($clocks<<10)|($c<<5)|$r}]] h0 h1
 set pair [list $h0 $h1]
 if {$emit} { emit_scan $r $c $label $clocks $pair }
 # A SET/RESET programming pulse can legitimately exceed the current-sense ADC
 # span.  Like the reference Kintex flow, programming decisions are made only
 # from the restored read-profile capture after the pulse.  Preserve and flag
 # the saturated pulse trace, but never relax clipping checks for a READ.
 if {([expr 0x$h0]>>37)&1} {
  if {!$emit} { emit_scan $r $c $label $clocks $pair }
  if {$allow_program_clip} {
   puts "EVENT PROGRAM_PULSE_CLIPPED $label: trace retained; continuing to post-pulse read"; flush stdout
   return $pair
  }
  error "ADC clipping on a current-sense channel; stopped before another pulse"
 }
 return $pair
}
source [file join $here scan_ramp.tcl]
proc mac_config {} {
 global here jobid
 set path [file join $here runs "$jobid.mac.txt"]
 if {![file exists $path]} { error "MAC configuration is missing" }
 set f [open $path r]
 set header [split [string trim [gets $f]]]
 if {[llength $header] ni {3 4}} { close $f; error "Invalid MAC configuration header" }
 lassign $header rows cols program program_start
 if {$program_start eq {}} { set program_start 0 }
 if {![string is integer -strict $rows] || $rows<1 || $rows>32 ||
     ![string is integer -strict $cols] || $cols<1 || $cols>32 || $program ni {0 1} ||
     ![string is integer -strict $program_start] || $program_start<0 || $program_start>$rows*$cols} {
  close $f; error "MAC dimensions/program flag are invalid"
 }
 set inputs [split [string trim [gets $f]]]
 if {[llength $inputs]!=$rows} { close $f; error "MAC input length differs from matrix rows" }
 foreach value $inputs {
  if {![string is integer -strict $value] || $value<-128 || $value>127} {
   close $f; error "MAC inputs must be signed 8-bit integers"
  }
 }
 set matrix {}
 for {set r 0} {$r<$rows} {incr r} {
  set weights [split [string trim [gets $f]]]
  if {[llength $weights]!=$cols} { close $f; error "MAC matrix is not rectangular" }
  foreach weight $weights {
   if {$weight ni {0 1}} { close $f; error "MAC weights must be binary" }
  }
  lappend matrix $weights
 }
 close $f
 return [dict create rows $rows cols $cols program $program program_start $program_start inputs $inputs matrix $matrix]
}
proc mac_clear {} {
 lassign [command 7 [expr {1<<57}]] h0 h1
 set b [expr 0x$h1]
 if {($b>>224)!=0x4D414332 || ($b&((1<<56)-1))!=0} { error "FPGA MAC clear/readback failed" }
}
proc mac_result {col} {
 lassign [command 7 $col] h0 h1
 set b [expr 0x$h1]
 if {($b>>224)!=0x4D414332 || (($b>>56)&31)!=$col || (($b>>64)&0xffffffff)!=48000} {
  error "FPGA MAC result signature/index/scale mismatch"
 }
 set raw [expr {$b&((1<<56)-1)}]
 if {$raw&(1<<55)} { set raw [expr {$raw-(1<<56)}] }
 return $raw
}
proc scan_mac {} {
 set cfg [mac_config]
 set rows [dict get $cfg rows]; set cols [dict get $cfg cols]
 set inputs [dict get $cfg inputs]; set matrix [dict get $cfg matrix]
 puts "MAC_METADATA $rows $cols [dict get $cfg program]"; flush stdout
 set program_start [dict get $cfg program_start]
 puts "MAC_RESUME $program_start"; flush stdout
 set program_attempts 0; set program_failures 0
 if {[dict get $cfg program]} {
  set array_owned 0
  if {$rows==32 && $cols==32} {
   if {$program_start==1024} {
    set array_owned 1
   } else {
    set first [lindex [lindex $matrix [expr {$program_start/32}]] [expr {$program_start%32}]]
    set uniform 1
    for {set index $program_start} {$index<1024} {incr index} {
     if {[lindex [lindex $matrix [expr {$index/32}]] [expr {$index%32}]]!=$first} { set uniform 0; break }
    }
    if {$uniform} {
     set array_owned 1
     set outcome [scan_array_program [expr {$first ? "set" : "reset"}] $program_start [expr {1024-$program_start}]]
     set program_attempts [dict get $outcome completed]
     set program_failures [dict get $outcome failures]
    }
   }
  }
  if {!$array_owned} {
   for {set r 0} {$r<$rows} {incr r} {
    for {set c 0} {$c<$cols} {incr c} {
     if {$r*$cols+$c<$program_start} continue
     cancel_check
     set op [expr {[lindex [lindex $matrix $r] $c] ? "set" : "reset"}]
     puts "EVENT MAC programming r$r/c$c as [string toupper $op]"; flush stdout
     set outcome [scan_program $r $c $op]
     incr program_attempts
     set met [dict get $outcome target_met]
     set current [dict get $outcome last_current]
     puts "MAC_PROGRAM_RESULT $r $c $op $met [format %.6f $current]"; flush stdout
     if {!$met} {
      incr program_failures
      puts "EVENT MAC programming error r$r/c$c: [string toupper $op] target not confirmed; continuing"; flush stdout
     }
    }
   }
  }
 }
 puts "MAC_PROGRAM_SUMMARY $program_attempts $program_failures"; flush stdout
 scan_profile
 mac_clear
 set active 0
 for {set c 0} {$c<$cols} {incr c} {
  for {set r 0} {$r<$rows} {incr r} {
   set value [lindex $inputs $r]
   if {$value==0} continue
   incr active
   puts "EVENT MAC measuring r$r/c$c input=$value"; flush stdout
   scan_capture $r $c 0 2400 "mac_read__r${r}_c${c}" 1 0 $value
  }
 }
 for {set c 0} {$c<$cols} {incr c} {
  puts "MAC_RESULT $c [mac_result $c] 48000"; flush stdout
 }
 puts "MAC_COMPLETE $rows $cols $active"; flush stdout
}
proc wb_capture {r c op {pwm 0xFF} {match_pre_epoch 0}} {
 global last_wb_shunt bulk_job_tag16 bulk_epoch_open bulk_epoch_row bulk_epoch_col
 cancel_check
 # Use the reference project's permanent runtime firmware programming path
 # documented in api_v1/prerequisites/HARDWARE_RUNTIME_BACKUP.md.
 set mode [expr {$op eq "read" ? 1 : ($op eq "set" ? 3 : 0)}]
 # Match evaluation-board memory_controller_packet().  Phase-P programming
 # supplies a zero-low-nibble coarse PWM byte; legacy callers retain 0xFF.
 if {![string is integer -strict $pwm] || $pwm<0 || $pwm>255} { error "Invalid PWM byte" }
 if {$op ne "read" && (($pwm&15)!=0 || $pwm<0x10 || $pwm>0xF0)} {
  error "Programming PWM must be 0x10..0xF0 with a zero low nibble"
 }
 set value [expr {($mode<<30)|($r<<25)|($c<<20)|(2<<16)|(0xAA<<8)|$pwm}]
 # gui_wb_mode.c's write branch calls issue_program_setup for SET/RESET.
 # It sends the program packet twice without an intervening chip reset.
 # Repeating the read-transport command instead resets between packets and
 # returned zero banks on this PCB. READ uses issue_read_setup_once.
 set programming [expr {$op ne "read"}]
 # Only PWM/preflight callers that just validated a complete matching phase-0
 # bank may reuse its epoch.  Every standalone WB call opens a fresh epoch,
 # even when repeated for the same target (wb_cycle/diagnostics).  This keeps
 # the FPGA's pre-bank gate explicit and prevents an earlier standalone WB
 # transaction from being mistaken for scan pre-evidence.
 if {$match_pre_epoch ni {0 1}} { error "Invalid WB epoch mode" }
 if {$match_pre_epoch &&
     (!$bulk_epoch_open || $bulk_epoch_row!=$r || $bulk_epoch_col!=$c)} {
  error "Matched WB request has no current bulk pre epoch for r$r/c$c"
 }
 set open_epoch [expr {!$match_pre_epoch}]
 set payload [expr {($programming<<57)|($bulk_job_tag16<<41)|
                    ($open_epoch<<40)|(0x80<<32)|$value}]
 if {$match_pre_epoch} { bulk_pre_token_require $r $c }
 cancel_check
 if {$match_pre_epoch} {
  bulk_pre_token_consume matched_wb_commit
 } else {
  bulk_committed_pre_clear
  bulk_pre_token_invalidate standalone_wb_commit
 }
 lassign [command 3 $payload 65000] h0 h1
 set a [expr 0x$h0]
 set b [expr 0x$h1]
 if {(($a>>224)&0xFFFFFFFF)!=0x57424131} {
  error "WB ADC observation signature mismatch"
 }
 set shunt_base_sum [expr {($a>>96)&0xFFFFFF}]
 set shunt_base_count 4096
 set shunt_base_min [expr {($a>>120)&0xFFF}]
 set shunt_base_max [expr {($a>>132)&0xFFF}]
 set shunt_active_min [expr {($a>>144)&0xFFF}]
 set shunt_active_max [expr {($a>>156)&0xFFF}]
 set shunt_pos_max_run [expr {($a>>168)&0xFFFF}]
 set shunt_neg_max_run [expr {($a>>184)&0xFFFF}]
 set shunt_active_count_sat [expr {($a>>200)&0xFF}]
 set shunt_channel [expr {($a>>208)&3}]
 set shunt_base_clip [expr {($a>>210)&1}]
 set shunt_active_clip [expr {($a>>211)&1}]
 set shunt_base_valid [expr {($a>>212)&1}]
 set shunt_threshold [expr {($a>>213)&0xF}]
 set expected_channel [expr {$op eq "reset" ? 1 : ($op eq "set" ? 2 : 0)}]
 if {$shunt_channel!=$expected_channel || !$shunt_base_valid || $shunt_active_count_sat==0} {
  error "WB ADC observation incomplete: channel=$shunt_channel baseline_valid=$shunt_base_valid active_count_sat=$shunt_active_count_sat"
 }
 set shunt_name [lindex {ADC1/R9_READ ADC2/R11_RESET ADC3/R10_SET} $shunt_channel]
 set shunt_base_mean [expr {double($shunt_base_sum)/$shunt_base_count}]
 set shunt_pos_delta [expr {$shunt_active_max-$shunt_base_mean}]
 set shunt_neg_delta [expr {$shunt_base_mean-$shunt_active_min}]
 set shunt_pos_beyond_noise [expr {$shunt_active_max-$shunt_base_max}]
 set shunt_neg_beyond_noise [expr {$shunt_base_min-$shunt_active_min}]
 set shunt_excursion [expr {max(abs($shunt_pos_delta),abs($shunt_neg_delta))}]
 set shunt_nominal_uA [expr {$shunt_excursion/2048.0/(7.0*56.0)*1.0e6}]
 # The on-chip coarse PWM is one 10-MHz clock per high-nibble count: 0x10 is
 # only about one ADC sample and 0xF0 about fifteen. Use half the nominal width
 # (minimum one sample) as the run criterion; a missing 0x10 event is explicitly
 # inconclusive because ADC/pulse phase can miss a 100-ns pulse.
 set shunt_expected_pwm_samples [expr {$pwm>>4}]
 set shunt_run_required [expr {max(1,($shunt_expected_pwm_samples+1)/2)}]
 set shunt_sustained_positive [expr {$shunt_pos_max_run>=$shunt_run_required &&
                                      $shunt_pos_beyond_noise>=4}]
 set shunt_sustained_negative [expr {$shunt_neg_max_run>=$shunt_run_required &&
                                      $shunt_neg_beyond_noise>=4}]
 set shunt_sustained_any [expr {$shunt_sustained_positive || $shunt_sustained_negative}]
 set shunt_sampling_inconclusive_if_missing [expr {$programming && $pwm==0x10}]
 set shunt_adc_resolution_limited [expr {$programming && $shunt_expected_pwm_samples<=2}]
 puts [format "WB_SHUNT r%d/c%d op=%s path=%s channel=%d baseline_sum=%d baseline_count=%d baseline_mean_code=%.6f baseline_min=%d baseline_max=%d active_samples_at_least=%d active_min=%d active_max=%d threshold_code=%d positive_max_run=%d negative_max_run=%d expected_pwm_samples=%d run_required=%d sustained_positive=%d sustained_negative=%d sustained_any=%d adc_resolution_limited=%d sampling_inconclusive_if_missing=%d peak_positive_delta_code=%.6f peak_negative_delta_code=%.6f positive_beyond_baseline_code=%.6f negative_beyond_baseline_code=%.6f peak_excursion_nominal_uA_56ohm_gain7=%.6f peak_advisory=1 baseline_clip=%d active_clip=%d" \
   $r $c $op $shunt_name $shunt_channel $shunt_base_sum $shunt_base_count \
   $shunt_base_mean $shunt_base_min $shunt_base_max $shunt_active_count_sat \
   $shunt_active_min $shunt_active_max $shunt_threshold $shunt_pos_max_run \
   $shunt_neg_max_run $shunt_expected_pwm_samples $shunt_run_required \
   $shunt_sustained_positive \
   $shunt_sustained_negative $shunt_sustained_any \
   $shunt_adc_resolution_limited $shunt_sampling_inconclusive_if_missing \
   $shunt_pos_delta $shunt_neg_delta \
   $shunt_pos_beyond_noise $shunt_neg_beyond_noise $shunt_nominal_uA \
   $shunt_base_clip $shunt_active_clip]
 flush stdout
 set last_wb_shunt [dict create path $shunt_name channel $shunt_channel \
  sustained_positive $shunt_sustained_positive \
  sustained_negative $shunt_sustained_negative sustained_any $shunt_sustained_any \
  baseline_clip $shunt_base_clip active_clip $shunt_active_clip \
  baseline_mean_code $shunt_base_mean active_min $shunt_active_min \
  active_max $shunt_active_max positive_max_run $shunt_pos_max_run \
  negative_max_run $shunt_neg_max_run run_required $shunt_run_required \
  expected_pwm_samples $shunt_expected_pwm_samples \
  adc_resolution_limited $shunt_adc_resolution_limited \
  sampling_inconclusive_if_missing $shunt_sampling_inconclusive_if_missing]
 if {(($b>>40)&0x7FFF)!=0x7FFF || !(($b>>55)&1) || (($b>>64)&0xFFFFFFFF)!=$value} {
  error "Wishbone command echo or readback mask mismatch"
 }
 set packet_issues [expr {$programming ? 2 : 1}]
 puts "EVENT WB_ACK $op r$r/c$c pwm=[format 0x%02X $pwm] firmware_transaction=1 firmware_packet_issues=$packet_issues completed"; flush stdout

 # One input-only VIO refresh retrieves all fifteen already-cached words.  The
 # packed bank is tied to the same job/epoch/target, a full valid mask, and a
 # CRC.  It replaces fifteen command-5 commit/refresh round trips.
 set bulk_tdc [bulk_tdc_snapshot $r $c $open_epoch]
 set words [dict get $bulk_tdc words]
 set first_nonzero_index -1
 set last_nonzero_index -1
 set nonzero_count 0
 set base_h1 [string toupper $h1]
 if {[string length $base_h1]>64} { error "WB result1 wider than 256 bits" }
 set base_h1 "[string repeat 0 [expr {64-[string length $base_h1]}]]$base_h1"
 for {set i 0} {$i<15} {incr i} {
  set word [lindex $words $i]
  if {$word!=0} {
   if {$first_nonzero_index<0} { set first_nonzero_index $i }
   set last_nonzero_index $i
   incr nonzero_count
  }
  # Recreate the deterministic selected-word view formerly returned by op5,
  # while WB_TDC_BULK_RAW above retains the genuine packed transport words.
  set word_view [string replace $base_h1 31 31 [format %X $i]]
  set word_view [string replace $word_view 54 55 [format %02X [expr {0x80+$i}]]]
  set word_view [string replace $word_view 56 63 [format %08X $word]]
  puts "WB_WORD $r $c $op $i [format %08X $value] $h0 $word_view"; flush stdout
 }
 set last_word [lindex $words end]
 set gate_pass [expr {$nonzero_count>0}]
 puts [format "WB_TDC_BULK_GATE job_tag=0x%04X sequence=0x%08X row=%d column=%d op=%s valid_mask=0x7FFF packet_echo=0x%08X nonzero_count=%d crc_pass=1 sequence_pass=1 pass=%d" \
       $bulk_job_tag16 [dict get $bulk_tdc sequence] $r $c $op $value $nonzero_count $gate_pass]
 puts "WB_TDC_RETURN_GATE r$r/c$c op=$op first_nonzero_index=$first_nonzero_index last_nonzero_index=$last_nonzero_index last_word=[format %08X $last_word] nonzero_count=$nonzero_count pass=$gate_pass"; flush stdout
 if {!$gate_pass} {
  error "WB/TDC full-loop gate failed after $op r$r/c$c: all 15 cached return words were zero"
 }
 puts "WB_COMPLETE $r $c $op"; flush stdout
 return $words
}
proc collect_trace {} {
 # The last scan operation remains in FPGA trace RAM. Three measured samples
 # per VIO read; their time stamps count 10-MSPS snapshots from baseline start.
 for {set i 0} {$i<256} {incr i 3} {
  cancel_check
  lassign [command 6 $i] h0 h1
  set b [expr 0x$h1]
  if {($b>>224)!=0x54524332 || (($b>>201)&255)!=$i} { error "ADC trace signature/index mismatch" }
  set n [expr {($b>>192)&511}]
  if {$n<3 || $n>256} { error "ADC trace length invalid: $n" }
  puts "TRACE_CHUNK $i $h1"; flush stdout
  if {$i+3>=$n} { break }
 }
}
proc pwm_profile_check_run {r c set_voltage reset_voltage} {
 global pwm_scan_restored
 # No program packet is emitted.  This only proves that the FPGA accepts the
 # complete DAC0..6 command profiles and that scan readback survives restore.
 set pwm_scan_restored 0
 hold_cpu 1
 clock_profile 10
 phase_p_profile set $set_voltage
 phase_p_profile reset $reset_voltage
 hold_cpu
 clock_profile 2
 scan_profile
 set pwm_scan_restored 1
 scan_capture $r $c 0 2400 profile_check_target
 foreach sentinel {{0 0} {8 0} {16 0} {24 0}} {
  lassign $sentinel sr sc
  scan_capture $sr $sc 0 2400 profile_check_sentinel
 }
 puts "PWM_PROFILE_CHECK_COMPLETE set_v=$set_voltage reset_v=$reset_voltage program_packets=0"
 flush stdout
}
proc pwm_skew_profile_check_run {r c set_voltage wl_voltage} {
 global pwm_scan_restored
 # Read-only rail-profile proof for independent WB-PWM SET rails.  No WB
 # packet is emitted; scan readback after restoration is the terminal gate.
 set pwm_scan_restored 0
 hold_cpu 1
 clock_profile 10
 phase_p_profile set $set_voltage $wl_voltage 1
 hold_cpu
 clock_profile 2
 scan_profile 1
 set pwm_scan_restored 1
 scan_capture $r $c 0 2400 profile_check_target
 foreach sentinel {{0 0} {8 0} {16 0} {24 0}} {
  lassign $sentinel sr sc
  scan_capture $sr $sc 0 2400 profile_check_sentinel
 }
 puts "PWM_SKEW_PROFILE_CHECK_COMPLETE set_v=$set_voltage wl_v=$wl_voltage program_packets=0"
 flush stdout
}
proc scan_current_ua {pair} {
 lassign $pair h0 h1
 set a [expr 0x$h0]
 set b [expr 0x$h1]
 set baseline_count [expr {($a>>64)&0xFFFF}]
 set active_count [expr {($a>>80)&0xFFFF}]
 if {$baseline_count==0 || $active_count==0} { error "Zero scan sample count" }
 set baseline_mean [expr {double(($a>>160)&0xFFFFFFFF)/$baseline_count}]
 set active_mean [expr {double(($b>>64)&0xFFFFFFFF)/$active_count}]
 return [expr {($active_mean-$baseline_mean)/2048.0/(7.0*56.0)*1.0e6}]
}
proc scan_guard_current_ua {pair r c} {
 # Hardware-local validation for irreversible guarded forming decisions.  The
 # host validates the emitted frame again, but that happens too late to stop a
 # pulse.  Keep this contract aligned with decode_scan_frame: SCR2 signature,
 # exact READ packet/address, complete status, deployed sample-count windows,
 # finite 12-bit ADC means, aggregate clip, and the ADC3/R10 outcome clip bit.
 lassign $pair h0 h1
 set a [expr 0x$h0]
 set b [expr 0x$h1]
 set packet [expr {($r<<10)|($c<<5)|$r}]
 if {($b>>224)!=0x53435232} { error "Guarded scan frame signature mismatch" }
 if {(($a>>16)&0xFFFF)!=$packet} { error "Guarded scan frame packet/address mismatch" }
 if {!($a&(1<<9)) || ($a&(1<<8)) || ($a&(1<<10))} {
  error "Guarded scan frame status is not done/idle/fault-free"
 }
 if {(($a>>37)&1)} { error "Guarded scan frame aggregate clip" }
 set baseline_count [expr {($a>>64)&0xFFFF}]
 set active_count [expr {($a>>80)&0xFFFF}]
 if {$baseline_count<128 || $baseline_count>800 ||
     $active_count<11900 || $active_count>12100} {
  error "Guarded scan frame sample count outside deployed range"
 }
 set outcome_baseline_mean 0.0
 set outcome_active_mean 0.0
 for {set channel 0} {$channel<5} {incr channel} {
  set baseline_sum [expr {($a>>(96+32*$channel))&0xFFFFFFFF}]
  set active_sum [expr {($b>>(32*$channel))&0xFFFFFFFF}]
  set baseline_mean [expr {double($baseline_sum)/$baseline_count}]
  set active_mean [expr {double($active_sum)/$active_count}]
  if {$baseline_mean<0.0 || $baseline_mean>4095.0 ||
      $active_mean<0.0 || $active_mean>4095.0} {
   error "Guarded scan frame ADC mean outside 12-bit range"
  }
  if {$channel==2} {
   set outcome_baseline_mean $baseline_mean
   set outcome_active_mean $active_mean
  }
 }
 if {(($a>>41)&1)} { error "Guarded scan frame ADC3/R10 outcome clipped" }
 return [expr {($outcome_active_mean-$outcome_baseline_mean)/2048.0/(7.0*56.0)*1.0e6}]
}
proc sample_median {values} {
 set sorted [lsort -real $values]
 set n [llength $sorted]
 if {$n==0} { error "Cannot take median of an empty list" }
 if {$n&1} { return [lindex $sorted [expr {$n/2}]] }
 return [expr {([lindex $sorted [expr {$n/2-1}]]+[lindex $sorted [expr {$n/2}]])/2.0}]
}
proc phase_p_sentinels {target_row target_col} {
 # Rotate four distant controls with the target so every array cell can be
 # programmed without ever being compared against itself.  The offsets are
 # balanced across the 32x32 array, avoiding a permanently over-read sentinel
 # subset during long experiments.
 return [list \
  [list [expr {($target_row+8)&31}] $target_col] \
  [list [expr {($target_row+16)&31}] $target_col] \
  [list [expr {($target_row+24)&31}] $target_col] \
  [list $target_row [expr {($target_col+16)&31}]]]
}
proc bulk_scan_block {r c phase sentinel_first clocks target_prefix sentinel_label {expect_wb 0}} {
 global bulk_job_tag16 bulk_epoch bulk_epoch_open bulk_epoch_row bulk_epoch_col
 global bulk_last_tdc_low bulk_last_tdc_high bulk_last_tdc_sequence
 if {$phase ni {pre post} || $sentinel_first ni {0 1} ||
     ![string is integer -strict $clocks] || $clocks<20 || $clocks>12000} {
  error "Invalid bulk scan block request"
 }
 set phase_bit [expr {$phase eq "post"}]
 if {$phase eq "post" && !$bulk_epoch_open} {
  error "Bulk post block requested without a current pre epoch"
 }
 set descriptor0 [expr {(1<<27)|($phase_bit<<26)|($sentinel_first<<25)|
                        ($r<<20)|($c<<15)|$clocks}]
 set descriptor1 [expr {(0xB901<<16)|$bulk_job_tag16}]
 set payload [expr {($phase_bit<<57)|($bulk_job_tag16<<41)|
                    ($sentinel_first<<40)|($clocks<<10)|($c<<5)|$r}]
 if {$phase eq "pre"} {
  bulk_pre_token_invalidate new_phase0
  bulk_committed_pre_clear
 } else {
  bulk_pre_token_invalidate phase1_start
 }
 command 14 $payload 30000
 if {$phase eq "pre"} {
  set first_page [bulk_page_read pre 0]
  set second_page [bulk_page_read pre 1]
  set halves [concat [dict get $first_page lanes] \
                     [lrange [dict get $second_page lanes] 0 7]]
 } else {
  set first_page [bulk_page_read post 2]
  set second_page [bulk_page_read post 3]
  set halves [concat [dict get $first_page lanes] \
                     [lrange [dict get $second_page lanes] 0 7]]
 }
 set header_raw [dict get $first_page header_raw]
 set header [dict get $first_page header]
 if {[dict get $second_page header_raw] ne $header_raw} {
  if {$phase eq "pre"} { bulk_pre_token_invalidate page_header_changed }
  error "Bulk $phase page headers changed during one bank snapshot"
 }
 set pairs {}
 set frame_clip_mask 0
 for {set slot 0} {$slot<9} {incr slot} {
  set h0 [lindex $halves [expr {2*$slot}]]
  set h1 [lindex $halves [expr {2*$slot+1}]]
  lappend pairs [list $h0 $h1]
  if {([expr 0x$h0]>>37)&1} { set frame_clip_mask [expr {$frame_clip_mask|(1<<$slot)}] }
  puts "BULK_SCAN_FRAME_RAW phase=$phase slot=$slot h0=$h0 h1=$h1"
 }
 puts "BULK_HEADER_RAW phase=$phase header=$header_raw"
 set actual_crc [bulk_scan_crc $descriptor0 $descriptor1 $pairs]
 set expected_crc [dict get $header "${phase}_crc"]
 set crc_pass [expr {$actual_crc==$expected_crc}]
 puts [format "BULK_SCAN_CRC phase=%s expected=%08X actual=%08X pass=%d" \
       $phase $expected_crc $actual_crc $crc_pass]
 flush stdout

 set errors {}
 if {[dict get $header signature]!=0x42554C4B || [dict get $header version]!=2} {
  lappend errors capability
 }
 if {![dict get $header valid]} { lappend errors header_valid }
 if {[dict get $header job_tag]!=$bulk_job_tag16} { lappend errors job_tag }
 if {[dict get $header row]!=$r || [dict get $header column]!=$c} { lappend errors address }
 if {[dict get $header job_mismatch]} { lappend errors job_mismatch }
  if {[dict get $header wb_protocol_error]} { lappend errors wb_protocol_error }
 if {[dict get $header any_fault]} { lappend errors any_fault }
 if {[dict get $header any_clip]} { lappend errors any_clip }
 set metadata_clip_mask [dict get $header "${phase}_clip"]
 if {$metadata_clip_mask!=$frame_clip_mask} { lappend errors clip_mask_disagrees }
 if {$metadata_clip_mask!=0} { lappend errors clipped_frame }
 if {!$crc_pass} { lappend errors crc }
 set phase_valid [dict get $header "${phase}_valid"]
 set phase_done [dict get $header "${phase}_done"]
 set header_order [dict get $header "${phase}_sentinel_first"]
 if {!$phase_done || $phase_valid!=0x1FF} { lappend errors completeness }
 if {$header_order!=$sentinel_first} { lappend errors order }
 set sequence [dict get $header sequence]
 if {$phase eq "pre"} {
  set expected_sequence [expr {($bulk_epoch+1)&0xFFFFFFFF}]
  if {$sequence!=$expected_sequence} { lappend errors sequence }
   if {[dict get $header wb_echo]} { lappend errors unexpected_wb_echo }
  if {[dict get $header post_done] || [dict get $header wb_done] ||
      [dict get $header post_valid]!=0 || [dict get $header tdc_valid]!=0} {
   lappend errors epoch_not_cleared
  }
  set pre_tdc_low [expr 0x[lindex [dict get $second_page lanes] 8]]
  set pre_tdc_high [expr 0x[lindex [dict get $second_page lanes] 9]]
  if {$pre_tdc_low!=0 || $pre_tdc_high!=0} { lappend errors stale_tdc_payload }
 } else {
  if {$sequence!=$bulk_epoch} { lappend errors sequence }
  if {![dict get $header pre_done] || [dict get $header pre_valid]!=0x1FF} {
   lappend errors pre_association
  }
  if {$expect_wb && (![dict get $header wb_done] || [dict get $header tdc_valid]!=0x7FFF)} {
   lappend errors wb_association
  }
   if {$expect_wb && ![dict get $header wb_echo]} { lappend errors missing_wb_echo }
  set post_tdc_low [lindex [dict get $second_page lanes] 8]
  set post_tdc_high [lindex [dict get $second_page lanes] 9]
  if {$expect_wb && ($bulk_last_tdc_sequence!=$sequence ||
      $post_tdc_low ne $bulk_last_tdc_low || $post_tdc_high ne $bulk_last_tdc_high)} {
   lappend errors tdc_payload_changed
  }
  if {!$expect_wb && ([dict get $header wb_done] || [dict get $header tdc_valid]!=0)} {
   lappend errors unexpected_wb
  }
   if {!$expect_wb && [dict get $header wb_echo]} { lappend errors unexpected_wb_echo }
  if {!$expect_wb && ([expr 0x$post_tdc_low]!=0 || [expr 0x$post_tdc_high]!=0)} {
   lappend errors unexpected_tdc_payload
  }
 }
 if {[llength $errors]} {
  puts [format "BULK_SCAN_BLOCK phase=%s job_tag=0x%04X row=%d column=%d sentinel_first=%d valid_mask=0x%03X metadata_clip_mask=0x%03X frame_clip_mask=0x%03X crc_pass=%d address_pass=%d frame_pass=0 pass=0 errors=%s" \
        $phase $bulk_job_tag16 $r $c $sentinel_first $phase_valid \
        $metadata_clip_mask $frame_clip_mask $crc_pass \
        [expr {[dict get $header row]==$r && [dict get $header column]==$c}] \
        [join $errors ,]]
  flush stdout
  error "Bulk $phase scan header validation failed: [join $errors {, }]"
 }

 # Validate all nine frames before emitting any legacy CELL_RESULT marker.
 set target_pairs [lrange $pairs 0 4]
 set sentinel_pairs [lrange $pairs 5 8]
 set target_currents {}
 foreach pair $target_pairs { lappend target_currents [scan_guard_current_ua $pair $r $c] }
 set sentinels [phase_p_sentinels $r $c]
 set sentinel_currents {}
 for {set i 0} {$i<4} {incr i} {
  lassign [lindex $sentinels $i] sr sc
  lappend sentinel_currents [scan_guard_current_ua [lindex $sentinel_pairs $i] $sr $sc]
 }
 if {$phase eq "pre"} {
  set bulk_epoch $sequence
  set bulk_epoch_open 1
  set bulk_epoch_row $r
  set bulk_epoch_col $c
 }

 if {$sentinel_first} {
  for {set i 0} {$i<4} {incr i} {
   lassign [lindex $sentinels $i] sr sc
   emit_scan $sr $sc $sentinel_label $clocks [lindex $sentinel_pairs $i]
  }
 }
 for {set i 0} {$i<5} {incr i} {
  emit_scan $r $c "${target_prefix}_[expr {$i+1}]" $clocks [lindex $target_pairs $i]
 }
 if {!$sentinel_first} {
  for {set i 0} {$i<4} {incr i} {
   lassign [lindex $sentinels $i] sr sc
   emit_scan $sr $sc $sentinel_label $clocks [lindex $sentinel_pairs $i]
  }
 }
 puts [format "BULK_SCAN_BLOCK phase=%s job_tag=0x%04X row=%d column=%d sentinel_first=%d valid_mask=0x%03X metadata_clip_mask=0x%03X frame_clip_mask=0x%03X crc_pass=1 address_pass=1 frame_pass=1 sequence=0x%08X pass=1" \
       $phase $bulk_job_tag16 $r $c $sentinel_first $phase_valid \
       $metadata_clip_mask $frame_clip_mask $sequence]
 flush stdout
 if {$phase eq "pre"} {
  bulk_pre_token_publish $sequence $bulk_job_tag16 $r $c $expected_crc \
   $header_raw [lrange [dict get $second_page lanes] 0 7] $sentinel_first
 }
 return [dict create target_pairs $target_pairs target_currents $target_currents \
  sentinel_pairs $sentinel_pairs sentinel_currents $sentinel_currents \
  sentinels $sentinels sequence $sequence header $header header_raw $header_raw]
}
proc bulk_tdc_snapshot {r c open_epoch} {
 global bulk_job_tag16 bulk_epoch bulk_epoch_open bulk_epoch_row bulk_epoch_col
 global bulk_last_tdc_low bulk_last_tdc_high bulk_last_tdc_sequence
 global bulk_committed_pre_valid bulk_committed_pre_sequence bulk_committed_pre_job
 global bulk_committed_pre_row bulk_committed_pre_col bulk_committed_pre_crc
 global bulk_committed_pre_tail bulk_committed_pre_order
 # A matched WB leaves page1 selected by the validated pre snapshot, so this
 # is one refresh with no selector commit. Standalone WB selects page1 once.
 if {$open_epoch} {
  set snapshot [bulk_page_read wb 1]
 } else {
  set snapshot [bulk_page_read wb 1 0]
 }
 set header_raw [dict get $snapshot header_raw]
 set header [dict get $snapshot header]
 set wb_page_lanes [dict get $snapshot lanes]
 set low [lindex $wb_page_lanes 8]
 set high [lindex $wb_page_lanes 9]
 set lo [expr 0x$low]
 set hi [expr 0x$high]
 set words {}
 for {set i 0} {$i<8} {incr i} { lappend words [expr {($lo>>(32*$i))&0xFFFFFFFF}] }
 for {set i 0} {$i<7} {incr i} { lappend words [expr {($hi>>(32*$i))&0xFFFFFFFF}] }
 set actual_crc [bulk_tdc_crc $words]
 set expected_crc [dict get $header wb_crc]
 set crc_pass [expr {$actual_crc==$expected_crc}]
 puts "BULK_HEADER_RAW phase=wb header=$header_raw"
 puts [format "WB_TDC_BULK_RAW header=%s low=%s high=%s mask=0x%04X expected_crc=%08X actual_crc=%08X pass=%d" \
       $header_raw $low $high [dict get $header tdc_valid] $expected_crc $actual_crc $crc_pass]
 flush stdout
 set errors {}
 if {[dict get $header signature]!=0x42554C4B || [dict get $header version]!=2} { lappend errors capability }
 if {![dict get $header valid]} { lappend errors header_valid }
 if {[dict get $header job_tag]!=$bulk_job_tag16} { lappend errors job_tag }
 if {[dict get $header row]!=$r || [dict get $header column]!=$c} { lappend errors address }
 if {[dict get $header job_mismatch]} { lappend errors job_mismatch }
 if {[dict get $header wb_protocol_error]} { lappend errors wb_protocol_error }
 if {![dict get $header wb_echo]} { lappend errors wb_echo }
  if {[dict get $header any_fault]} { lappend errors any_fault }
  if {[dict get $header any_clip]} { lappend errors any_clip }
 if {![dict get $header wb_done] || [dict get $header tdc_valid]!=0x7FFF} { lappend errors completeness }
 if {($hi>>224)!=0} { lappend errors high_padding }
 if {!$crc_pass} { lappend errors crc }
 set sequence [dict get $header sequence]
 if {$open_epoch} {
  set expected_sequence [expr {($bulk_epoch+1)&0xFFFFFFFF}]
  if {$sequence!=$expected_sequence} { lappend errors sequence }
  if {[dict get $header pre_done] || [dict get $header post_done] ||
      [dict get $header pre_valid]!=0 || [dict get $header post_valid]!=0} {
   lappend errors epoch_not_cleared
  }
 } else {
  if {!$bulk_epoch_open || $sequence!=$bulk_epoch ||
      $bulk_epoch_row!=$r || $bulk_epoch_col!=$c} {
   lappend errors sequence_or_target
  }
  if {!$bulk_committed_pre_valid ||
      $bulk_committed_pre_sequence!=$sequence ||
      $bulk_committed_pre_job!=$bulk_job_tag16 ||
      $bulk_committed_pre_row!=$r || $bulk_committed_pre_col!=$c ||
      $bulk_committed_pre_crc!=[dict get $header pre_crc] ||
      ![dict get $header pre_done] || [dict get $header pre_valid]!=0x1FF ||
      [dict get $header pre_clip]!=0 ||
      [dict get $header pre_sentinel_first]!=$bulk_committed_pre_order ||
      [lrange $wb_page_lanes 0 7] ne $bulk_committed_pre_tail} {
   lappend errors pre_page1_changed
  }
 }
 if {[llength $errors]} {
  bulk_committed_pre_clear
  error "Packed WB/TDC validation failed: [join $errors {, }]"
 }
 if {$open_epoch} {
  set bulk_epoch $sequence
  set bulk_epoch_open 1
  set bulk_epoch_row $r
  set bulk_epoch_col $c
 }
 set bulk_last_tdc_low $low
 set bulk_last_tdc_high $high
 set bulk_last_tdc_sequence $sequence
 if {!$open_epoch} { bulk_committed_pre_clear }
 return [dict create words $words header $header header_raw $header_raw \
  low $low high $high sequence $sequence crc $actual_crc]
}
proc bulk_profile_check_run {r c} {
 global pwm_scan_restored bulk_job_tag16
 # Exercise both scheduler orders and both evidence banks.  The intervening
 # Wishbone transaction is READ-only and exists solely to validate the packed
 # fifteen-word return path; no SET/RESET packet or scan-program pulse exists
 # anywhere in this procedure.
 set pwm_scan_restored 0
 set pre [bulk_scan_block $r $c pre 1 2400 \
          bulk_profile_target_pre bulk_profile_sentinel_pre 0]
 hold_cpu 1
 clock_profile 10
 wb_profile
 wb_capture $r $c read 0xFF 1
 hold_cpu
 clock_profile 2
 scan_profile
 puts "BULK_PROFILE_SCAN_RESTORED row=$r column=$c"; flush stdout
 set post [bulk_scan_block $r $c post 0 2400 \
           bulk_profile_target_post bulk_profile_sentinel_post 1]
 set pwm_scan_restored 1
 set header [dict get $post header]
 puts [format "BULK_PREFLIGHT_COMPLETE sequence=0x%08X job16=0x%04X pre_valid=0x%03X post_valid=0x%03X tdc_valid=0x%04X wb_read_packets=1 program_packets=0 program_pulses=0" \
       [dict get $post sequence] $bulk_job_tag16 [dict get $header pre_valid] \
       [dict get $header post_valid] [dict get $header tdc_valid]]
 puts "BULK_PROFILE_CHECK_COMPLETE row=$r column=$c target_pre=5 target_post=5 sentinel_pre=4 sentinel_post=4 wb_read_packets=1 program_packets=0 program_pulses=0"
 flush stdout
 return [dict create pre $pre post $post]
}
proc scan_reset_guarded_run {r c} {
 set sentinels [phase_p_sentinels $r $c]
 set sentinel_baselines [dict create]
 foreach sentinel $sentinels {
  lassign $sentinel sr sc
  set current [scan_current_ua [scan_capture $sr $sc 0 2400 scan_reset_sentinel_pre]]
  dict set sentinel_baselines "$sr,$sc" $current
 }
 set result [scan_program $r $c reset]
 # The scan-ramp command returns a restored read capture, but explicitly
 # reassert the read profile before evaluating non-target cells.
 hold_cpu
 clock_profile 2
 scan_profile
 set max_sentinel_drift 0.0
 foreach sentinel $sentinels {
  lassign $sentinel sr sc
  set current [scan_current_ua [scan_capture $sr $sc 0 2400 scan_reset_sentinel_post]]
  set drift [expr {abs($current-[dict get $sentinel_baselines "$sr,$sc"])}]
  if {$drift>$max_sentinel_drift} { set max_sentinel_drift $drift }
 }
 set pass [expr {$max_sentinel_drift<=10.0}]
 puts "SCAN_RESET_SENTINEL_GATE $r $c $max_sentinel_drift $pass"; flush stdout
 if {!$pass} {
  error "Scan RESET sentinel drift gate failed after r$r/c$c: $max_sentinel_drift uA"
 }
 return $result
}
proc guarded_form_voltage_index {voltage} {
 set levels {1.20 1.70 2.00 2.30 2.50 2.70 3.00}
 for {set index 0} {$index<[llength $levels]} {incr index} {
  if {abs(double($voltage)-double([lindex $levels $index]))<=1.0e-9} {
   return $index
  }
 }
 error "Guarded scan SET forming VCC must be one of: $levels V"
}
proc guarded_form_program_step {r c label voltage_index} {
 global bulk_job_tag16
 # Outer op15 carries epoch identity, target, and one reviewed three-bit VCC
 # ladder index. Fabric still binds this to exactly one SET pulse with 2.50-V
 # WL and 1200 clocks, then returns the normal core-op8 feedback frame.
 set payload [expr {($bulk_job_tag16<<41)|($voltage_index<<38)|($c<<5)|$r}]
 cancel_check
 bulk_pre_token_require $r $c
 cancel_check
 bulk_pre_token_consume guarded_op15_commit
 lassign [command 15 $payload] h0 h1
 set pair [list $h0 $h1]
 if {([expr 0x$h0]>>44)&1} {
  puts "EVENT GUARDED_PROGRAM_STEP pulse clipped at r$r/c$c label=$label"; flush stdout
 }
 return [list $pair [ramp_current $pair $r $c]]
}
proc scan_set_form_guarded_run {r c} {
 global setv setwl hold cycles
 # This forming operation is intentionally narrower than the ordinary
 # three-point scan SET ramp. The caller must request one reviewed ladder
 # point, and the controller can issue it only once per evidence epoch.
 set voltage_index [guarded_form_voltage_index $setv]
 if {$hold!=1200 || $cycles!=1 || abs(double($setwl)-2.50)>1.0e-9} {
  error "Guarded scan SET forming requires WL=2.50 V, hold=1200, cycles=1"
 }

 # Execute four sentinel baselines first, then the five target guards.  The
 # five target frames remain the final physical reads before a possible pulse.
 set pre_block [bulk_scan_block $r $c pre 1 2400 \
                scan_set_form_pre scan_set_form_sentinel_pre 0]
 set sentinels [dict get $pre_block sentinels]
 set sentinel_baselines [dict create]
 for {set i 0} {$i<4} {incr i} {
  lassign [lindex $sentinels $i] sr sc
  set current [lindex [dict get $pre_block sentinel_currents] $i]
  dict set sentinel_baselines "$sr,$sc" $current
 }

 # These five fresh reads are the final operations before the possible pulse.
 # Requiring their maximum to be strictly below 20 uS makes every read pass
 # the forming guard, rather than relying on a median alone.
 set pre_conductance_us {}
 foreach current [dict get $pre_block target_currents] {
  lappend pre_conductance_us [expr {2.0*$current}]
 }
 set max_pre_us [lindex [lsort -real $pre_conductance_us] end]
 set issued [expr {$max_pre_us<20.0}]
 set pulse_clipped 0
 if {$issued} {
  # Exactly one compound scan-program step. Do not call scan_program here:
  # its normal SET path can issue three pulses and targets 70 uS.
  lassign [guarded_form_program_step $r $c scan_set_form_pulse $voltage_index] pulse_pair pulse_current
  lassign $pulse_pair pulse_h0 pulse_h1
  set pulse_clipped [expr {([expr 0x$pulse_h0]>>44)&1}]
  emit_scan $r $c scan_set_form_pulse_feedback 1200 $pulse_pair
 }

 # Explicitly restore the 2-MHz scan-read profile before outcome reads.
 hold_cpu
 clock_profile 2
 scan_profile
 puts "SCAN_SET_FORM_PROFILE_RESTORED $r $c"; flush stdout

 set post_block [bulk_scan_block $r $c post 0 2400 \
                 scan_set_form_post scan_set_form_sentinel_post 0]
 set post_conductance_us {}
 foreach current [dict get $post_block target_currents] {
  lappend post_conductance_us [expr {2.0*$current}]
 }
 set max_post_us [lindex [lsort -real $post_conductance_us] end]
 set crossed20 [expr {$max_post_us>=20.0}]

 set max_sentinel_drift 0.0
 for {set i 0} {$i<4} {incr i} {
  lassign [lindex $sentinels $i] sr sc
  set current [lindex [dict get $post_block sentinel_currents] $i]
  set drift [expr {abs($current-[dict get $sentinel_baselines "$sr,$sc"])}]
  if {$drift>$max_sentinel_drift} { set max_sentinel_drift $drift }
 }
 set sentinel_pass [expr {$max_sentinel_drift<=10.0}]
 puts [format "SCAN_SET_FORM_SENTINEL_GATE %d %d %.6f %d" \
       $r $c $max_sentinel_drift $sentinel_pass]
 puts [format "SCAN_SET_FORM_RESULT %d %d %d %d %.3f 2.500 1200 %.6f %.6f %d" \
       $r $c $issued $pulse_clipped $setv $max_pre_us $max_post_us $crossed20]
 flush stdout
 if {!$sentinel_pass} {
  error "Guarded scan SET sentinel drift gate failed after r$r/c$c: $max_sentinel_drift uA"
 }
 return [dict create issued $issued pulse_clipped $pulse_clipped \
         max_pre_uS $max_pre_us max_post_uS $max_post_us crossed20 $crossed20 \
         sentinel_max_abs_drift_uA $max_sentinel_drift sentinel_pass $sentinel_pass]
}
proc pwm_doe_run {r c op pwm voltage post_reads {wl_voltage {}}} {
 global pwm_scan_restored last_wb_shunt
 # Experimental outcomes are exclusively the scan captures below.  The WB
 # words are retained as transport diagnostics and never used for response.
 set pwm_scan_restored 0
 if {$wl_voltage eq {}} { set wl_voltage $voltage }
 foreach value [list $voltage $wl_voltage] {
  if {![string is double -strict $value] || $value<0 || $value>3.0} {
   error "PWM program rail outside 0..3.0 V"
  }
 }
 if {$post_reads!=5} { error "Bulk PWM transport requires exactly five post-write reads" }
 set pre_block [bulk_scan_block $r $c pre 0 2400 \
                "phase_p_pre_${op}" "phase_p_sentinel_pre_${op}" 0]
 set pre_currents [dict get $pre_block target_currents]
 # Sentinel baselines are chip- and run-specific. Capture them immediately
 # before every program transaction so a physical chip swap cannot compare
 # the new device against stale values from a prior chip.
 set sentinel_baselines [dict create]
 set sentinels [dict get $pre_block sentinels]
 for {set i 0} {$i<4} {incr i} {
  lassign [lindex $sentinels $i] sr sc
  set current [lindex [dict get $pre_block sentinel_currents] $i]
  dict set sentinel_baselines "$sr,$sc" $current
 }
  set sorted_pre_currents [lsort -real $pre_currents]
  set pre_max [lindex $sorted_pre_currents end]
  set pre_max_uS [expr {2.0*$pre_max}]
  # SET is prohibited when any immediate pre-read is already at the guard
  # band below the absolute 120-uS ceiling.  This check occurs before clock,
  # DAC, or WB programming changes, so this marker proves that no pulse was
  # issued for the skipped attempt.
  if {$op eq "set" && $pre_max_uS>=119.5} {
   set pre_median [sample_median $pre_currents]
   puts "PWM_PRE_PULSE_SAFETY_STOP $r $c $op [format 0x%02X $pwm] $voltage wl_voltage=$wl_voltage pre_median_uS=[expr {2.0*$pre_median}] pre_max_uS=$pre_max_uS ceiling_uS=120.0 pulse_issued=0"
   puts "PWM_DOE_SKIPPED $r $c $op [format 0x%02X $pwm] $voltage pre_pulse_ceiling"
   flush stdout
   set pwm_scan_restored 1
   return [dict create row $r column $c operation $op pwm $pwm voltage $voltage \
    pre_median_uA $pre_median post_median_uA $pre_median \
    post_min_uA [lindex $sorted_pre_currents 0] post_max_uA $pre_max delta_uA 0.0 \
    target_count 0 response 0 sentinel_max_abs_drift_uA 0.0 \
    shunt_confirmed 0 shunt_clip 0 set_absolute_stop 1 safety_stop 1 skipped 1]
  }
 hold_cpu 1
 clock_profile 10
  phase_p_profile $op $voltage $wl_voltage 1
 wb_capture $r $c $op $pwm 1
 hold_cpu
 clock_profile 2
 scan_profile 1
 set pwm_scan_restored 1
 set post_block [bulk_scan_block $r $c post 0 2400 \
                 "phase_p_post_${op}" "phase_p_sentinel_${op}" 1]
 set post_currents [dict get $post_block target_currents]
 set max_sentinel_drift 0.0
 for {set i 0} {$i<4} {incr i} {
  lassign [lindex $sentinels $i] sr sc
  set current [lindex [dict get $post_block sentinel_currents] $i]
  set drift [expr {abs($current-[dict get $sentinel_baselines "$sr,$sc"])}]
  if {$drift>$max_sentinel_drift} { set max_sentinel_drift $drift }
 }
 set pre_median [sample_median $pre_currents]
 set post_median [sample_median $post_currents]
 set delta [expr {$post_median-$pre_median}]
 set sorted_post_currents [lsort -real $post_currents]
 set post_min [lindex $sorted_post_currents 0]
 set post_max [lindex $sorted_post_currents end]
 set target_count 0
 foreach current $post_currents {
  if {($op eq "set" && $current>=35.0) || ($op eq "reset" && $current<=34.2563054)} { incr target_count }
 }
 set directional [expr {$op eq "set" ? $delta>=5.0 : $delta<=-5.0}]
 set response [expr {$target_count>=4 && $directional}]
 set shunt_clip [expr {[dict get $last_wb_shunt baseline_clip] ||
                        [dict get $last_wb_shunt active_clip]}]
 set shunt_confirmed [expr {[dict get $last_wb_shunt sustained_positive] && !$shunt_clip}]
 # The 4-bit plan intentionally spans through 117 uS.  Stop only if a measured
 # post-read exceeds the documented absolute 120-uS SET ceiling (60 uA at the
 # 0.5-V read bias); the next attempt is separately guarded at 119.5 uS before
 # any clock, DAC, or WB programming change.
 set set_absolute_stop [expr {$op eq "set" && $post_max>60.0}]
 set sentinel_stop [expr {$max_sentinel_drift>10.0}]
 set safety_stop [expr {$shunt_clip || $set_absolute_stop || $sentinel_stop}]
 puts "PWM_METRIC $r $c $op [format 0x%02X $pwm] $voltage wl_voltage=$wl_voltage pre_median_uA=$pre_median post_median_uA=$post_median post_min_uA=$post_min post_max_uA=$post_max delta_uA=$delta target_count=$target_count response=$response sentinel_max_abs_drift_uA=$max_sentinel_drift"
 puts "PWM_SHUNT_GATE $r $c $op path=[dict get $last_wb_shunt path] positive_max_run=[dict get $last_wb_shunt positive_max_run] run_required=[dict get $last_wb_shunt run_required] sustained_positive=[dict get $last_wb_shunt sustained_positive] sustained_negative=[dict get $last_wb_shunt sustained_negative] sampling_inconclusive_if_missing=[dict get $last_wb_shunt sampling_inconclusive_if_missing] clip=$shunt_clip analog_confirmed=$shunt_confirmed set_absolute_stop=$set_absolute_stop sentinel_stop=$sentinel_stop safety_stop=$safety_stop"
 flush stdout
 puts "PWM_SENTINEL_GATE $r $c $op max_abs_drift_uA=$max_sentinel_drift limit_uA=10.0 pass=[expr {!$sentinel_stop}] safety_stop=$safety_stop"
 puts "PWM_DOE_COMPLETE $r $c $op [format 0x%02X $pwm] $voltage 1 2 5 $post_reads scan_debug_only"
 flush stdout
 return [dict create row $r column $c operation $op pwm $pwm voltage $voltage \
  pre_median_uA $pre_median post_median_uA $post_median \
  post_min_uA $post_min post_max_uA $post_max delta_uA $delta \
  target_count $target_count response $response \
  sentinel_max_abs_drift_uA $max_sentinel_drift \
  shunt_confirmed $shunt_confirmed shunt_clip $shunt_clip \
  set_absolute_stop $set_absolute_stop sentinel_stop $sentinel_stop safety_stop $safety_stop]
}
proc pwm_doe_batch_run {} {
 global here jobid
 set path [file join $here runs "$jobid.pwm.txt"]
 if {![file exists $path]} { error "PWM batch plan is missing: $path" }
 set f [open $path r]
 set entries {}
  while {[gets $f line]>=0} {
  set line [string trim $line]
  if {$line eq {} || [string index $line 0] eq "#"} { continue }
  set fields [split $line]
   if {[llength $fields]==7} {
    lassign $fields r c op pwm voltage post_reads arm
    set wl_voltage $voltage
   } elseif {[llength $fields]==8} {
    lassign $fields r c op pwm voltage wl_voltage post_reads arm
   } else {
    close $f; error "Invalid PWM batch line: $line"
   }
  if {![string is integer -strict $r] || $r<0 || $r>31 ||
      ![string is integer -strict $c] || $c<0 || $c>31 ||
      $op ni {set reset} || ![string is integer -strict $pwm] ||
      $pwm<16 || $pwm>240 || ($pwm&15)!=0 ||
       ![string is double -strict $voltage] || $voltage<0 || $voltage>3.0 ||
       ![string is double -strict $wl_voltage] || $wl_voltage<0 || $wl_voltage>3.0 ||
      ![string is integer -strict $post_reads] || $post_reads!=5} {
   close $f; error "Invalid PWM batch entry: $line"
  }
   lappend entries [list $r $c $op $pwm $voltage $wl_voltage $post_reads $arm]
 }
 close $f
 if {[llength $entries]==0} { error "PWM batch plan is empty" }
 puts "PWM_BATCH_START count=[llength $entries] plan=[file tail $path]"; flush stdout
 set completed 0
 foreach entry $entries {
   lassign $entry r c op pwm voltage wl_voltage post_reads arm
  cancel_check
   puts "PWM_BATCH_CELL_START index=[expr {$completed+1}] arm=$arm row=$r column=$c op=$op pwm=[format 0x%02X $pwm] voltage=$voltage wl_voltage=$wl_voltage"; flush stdout
   set result [pwm_doe_run $r $c $op $pwm $voltage $post_reads $wl_voltage]
  incr completed
  puts "PWM_BATCH_PROGRESS completed=$completed total=[llength $entries]"; flush stdout
  if {[dict exists $result skipped] && [dict get $result skipped]} {
   puts "PWM_BATCH_STOP_PRE_PULSE_SAFETY row=$r column=$c op=$op pwm=[format 0x%02X $pwm] voltage=$voltage"; flush stdout
   break
  }
  if {[string match "code*" $arm]} {
   set code [expr {$pwm/16}]
   set post_median_uS [expr {2.0*[dict get $result post_median_uA]}]
   set post_min_uS [expr {2.0*[dict get $result post_min_uA]}]
   set post_max_uS [expr {2.0*[dict get $result post_max_uA]}]
   # In a multi-pulse target ladder, sentinel drift is just as terminal as
   # shunt clipping or the absolute SET ceiling.  Stop inside this Vivado
   # session before the next cumulative pulse can be issued.
   set fourbit_hard_stop [dict get $result safety_stop]
   puts "PWM_4BIT_CODE_RESULT code=$code pwm=[format 0x%02X $pwm] voltage=$voltage post_median_uS=$post_median_uS post_min_uS=$post_min_uS post_max_uS=$post_max_uS hard_stop=$fourbit_hard_stop"
   flush stdout
   if {$fourbit_hard_stop} {
    puts "PWM_BATCH_STOP_4BIT_SAFETY code=$code pwm=[format 0x%02X $pwm] post_max_uS=$post_max_uS"; flush stdout
    break
   }
   continue
  }
  if {[string match "form*" $arm]} {
   set forming_threshold_uS [string range $arm 4 end]
   if {![string is double -strict $forming_threshold_uS] ||
       $forming_threshold_uS<1.0 || $forming_threshold_uS>120.0} {
    error "Invalid forming-threshold arm: $arm"
   }
   set post_median_uS [expr {2.0*[dict get $result post_median_uA]}]
   set post_min_uS [expr {2.0*[dict get $result post_min_uA]}]
   set post_max_uS [expr {2.0*[dict get $result post_max_uA]}]
   set formed [expr {$post_median_uS>=$forming_threshold_uS}]
   set threshold_transient [expr {!$formed && $post_max_uS>=$forming_threshold_uS}]
   set forming_hard_stop [dict get $result safety_stop]
   puts "PWM_FORM_RESULT threshold_uS=$forming_threshold_uS pwm=[format 0x%02X $pwm] voltage=$voltage post_median_uS=$post_median_uS post_min_uS=$post_min_uS post_max_uS=$post_max_uS formed=$formed threshold_transient=$threshold_transient hard_stop=$forming_hard_stop"
   flush stdout
   if {$forming_hard_stop} {
    puts "PWM_BATCH_STOP_FORM_SAFETY threshold_uS=$forming_threshold_uS pwm=[format 0x%02X $pwm] post_max_uS=$post_max_uS"; flush stdout
    break
   }
   if {![dict get $result shunt_confirmed]} {
    puts "PWM_BATCH_STOP_FORM_ANALOG_UNCONFIRMED threshold_uS=$forming_threshold_uS pwm=[format 0x%02X $pwm] post_median_uS=$post_median_uS post_max_uS=$post_max_uS"; flush stdout
    break
   }
   if {$formed} {
    puts "PWM_BATCH_STOP_FORMED threshold_uS=$forming_threshold_uS pwm=[format 0x%02X $pwm] post_median_uS=$post_median_uS"; flush stdout
    break
   }
   if {$threshold_transient} {
    puts "PWM_BATCH_STOP_FORM_TRANSIENT threshold_uS=$forming_threshold_uS pwm=[format 0x%02X $pwm] post_median_uS=$post_median_uS post_max_uS=$post_max_uS"; flush stdout
    break
   }
   continue
  }
  if {[string match "target*" $arm] || [string match "fasttarget*" $arm]} {
   set fast_target [string match "fasttarget*" $arm]
   set target_code [string range $arm [expr {$fast_target ? 10 : 6}] end]
   if {![string is integer -strict $target_code] || $target_code<0 || $target_code>15} {
    error "Invalid 4-bit target arm: $arm"
   }
   set target_low_uS [expr {53.0+4.0*$target_code}]
   set target_high_uS [expr {57.0+4.0*$target_code}]
   set post_median_uS [expr {2.0*[dict get $result post_median_uA]}]
   set post_min_uS [expr {2.0*[dict get $result post_min_uA]}]
   set post_max_uS [expr {2.0*[dict get $result post_max_uA]}]
   set target_landed [expr {$post_median_uS>=$target_low_uS && $post_median_uS<=$target_high_uS}]
   set target_overshot [expr {$post_median_uS>$target_high_uS}]
   # Preserve every PWM safety gate between conditions in the target ladder.
   set fourbit_hard_stop [dict get $result safety_stop]
   puts "PWM_4BIT_TARGET_RESULT target=$target_code pwm=[format 0x%02X $pwm] voltage=$voltage target_low_uS=$target_low_uS target_high_uS=$target_high_uS post_median_uS=$post_median_uS post_min_uS=$post_min_uS post_max_uS=$post_max_uS landed=$target_landed overshot=$target_overshot hard_stop=$fourbit_hard_stop"
   flush stdout
   if {$fourbit_hard_stop} {
    puts "PWM_BATCH_STOP_4BIT_SAFETY target=$target_code pwm=[format 0x%02X $pwm] post_max_uS=$post_max_uS"; flush stdout
    break
   }
   if {$target_landed} {
    puts "PWM_BATCH_STOP_TARGET target=$target_code pwm=[format 0x%02X $pwm] post_median_uS=$post_median_uS"; flush stdout
    break
   }
   if {$target_overshot} {
    puts "PWM_BATCH_STOP_OVERSHOOT target=$target_code pwm=[format 0x%02X $pwm] post_median_uS=$post_median_uS"; flush stdout
    break
   }
   if {$fast_target && ![dict get $result response]} {
    puts "PWM_BATCH_STOP_NONRESPONSIVE target=$target_code pwm=[format 0x%02X $pwm] voltage=$voltage post_median_uS=$post_median_uS"; flush stdout
    break
   }
   continue
  }
  if {[dict get $result response]} {
   puts "PWM_BATCH_STOP_RESPONSE row=$r column=$c op=$op pwm=[format 0x%02X $pwm] voltage=$voltage"; flush stdout
   break
  }
  if {[dict get $result safety_stop]} {
   puts "PWM_BATCH_STOP_SAFETY row=$r column=$c op=$op pwm=[format 0x%02X $pwm] voltage=$voltage"; flush stdout
   break
  }
 }
 puts "PWM_BATCH_COMPLETE completed=$completed"; flush stdout
}
lassign [status_read] h0 h1
set startup_receipt [expr 0x$h1]
if {($startup_receipt>>224)!=0x53435232} {
 # Every local DAC profile intentionally publishes a DPF1 receipt over the
 # ordinary SCR2 result view. A new Vivado session can therefore begin on the
 # exact completed scan-profile receipt left by the previous safe cleanup.
 # Re-establish SCR2 with non-mutating op4 before trusting controller status.
 if {($startup_receipt>>224)!=0x44504631 || ($startup_receipt&3)!=0 ||
     (($startup_receipt>>2)&15)!=12 || (($startup_receipt>>22)&1)!=0 ||
     (($startup_receipt>>23)&1)!=1} {
  error "Full-control FPGA signature mismatch"
 }
 hold_cpu 0
 lassign [status_read] h0 h1
 if {([expr 0x$h1]>>224)!=0x53435232} {
  error "Full-control FPGA signature recovery failed"
 }
 puts "CONTROL_STATUS_RECOVERED_FROM_DPF1"; flush stdout
}
puts "CONTROL_STATUS $h0 $h1"; flush stdout
if {$action eq "check"} { close_i2c_bridge; exit }
set iswb [expr {[string match wb_* $action] || $action in {vbias_diag vbias_tdc_repeat5 vcomp_diag vcomp_tdc_repeat5 biascomp2_diag biascomp2_tdc_repeat5}}]
set outcome [catch {
 hold_cpu $iswb
 if {$action eq "hold"} { puts "HELD"; flush stdout } else {
  if {(([expr 0x$h0]>>36)&1)!=1} { error "ADC not healthy; initialize the bench first" }
  if {$action in {vbias_diag vbias_tdc_repeat5}} {
   clock_profile 10; wb_profile $setv 1.0
  } elseif {$action in {vcomp_diag vcomp_tdc_repeat5}} {
   clock_profile 10; wb_profile 1.6 1.0 $setv
  } elseif {$action in {biascomp2_diag biascomp2_tdc_repeat5}} {
   clock_profile 10; wb_profile 1.6 1.0 0.9 $setv
  } elseif {$iswb} {
   clock_profile 10; wb_profile
  } else {
   clock_profile 2; scan_profile
  }
  switch $action {
   restore_scan { puts "RESTORE_SCAN_COMPLETE"; flush stdout }
   bulk_profile_check { bulk_profile_check_run $row $col }
    pwm_profile_check { pwm_profile_check_run $row $col $setv $resetv }
    pwm_skew_profile_check { pwm_skew_profile_check_run $row $col $setv $setwl }
   pwm_doe_set - pwm_doe_reset {
    set op [string range $action 8 end]
    set pwm [expr {$op eq "set" ? $setwl : $resetwl}]
    set voltage [expr {$op eq "set" ? $setv : $resetv}]
    pwm_doe_run $row $col $op $pwm $voltage $cycles
   }
   pwm_doe_batch { pwm_doe_batch_run }
   read { scan_capture $row $col 0 $hold read }
   read_repeat {
    for {set k 1} {$k<=$cycles} {incr k} {
     scan_capture $row $col 0 $hold "read_repeat_$k"
    }
   }
   scan_reset_guarded { scan_reset_guarded_run $row $col }
   scan_set_form_guarded { scan_set_form_guarded_run $row $col }
   set - reset { scan_program $row $col $action }
   cycle {
    scan_capture $row $col 0 2400 read
    for {set n 0} {$n<$cycles} {incr n} {
     scan_program $row $col set
     set reset_outcome [scan_program $row $col reset]
     if {![dict get $reset_outcome target_met]} {
      puts "EVENT Cycle stopped: RESET ramp exhausted without a confirmed target"; flush stdout
      break
     }
    }
   }
   column { for {set r 0} {$r<32} {incr r} { scan_capture $r $col 0 $hold read } }
   columns {
    set last_column [expr {$col+$cycles-1}]
    if {$last_column>31} { error "Column batch exceeds the 32x32 array" }
    for {set c $col} {$c<=$last_column} {incr c} {
     for {set r 0} {$r<32} {incr r} { scan_capture $r $c 0 $hold read }
    }
   }
   array {
    for {set r 0} {$r<32} {incr r} { for {set c 0} {$c<32} {incr c} { scan_capture $r $c 0 2400 read } }
   }
   array_set { scan_array_program set 0 1024 }
   array_reset { scan_array_program reset 0 1024 }
   diagnostic_reset_setrails {
    scan_capture $row $col 0 2400 before_reset_setrails
    hold_cpu; dac 2 $resetv; dac 3 $resetwl; power_mask 0
    scan_capture $row $col 0 $hold reset_setrails_pulse 1 1
    hold_cpu; dac 2 0.5; dac 3 2.5; power_mask 0
    scan_capture $row $col 0 2400 after_reset_setrails
   }
    scan_mac { scan_mac }
   wb_read - wb_set - wb_reset { wb_capture $row $col [string range $action 3 end] }
   wb_cycle {
    for {set n 0} {$n<$cycles} {incr n} {
     wb_capture $row $col read; wb_capture $row $col set; wb_capture $row $col read
     wb_capture $row $col reset; wb_capture $row $col read
    }
   }
   wb_column { for {set r 0} {$r<32} {incr r} { wb_capture $r $col read } }
   wb_array { for {set r 0} {$r<32} {incr r} { for {set c 0} {$c<32} {incr c} { wb_capture $r $c read } } }
   vbias_diag - vbias_tdc_repeat5 {
    # Read-only VBIAS experiment on the physically isolated evaluation board.
    # The setV argument carries VBIAS; IREF/VCOMP/BIAS_COMP2/DC remain fixed.
    if {$action eq "vbias_diag"} {
     set repeats 3
     set rows [list $row]
    } else {
     set repeats 5
     set rows {0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31}
    }
    set completed_rows 0
    set completed_samples 0
    set numeric_samples 0
    set saturated_samples 0
    foreach r $rows {
     set row_tdc_sum 0
     set row_coarse_sum 0
     set row_fine_sum 0
     set row_completed 0
     set row_numeric 0
     set row_saturated 0
     for {set sample 1} {$sample<=$repeats} {incr sample} {
      set sample_ok 0
      for {set attempt 1} {$attempt<=3} {incr attempt} {
       set rc [catch {wb_capture $r 31 read} words]
       if {$rc} {
        puts "VBIAS_SAMPLE_REJECTED $setv $r $sample $attempt transport [string map {\n { }} $words]"
        flush stdout
        if {$attempt<3} { hold_cpu 1; wb_profile $setv 1.0 }
        continue
       }
       set selected 0
       foreach value $words {
        if {$value!=0 && (($value>>14)&31)==31} { set selected $value; break }
       }
       set coarse [expr {($selected>>8)&63}]
       set fine [expr {$selected&255}]
       set extra [expr {$selected>>19}]
       if {$selected!=0 && $extra==0 && $coarse>0 && $coarse<51 && $fine<=100} {
        set tdc [expr {100*$coarse+99-$fine}]
        puts "VBIAS_SAMPLE_ACCEPTED $setv $r $sample $attempt [format %08X $selected] $coarse $fine $extra $tdc"
        flush stdout
        incr row_tdc_sum $tdc
        incr row_coarse_sum $coarse
        incr row_fine_sum $fine
        incr row_completed
        incr row_numeric
        incr completed_samples
        incr numeric_samples
        set sample_ok 1
        break
       }
       # Coarse count 51 is the TDC ceiling.  The fine field can retain a
       # residual value at this ceiling, so preserve the raw word but censor
       # the sample instead of treating it as a transport/range failure.
       if {$selected!=0 && $extra==0 && $coarse==51 && $fine<=100} {
        puts "VBIAS_SAMPLE_SATURATED $setv $r $sample $attempt [format %08X $selected] $coarse $fine $extra"
        flush stdout
        incr row_completed
        incr row_saturated
        incr completed_samples
        incr saturated_samples
        set sample_ok 1
        break
       }
       puts "VBIAS_SAMPLE_REJECTED $setv $r $sample $attempt range [format %08X $selected] $coarse $fine $extra"
       flush stdout
       if {$attempt<3} { hold_cpu 1; wb_profile $setv 1.0 }
      }
      if {!$sample_ok} { error "VBIAS sample failed voltage=$setv row=$r sample=$sample" }
     }
     if {$row_completed!=$repeats} { error "VBIAS row incomplete voltage=$setv row=$r" }
     incr completed_rows
     if {$row_numeric>0} {
      set mean_c [expr {double($row_coarse_sum)/$row_numeric}]
      set mean_f [expr {double($row_fine_sum)/$row_numeric}]
      set mean_t [expr {double($row_tdc_sum)/$row_numeric}]
     } else {
      set mean_c NaN
      set mean_f NaN
      set mean_t NaN
     }
     puts "VBIAS_ROW_RESULT $setv $r $row_completed $row_numeric $row_saturated $mean_c $mean_f $mean_t"
     puts "VBIAS_PROGRESS $setv $completed_rows/[llength $rows] $completed_samples/[expr {[llength $rows]*$repeats}] numeric=$numeric_samples saturated=$saturated_samples"
     flush stdout
    }
    puts "VBIAS_COMPLETE $setv $completed_rows $completed_samples $numeric_samples $saturated_samples $repeats"
    flush stdout
   }
   vcomp_diag - vcomp_tdc_repeat5 - biascomp2_diag - biascomp2_tdc_repeat5 {
    # Read-only VCOMP or BIAS_COMP2 experiment on the physically isolated board.
    # The setV argument carries the selected sweep voltage; other biases remain fixed.
    if {$action in {biascomp2_diag biascomp2_tdc_repeat5}} {
     set experiment BIASCOMP2
     set voltage_name bias_comp2
    } else {
     set experiment VCOMP
     set voltage_name vcomp
    }
    if {$action in {vcomp_diag biascomp2_diag}} {
     set repeats 3
     set rows [list $row]
    } else {
     set repeats 5
     set rows {0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31}
    }
    set completed_rows 0
    set completed_samples 0
    set numeric_samples 0
    set saturated_samples 0
    foreach r $rows {
     set row_tdc_sum 0
     set row_coarse_sum 0
     set row_fine_sum 0
     set row_completed 0
     set row_numeric 0
     set row_saturated 0
     for {set sample 1} {$sample<=$repeats} {incr sample} {
      set sample_ok 0
      for {set attempt 1} {$attempt<=3} {incr attempt} {
       set rc [catch {wb_capture $r 31 read} words]
       if {$rc} {
        puts "${experiment}_SAMPLE_REJECTED $setv $r $sample $attempt transport [string map {\n { }} $words]"
        flush stdout
        if {$attempt<3} {
         hold_cpu 1
         if {$experiment eq "BIASCOMP2"} { wb_profile 1.6 1.0 0.9 $setv } else { wb_profile 1.6 1.0 $setv }
        }
        continue
       }
       set selected 0
       foreach value $words {
        if {$value!=0 && (($value>>14)&31)==31} { set selected $value; break }
       }
       set coarse [expr {($selected>>8)&63}]
       set fine [expr {$selected&255}]
       set extra [expr {$selected>>19}]
       if {$selected!=0 && $extra==0 && $coarse>0 && $coarse<51 && $fine<=100} {
        set tdc [expr {100*$coarse+99-$fine}]
        puts "${experiment}_SAMPLE_ACCEPTED $setv $r $sample $attempt [format %08X $selected] $coarse $fine $extra $tdc"
        flush stdout
        incr row_tdc_sum $tdc
        incr row_coarse_sum $coarse
        incr row_fine_sum $fine
        incr row_completed
        incr row_numeric
        incr completed_samples
        incr numeric_samples
        set sample_ok 1
        break
       }
       # Coarse count 51 is the TDC ceiling.  Retain its raw fine field but
       # exclude the censored observation from numeric means.
       if {$selected!=0 && $extra==0 && $coarse==51 && $fine<=100} {
        puts "${experiment}_SAMPLE_SATURATED $setv $r $sample $attempt [format %08X $selected] $coarse $fine $extra"
        flush stdout
        incr row_completed
        incr row_saturated
        incr completed_samples
        incr saturated_samples
        set sample_ok 1
        break
       }
       puts "${experiment}_SAMPLE_REJECTED $setv $r $sample $attempt range [format %08X $selected] $coarse $fine $extra"
       flush stdout
       if {$attempt<3} {
        hold_cpu 1
        if {$experiment eq "BIASCOMP2"} { wb_profile 1.6 1.0 0.9 $setv } else { wb_profile 1.6 1.0 $setv }
       }
      }
      if {!$sample_ok} { error "$experiment sample failed $voltage_name=$setv row=$r sample=$sample" }
     }
     if {$row_completed!=$repeats} { error "$experiment row incomplete $voltage_name=$setv row=$r" }
     incr completed_rows
     if {$row_numeric>0} {
      set mean_c [expr {double($row_coarse_sum)/$row_numeric}]
      set mean_f [expr {double($row_fine_sum)/$row_numeric}]
      set mean_t [expr {double($row_tdc_sum)/$row_numeric}]
     } else {
      set mean_c NaN
      set mean_f NaN
      set mean_t NaN
     }
     puts "${experiment}_ROW_RESULT $setv $r $row_completed $row_numeric $row_saturated $mean_c $mean_f $mean_t"
     puts "${experiment}_PROGRESS $setv $completed_rows/[llength $rows] $completed_samples/[expr {[llength $rows]*$repeats}] numeric=$numeric_samples saturated=$saturated_samples"
     flush stdout
    }
    puts "${experiment}_COMPLETE $setv $completed_rows $completed_samples $numeric_samples $saturated_samples $repeats"
    flush stdout
   }
   tdc_conductance_repeat - tdc_conductance_repeat5 {
    # Reproduce the Chip-4 read-only survey on the ZedBoard-connected chip.
    # Use the experiment's explicit bias values during the two scan columns.
    set repeats [expr {$action eq "tdc_conductance_repeat5" ? 5 : 1}]
    dac 9 1.0; dac 10 0.9; dac 11 0.6; dac 12 1.6; dac 13 1.5
    set scan_count 0
    foreach c {30 31} {
     puts "EXPERIMENT_PHASE scan_column_$c"; flush stdout
     for {set r 0} {$r<32} {incr r} {
      scan_capture $r $c 0 $hold read
      incr scan_count
      puts "EXPERIMENT_PROGRESS scan $scan_count/64 r$r/c$c"; flush stdout
     }
    }
    hold_cpu 1
    clock_profile 10
    wb_profile
    set accepted 0
    set accepted_samples 0
    set failed 0
    for {set r 0} {$r<32} {incr r} {
     set row_ok 0
     set row_tdc_sum 0
     set row_coarse_sum 0
     set row_fine_sum 0
     set row_samples 0
     for {set sample 1} {$sample<=$repeats} {incr sample} {
      set sample_ok 0
      for {set attempt 1} {$attempt<=3} {incr attempt} {
       set rc [catch {wb_capture $r 31 read} words]
       if {$rc} {
        if {$repeats==1} {
         puts "WB_REJECTED $r $attempt transport [string map {\n { }} $words]"
        } else {
         puts "WB_SAMPLE_REJECTED $r $sample $attempt transport [string map {\n { }} $words]"
        }
        flush stdout
        if {$attempt<3} { hold_cpu 1; wb_profile }
        continue
       }
       set selected 0
       foreach value $words {
        if {$value!=0 && (($value>>14)&31)==31} { set selected $value; break }
       }
       set coarse [expr {($selected>>8)&63}]
       set fine [expr {$selected&255}]
       set extra [expr {$selected>>19}]
       if {$selected!=0 && $extra==0 && $coarse>0 && $coarse<51 && $fine>=0 && $fine<=100} {
        set tdc [expr {100*$coarse+99-$fine}]
        if {$repeats==1} {
         puts "WB_ACCEPTED $r $attempt [format %08X $selected] $coarse $fine $extra $tdc"
        } else {
         puts "WB_SAMPLE_ACCEPTED $r $sample $attempt [format %08X $selected] $coarse $fine $extra $tdc"
        }
        flush stdout
        incr row_tdc_sum $tdc
        incr row_coarse_sum $coarse
        incr row_fine_sum $fine
        incr row_samples
        incr accepted_samples
        set sample_ok 1
        break
       }
       if {$repeats==1} {
        puts "WB_REJECTED $r $attempt range [format %08X $selected] $coarse $fine $extra"
       } else {
        puts "WB_SAMPLE_REJECTED $r $sample $attempt range [format %08X $selected] $coarse $fine $extra"
       }
       flush stdout
      }
      if {!$sample_ok} { break }
     }
     if {$row_samples==$repeats} {
      set mean_t [expr {double($row_tdc_sum)/$repeats}]
      set mean_c [expr {double($row_coarse_sum)/$repeats}]
      set mean_f [expr {double($row_fine_sum)/$repeats}]
      if {$repeats>1} { puts [format "WB_ROW_AVERAGE %d %d %.6f %.6f %.6f" $r $row_samples $mean_t $mean_c $mean_f]; flush stdout }
      incr accepted
      set row_ok 1
     }
     if {!$row_ok} { incr failed }
     puts "EXPERIMENT_PROGRESS wb [expr {$r+1}]/32 accepted_rows=$accepted failed_rows=$failed accepted_samples=$accepted_samples/[expr {32*$repeats}]"; flush stdout
    }
    if {$repeats==1} {
     puts "EXPERIMENT_COMPLETE $scan_count $accepted $failed"
    } else {
     puts "EXPERIMENT_COMPLETE $scan_count $accepted $failed $accepted_samples $repeats"
    }
    flush stdout
   }
  }
   # A read-only column prescreen is complete once all 32 CELL_RESULT frames
   # have been captured. Exporting the unrelated legacy trace adds hundreds
   # of remote VIO round trips and can turn a valid prescreen into a late
   # transport failure, so skip it for framed column reads as well.
   if {!$iswb && $action ni {restore_scan read_repeat column columns scan_mac array_set array_reset tdc_conductance_repeat tdc_conductance_repeat5 bulk_profile_check pwm_profile_check pwm_skew_profile_check pwm_doe_set pwm_doe_reset pwm_doe_batch scan_set_form_guarded}} { collect_trace }
 }
} message options]
# Leave CPU held and restore the scan read rails even after cancellation/fault.
set restoring 1
set cleanup [catch {
 hold_cpu
 set pwm_already_safe [expr {$action in {bulk_profile_check pwm_profile_check pwm_skew_profile_check pwm_doe_set pwm_doe_reset pwm_doe_batch} && $pwm_scan_restored}]
 if {$action ne "hold" && !$pwm_already_safe} { clock_profile 2; scan_profile }
} cleanup_message]
if {$cleanup} { puts "RESTORE_FAILED $cleanup_message"; flush stdout }
close_i2c_bridge
if {$outcome} { error $message }
if {$cleanup} { error "Read-profile restoration failed: $cleanup_message" }
puts "JOB_COMPLETE $action"; flush stdout
exit
