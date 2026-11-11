proc extract_proc {path name} {
 set fh [open $path r]
 set body ""
 set collecting 0
 while {[gets $fh line]>=0} {
  if {!$collecting && [string match "proc $name *" $line]} {
   set collecting 1
  }
  if {$collecting} {
   append body $line "\n"
   if {[info complete $body]} {
    close $fh
    return $body
   }
  }
 }
 close $fh
 error "procedure $name not found in $path"
}

set here [file dirname [file normalize [info script]]]
set control [file normalize [file join $here .. runtime control.tcl]]
foreach name {cancel_check command guarded_form_program_step} {
 eval [extract_proc $control $name]
}

set cp mock_command_probe
set restoring 0
set stopfile [file normalize [file join $here .cancel_race_test.stop]]
set bulk_job_tag16 0x1234
set get_calls 0
set set_calls 0
set commit_calls 0
set command_calls 0
set token_require_calls 0
set token_consume_calls 0
set create_stop_on_get 0
set create_stop_on_set 0
set staged_value 0000000000000000

proc get_property {property probe} {
 incr ::get_calls
 if {$::create_stop_on_get} {
  set fh [open $::stopfile w]
  puts $fh stop
  close $fh
 }
 return 0000000000000000
}
proc set_property {property value probe} {
 incr ::set_calls
 set ::staged_value $value
 if {$::create_stop_on_set && $::set_calls==1} {
  set fh [open $::stopfile w]
  puts $fh stop
  close $fh
 }
}
proc commit_hw_vio {probe} { incr ::commit_calls }
proc status_read {} { return [list 0000000000008200 0] }
proc ramp_current {pair r c} { return 0.0 }
proc bulk_pre_token_require {r c} { incr ::token_require_calls }
proc bulk_pre_token_consume {reason} { incr ::token_consume_calls }

# A stop already present must be observed before even reading the old toggle.
set fh [open $stopfile w]; puts $fh stop; close $fh
set rc [catch {command 15 0} message]
if {!$rc || $message ne "CANCELLED" || $get_calls!=0 || $set_calls!=0 || $commit_calls!=0} {
 error "pre-read cancellation committed or read VIO: rc=$rc msg=$message get=$get_calls set=$set_calls commit=$commit_calls"
}

# If cancellation arrives while the old toggle is read, the second check must
# prevent OUTPUT_VALUE from being staged and therefore prevent any commit.
file delete -force $stopfile
set get_calls 0; set set_calls 0; set commit_calls 0; set create_stop_on_get 1
set rc [catch {command 15 0} message]
if {!$rc || $message ne "CANCELLED" || $get_calls!=1 || $set_calls!=0 || $commit_calls!=0} {
 error "pre-set cancellation committed VIO: rc=$rc msg=$message get=$get_calls set=$set_calls commit=$commit_calls"
}

# Cancellation can also arrive after staging OUTPUT_VALUE but before commit.
# In that case command must restore the prior toggle/value without committing.
file delete -force $stopfile
set get_calls 0; set set_calls 0; set commit_calls 0
set create_stop_on_get 0; set create_stop_on_set 1; set staged_value BAD
set rc [catch {command 15 0} message]
if {!$rc || $message ne "CANCELLED" || $get_calls!=1 || $set_calls!=2 ||
    $commit_calls!=0 || $staged_value ne "0000000000000000"} {
 error "post-set cancellation left stale toggle: rc=$rc msg=$message get=$get_calls set=$set_calls commit=$commit_calls staged=$staged_value"
}

# Cleanup op4 is deliberately exempt and must derive its toggle from the
# restored old value, then commit even while the stopfile remains present.
set get_calls 0; set set_calls 0; set commit_calls 0; set create_stop_on_set 0
set result [command 4 0]
if {$get_calls!=1 || $set_calls!=1 || $commit_calls!=1 ||
    $staged_value ne "D000000000000000"} {
 error "cleanup op4 did not use restored toggle: get=$get_calls set=$set_calls commit=$commit_calls staged=$staged_value result=$result"
}

# The guarded helper has its own pre-command cancellation boundary. Replace
# command with a counter to prove it never reaches the generic command path.
rename command command_real
proc command {op payload args} { incr ::command_calls; return [list 0 0] }
set create_stop_on_get 0; set create_stop_on_set 0; set command_calls 0
set rc [catch {guarded_form_program_step 7 1 test 0} message]
if {!$rc || $message ne "CANCELLED" || $command_calls!=0 ||
    $token_require_calls!=0 || $token_consume_calls!=0} {
 error "guarded op15 helper crossed cancellation boundary: rc=$rc msg=$message calls=$command_calls require=$token_require_calls consume=$token_consume_calls"
}

file delete -force $stopfile
puts "CONTROL_CANCEL_RACE_PASS"
