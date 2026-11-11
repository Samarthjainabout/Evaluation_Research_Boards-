`timescale 1ns/1ps

// VIO command scheduler around the validated single-step controller.
// Operation 9 executes the complete bounded SET/RESET grid in fabric.
// Operation 11 executes a measured three-point min/mean/max grid.  Every
// inner operation remains a one-cell PROGRAM_STEP (operation 8), including
// rail restoration and a clean post-pulse read.  A passing read is followed
// by ten ordinary confirmation reads before success is reported.
// Operation 10 pages out the compact signed ADC-code trace, 28 points/page.
// Operation 12 owns a contiguous row-major array range in fabric.  It performs
// an initial state read, the selected fast/reliable ramp, and ten stable
// confirmation reads for every cell.  Payload bits 20:19 select zero to three
// additional bounded RESET sweeps when reliable mode is enabled.  Each inner packet still receives the
// mandatory Caravel reset/READY sequence from cell_control_core.
// Operation 13 pages out fourteen compact per-cell array results per request.
// Operation 14 captures one complete nine-read verification block without host
// round trips: five target reads and four rotated sentinels.  Bit 40 selects
// sentinel-first physical order for guarded forming; storage order is always
// target[0:4], sentinel[0:3].  Bit 57 selects pre/post bank and bits 56:41 are
// the host job identifier.  Full 512-bit frames remain available over the
// separate bulk VIO together with the WB/TDC cache and integrity metadata.
module cell_control_engine #(
 parameter RESET_SYS=2400000, POST_CYCLES=128,
 parameter READY_FALLBACK_CYCLES=2000,
 parameter [63:0] WATCHDOG_CYCLES=64'd12000000000,
 parameter WB_ZERO=200000, WB_ONE=800000, WB_HIGH=500000, WB_END=500000,
 parameter CLOCK_MISSING=2000, DAC_SETTLE_CYCLES=200000
)(
 input wire clk, feedback_clk, ready_i, reset_i, uart_rx,
 input wire dac_ready, adc_healthy, input wire [63:0] command,
 input wire sample_valid, input wire [59:0] samples,
 output wire interface_enable,
 output wire reset_low, tm, se, si,
 output wire scan_tristate, si_tristate,
 output wire dac_start, output wire [23:0] dac_frame, input wire dac_done,
 output reg [255:0] result0=0, result1=0,
 output wire [9215:0] bulk_scan_frames,
 output wire [479:0] bulk_tdc_words,
 output reg [255:0] bulk_header=0
);
 localparam WR_READY=0, WR_WAIT=1, WR_STEP=2, WR_CONFIRM=3, WR_ABORT=4,
            WR_ADVANCE=5, WR_PAGE=6, WR_ARRAY_PRE=7,
             WR_ARRAY_CONFIRM=8, WR_ARRAY_PAGE=9,
             WR_BULK_WAIT=10, WR_BULK_CRC=11;
 localparam signed [39:0] SET_THRESHOLD=40'sd1348731;
 localparam signed [39:0] RESET_THRESHOLD=40'sd192676;
 // Column-2 stronger RESET policy: continue the reliable 96-point ramp until
 // the verified read is at or below 52 uS (26 uA at 0.5 V).  The VCC ladder
 // still starts at 2.30 V and remains hard-capped at 3.00 V; this lower stop
 // threshold causes responsive cells to consume more of the established
 // voltage ramp instead of stopping at the former 68.5126-uS separator.
 // command 11 retains the strict 5-uA diagnostic target.
 localparam signed [39:0] HRS_THRESHOLD=40'sd1001910;

 // Guarded forming accepts only this reviewed, monotonically increasing
 // VCC_SET ladder.  Index 7 is deliberately invalid.  WL_SET and duration
 // remain fixed in fabric, so the host cannot synthesize an arbitrary pulse.
 function [15:0] guarded_form_vcc_code;
  input [2:0] index;
  begin
   case(index)
    3'd0: guarded_form_vcc_code=16'd15729; // 1.20 V
    3'd1: guarded_form_vcc_code=16'd22282; // 1.70 V
    3'd2: guarded_form_vcc_code=16'd26214; // 2.00 V
    3'd3: guarded_form_vcc_code=16'd30147; // 2.30 V
    3'd4: guarded_form_vcc_code=16'd32768; // 2.50 V
    3'd5: guarded_form_vcc_code=16'd35389; // 2.70 V
    3'd6: guarded_form_vcc_code=16'd39321; // 3.00 V hard cap
    default: guarded_form_vcc_code=16'd0;
   endcase
  end
 endfunction

 reg [3:0] wrapper_state=WR_READY;
 reg host_seen=0, have_result=0, ramp_result=0, ramp_view=0;
 reg array_active=0, array_result=0, array_view=0;
 reg [63:0] inner_command=0;
 reg [255:0] saved0=0, saved1=0;
 reg ramp_set=0, ramp_fast=0, ramp_met=0, ramp_clip_seen=0;
 reg [4:0] ramp_row=0, ramp_col=0;
 reg [14:0] ramp_hold=2400, array_base_hold=2400;
 reg [15:0] ramp_max_vcc=0, ramp_max_wl=0;
 reg [6:0] ramp_index=0;
 reg [7:0] ramp_pulses=0;
 reg [3:0] ramp_confirm_count=0;
 reg signed [15:0] ramp_current_code[0:95];
 reg [6:0] ramp_read_addr=0;
 reg [4:0] ramp_page_cursor=0;
 reg signed [15:0] ramp_read_data=0;
 reg ramp_page_primed=0;
 reg array_set=0, array_reliable=0;
 reg [9:0] array_start=0, array_end=0, array_index=0;
 reg [10:0] array_remaining=0;
 reg [10:0] array_completed=0, array_failures=0;
 reg [18:0] array_total_pulses=0;
 reg signed [15:0] array_last_current=0;
 reg [9:0] array_last_pulses=0, array_retry_pulses=0;
 reg [1:0] array_retry=0, array_retry_limit=0;
 reg array_last_met=0, array_last_clip=0;
 // One packed synchronous store maps cleanly to block RAM and keeps paging
 // logic small: {reserved[3:0], clip, met, pulses[9:0], current[15:0]}.
 reg [31:0] array_result_memory[0:1023];
 reg array_write_enable=0;
 reg [9:0] array_write_addr=0;
 reg [31:0] array_write_data=0;
 reg [9:0] array_read_addr=0;
 reg [3:0] array_page_cursor=0;
 reg [31:0] array_read_data=0;
 reg array_page_primed=0;
 reg [511:0] bulk_frame[0:17];
 reg [3:0] bulk_exec_index=0;
 reg [3:0] bulk_crc_frame=0;
 reg [4:0] bulk_crc_word=0;
 reg [1:0] bulk_crc_prefix=0;
 reg bulk_phase=0, bulk_sentinel_first=0;
 reg bulk_pre_order=0, bulk_post_order=0;
 reg [4:0] bulk_row=0, bulk_col=0;
 reg [14:0] bulk_hold=2400;
 reg [15:0] bulk_job=0;
 reg [31:0] bulk_sequence=0;
 reg [8:0] bulk_pre_valid=0, bulk_post_valid=0;
 reg [8:0] bulk_pre_clip=0, bulk_post_clip=0;
 reg [31:0] bulk_pre_crc=0, bulk_post_crc=0;
 reg [31:0] bulk_crc_work=32'hFFFFFFFF;
 reg bulk_any_fault=0, bulk_job_mismatch=0, bulk_epoch_valid=0;
 reg bulk_pre_done=0, bulk_wb_done=0, bulk_post_done=0;
 reg [15:0] bulk_wb_job=0;
 reg [14:0] bulk_wb_valid=0;
 reg [31:0] bulk_wb_crc=0;
 reg bulk_wb_echo=0, bulk_wb_protocol_error=0;
reg bulk_wb_started=0, bulk_post_started=0;
reg bulk_scan_program_started=0, bulk_scan_program_done=0;
reg bulk_scan_program_inflight=0;
 wire [255:0] inner_result0, inner_result1;
 wire [479:0] inner_wb_words;
 wire [14:0] inner_wb_valid;
 wire [31:0] inner_wb_crc;
 wire inner_interface_enable;
 wire inner_complete=inner_result0[15]==inner_command[63] &&
                     inner_result0[9] && !inner_result0[8];
wire inner_wb_echo_match=inner_result1[55] &&
                         inner_result1[95:64]==inner_command[31:0];

 cell_control_core #(
  .RESET_SYS(RESET_SYS),.POST_CYCLES(POST_CYCLES),.WATCHDOG_CYCLES(WATCHDOG_CYCLES),
  .READY_FALLBACK_CYCLES(READY_FALLBACK_CYCLES),
  .WB_ZERO(WB_ZERO),.WB_ONE(WB_ONE),.WB_HIGH(WB_HIGH),.WB_END(WB_END),
  .CLOCK_MISSING(CLOCK_MISSING),.DAC_SETTLE_CYCLES(DAC_SETTLE_CYCLES)
 ) core (
  .clk(clk),.feedback_clk(feedback_clk),.ready_i(ready_i),.reset_i(reset_i),.uart_rx(uart_rx),
  .dac_ready(dac_ready),.adc_healthy(adc_healthy),.command(inner_command),
  .sample_valid(sample_valid),.samples(samples),.interface_enable(inner_interface_enable),
  .reset_low(reset_low),.tm(tm),.se(se),.si(si),.scan_tristate(scan_tristate),
  .si_tristate(si_tristate),.dac_start(dac_start),.dac_frame(dac_frame),.dac_done(dac_done),
  .result0(inner_result0),.result1(inner_result1),
  .wb_words_flat(inner_wb_words),.wb_words_valid(inner_wb_valid),
  .wb_words_crc32(inner_wb_crc));
 assign interface_enable=inner_interface_enable;
 assign bulk_tdc_words=inner_wb_words;

 genvar bulk_flat_index;
 generate
  for(bulk_flat_index=0;bulk_flat_index<18;bulk_flat_index=bulk_flat_index+1) begin: BULK_FLATTEN
   assign bulk_scan_frames[512*bulk_flat_index+:512]=bulk_frame[bulk_flat_index];
  end
 endgenerate

 // CRC-32/ISO-HDLC: reflected polynomial 0xEDB88320, init/xorout FFFFFFFF.
 // Each 32-bit word is consumed bit 0 first. Scan blocks use descriptor0,
 // descriptor1, then each canonical frame's result0 words 0..7 followed by
 // result1 words 0..7. WB uses cache words 0..14 in cell_control_core.
 function [31:0] crc32_word;
  input [31:0] crc_in;
  input [31:0] data;
  reg [31:0] c;
  integer crc_bit;
  begin
   c=crc_in;
   for(crc_bit=0;crc_bit<32;crc_bit=crc_bit+1)
    c=(c[0]^data[crc_bit]) ? ((c>>1)^32'hEDB88320) : (c>>1);
   crc32_word=c;
  end
 endfunction

 wire [4:0] bulk_store_slot = bulk_sentinel_first ?
        (bulk_exec_index<4 ? bulk_exec_index+5 : bulk_exec_index-4) :
        bulk_exec_index;
 wire [511:0] bulk_crc_selected_frame=
        bulk_frame[(bulk_phase ? 9 : 0)+bulk_crc_frame];
 wire [31:0] bulk_crc_descriptor0={5'd1,bulk_phase,bulk_sentinel_first,
                                         bulk_row,bulk_col,bulk_hold};
 wire [31:0] bulk_crc_descriptor1={16'hB901,bulk_job};
 wire [31:0] bulk_crc_frame_word = bulk_crc_word<8 ?
        bulk_crc_selected_frame[32*bulk_crc_word+:32] :
        bulk_crc_selected_frame[256+32*(bulk_crc_word-8)+:32];
 wire [31:0] bulk_crc_input_word = bulk_crc_prefix==0 ? bulk_crc_descriptor0 :
        (bulk_crc_prefix==1 ? bulk_crc_descriptor1 : bulk_crc_frame_word);
 wire [31:0] bulk_crc_next=crc32_word(bulk_crc_work,bulk_crc_input_word);

 wire bulk_scan_busy=(wrapper_state==WR_BULK_WAIT || wrapper_state==WR_BULK_CRC);
wire bulk_evidence_busy=bulk_scan_busy || wrapper_state==WR_WAIT ||
                        wrapper_state==WR_ABORT;
 wire bulk_any_clip=|bulk_pre_clip || |bulk_post_clip;
wire bulk_clean_pre=bulk_epoch_valid && bulk_pre_done &&
                    bulk_pre_valid==9'h1FF && !bulk_any_fault &&
                    !bulk_job_mismatch && !(|bulk_pre_clip);
wire bulk_untracked_mutator=command[61:58]==1 || command[61:58]==8 || command[61:58]==9 ||
                            command[61:58]==11 || command[61:58]==12;
 always @* begin
  bulk_header=0;
  bulk_header[255:224]=32'h42554C4B; // "BULK"
  bulk_header[223:216]=8'd2;
  bulk_header[215:208]={bulk_wb_protocol_error,bulk_wb_echo,bulk_job_mismatch,
                        bulk_any_clip,bulk_any_fault,bulk_post_done,
                        bulk_wb_done,bulk_pre_done};
  bulk_header[207:176]=bulk_sequence;
  bulk_header[175:160]=bulk_job;
  bulk_header[159:128]=bulk_pre_crc;
  bulk_header[127:96]=bulk_wb_crc;
  bulk_header[95:64]=bulk_post_crc;
  bulk_header[63:55]=bulk_pre_valid;
  bulk_header[54:46]=bulk_post_valid;
  bulk_header[45:31]=bulk_wb_valid;
  bulk_header[30:22]=bulk_pre_clip;
  bulk_header[21:13]=bulk_post_clip;
  bulk_header[12]=bulk_pre_order;
  bulk_header[11]=bulk_post_order;
  bulk_header[10:6]=bulk_row;
  bulk_header[5:1]=bulk_col;
  bulk_header[0]=bulk_epoch_valid && !bulk_evidence_busy;
 end

 // Precomputed round(mV*65536/5000) codes keep the ramp scheduler out of
 // the 200 MHz critical path; there is no fabric divider or multiplier.
 function [15:0] ramp_vcc_code;
  input set_operation, fast_operation; input [6:0] index;
  begin
   if(fast_operation) begin
    if(set_operation) ramp_vcc_code=16'd30147; // SET: 2.30 V for all 3 points
    else case(index)
     0:ramp_vcc_code=16'd30147; // RESET minimum: 2.30 V
     1:ramp_vcc_code=16'd36700; // RESET measured mean: 2.80 V
     default:ramp_vcc_code=16'd39321; // RESET maximum: <=3.00 V hard cap
    endcase
   end else if(set_operation) ramp_vcc_code=index<32 ? 16'd30147 :
                                      (index<64 ? 16'd31457 : 16'd32768);
   else ramp_vcc_code=index<32 ? 16'd30147 :
                                // Match the host's hard 3.000-V floor cap.
                                (index<64 ? 16'd35389 : 16'd39321);
  end
 endfunction
 function [15:0] ramp_wl_code;
  input set_operation, fast_operation; input [6:0] index;
  begin
   if(fast_operation) begin
    if(set_operation) case(index)
     0:ramp_wl_code=16'd5767;   // 0.44 V measured minimum
     1:ramp_wl_code=16'd14287;  // 1.09 V measured mean
     default:ramp_wl_code=16'd22938; // 1.75 V measured maximum
    endcase
    else case(index)
     0:ramp_wl_code=16'd12321;  // 0.94 V response minimum
     1:ramp_wl_code=16'd23986;  // 1.83 V response mean
     default:ramp_wl_code=16'd37749; // 2.88 V configured maximum
    endcase
   end else if(set_operation) case(index[4:0])
    0:ramp_wl_code=5767; 1:ramp_wl_code=6554; 2:ramp_wl_code=7340; 3:ramp_wl_code=8258;
    4:ramp_wl_code=9044; 5:ramp_wl_code=9830; 6:ramp_wl_code=10617; 7:ramp_wl_code=11403;
    8:ramp_wl_code=12321; 9:ramp_wl_code=13107; 10:ramp_wl_code=13894; 11:ramp_wl_code=14680;
    12:ramp_wl_code=15598; 13:ramp_wl_code=16384; 14:ramp_wl_code=17170; 15:ramp_wl_code=17957;
    16:ramp_wl_code=18743; 17:ramp_wl_code=19661; 18:ramp_wl_code=20447; 19:ramp_wl_code=21234;
    20:ramp_wl_code=22020; 21:ramp_wl_code=22938; 22:ramp_wl_code=23724; 23:ramp_wl_code=24510;
    24:ramp_wl_code=25297; 25:ramp_wl_code=26083; 26:ramp_wl_code=27001; 27:ramp_wl_code=27787;
    28:ramp_wl_code=28574; 29:ramp_wl_code=29360; 30:ramp_wl_code=30278; default:ramp_wl_code=31064;
   endcase
   else case(index[4:0])
    0:ramp_wl_code=12321; 1:ramp_wl_code=13238; 2:ramp_wl_code=14025; 3:ramp_wl_code=14811;
    4:ramp_wl_code=15598; 5:ramp_wl_code=16515; 6:ramp_wl_code=17302; 7:ramp_wl_code=18088;
    8:ramp_wl_code=18874; 9:ramp_wl_code=19792; 10:ramp_wl_code=20578; 11:ramp_wl_code=21365;
    12:ramp_wl_code=22151; 13:ramp_wl_code=23069; 14:ramp_wl_code=23855; 15:ramp_wl_code=24642;
    16:ramp_wl_code=25428; 17:ramp_wl_code=26345; 18:ramp_wl_code=27132; 19:ramp_wl_code=27918;
    20:ramp_wl_code=28705; 21:ramp_wl_code=29622; 22:ramp_wl_code=30409; 23:ramp_wl_code=31195;
    24:ramp_wl_code=31982; 25:ramp_wl_code=32768; 26:ramp_wl_code=33686; 27:ramp_wl_code=34472;
    28:ramp_wl_code=35258; 29:ramp_wl_code=36045; 30:ramp_wl_code=36962; default:ramp_wl_code=37749;
   endcase
  end
 endfunction

 wire [33:0] ramp_active_times4={inner_result1[95:64],2'b00};
 wire [38:0] ramp_base_times75=inner_result0[191:160]*7'd75;
 wire signed [39:0] ramp_delta=$signed({6'd0,ramp_active_times4})-
                                  $signed({1'b0,ramp_base_times75});
 wire ramp_pass=ramp_set ? ramp_delta>=SET_THRESHOLD :
                 ramp_delta<=(ramp_fast ? RESET_THRESHOLD : HRS_THRESHOLD);
 wire array_initial_pass=array_set ? ramp_delta>=SET_THRESHOLD : ramp_delta<=HRS_THRESHOLD;
 wire signed [15:0] ramp_current_compact=ramp_delta>>>14;
 wire [10:0] array_command_count={1'b0,command[45:36]}+11'd1;
 wire [10:0] array_command_end={1'b0,command[55:46]}+
                               array_command_count-11'd1;

 task issue_step;
  input set_operation; input [6:0] index;
  input [14:0] pulse_hold; input [4:0] row, col;
  begin
   inner_command<={~inner_command[63],1'b1,4'd8,set_operation,
                   ramp_vcc_code(set_operation,ramp_fast,index),
                   ramp_wl_code(set_operation,ramp_fast,index),
                   pulse_hold,col,row};
  end
 endtask
 task issue_read;
  input [4:0] row, col;
  begin inner_command<={~inner_command[63],1'b1,4'd1,1'b0,32'd0,15'd2400,col,row}; end
 endtask
 task issue_bulk_read;
  input [3:0] exec_index;
  input sentinel_first;
  input [4:0] row, col;
  input [14:0] read_hold;
  reg is_target;
  reg [2:0] sentinel_index;
  reg [4:0] read_row, read_col;
  begin
   is_target=sentinel_first ? exec_index>=4 : exec_index<5;
   sentinel_index=sentinel_first ? exec_index[2:0] : exec_index-5;
   read_row=row; read_col=col;
   if(!is_target) begin
    case(sentinel_index)
     0: read_row=row+5'd8;
     1: read_row=row+5'd16;
     2: read_row=row+5'd24;
     default: read_col=col+5'd16;
    endcase
   end
   // Hard-coded READ operation. Operation 14 has no path to a program bit.
   inner_command<={~inner_command[63],1'b1,4'd1,1'b0,32'd0,
                   read_hold,read_col,read_row};
  end
 endtask

 integer bulk_init_index;
 initial begin
  for(bulk_init_index=0;bulk_init_index<18;bulk_init_index=bulk_init_index+1)
   bulk_frame[bulk_init_index]=0;
 end
 task finish_array_cell;
  input cell_met; input signed [15:0] current_code; input [9:0] pulse_count;
  reg [9:0] next_index;
  begin
   array_write_enable<=1; array_write_addr<=array_index;
   array_write_data<={4'd0,ramp_clip_seen,cell_met,pulse_count,current_code};
   array_completed<=array_completed+1'b1;
   if(!cell_met) array_failures<=array_failures+1'b1;
   array_total_pulses<=array_total_pulses+pulse_count;
   array_last_current<=current_code; array_last_pulses<=pulse_count;
   array_last_met<=cell_met; array_last_clip<=ramp_clip_seen;
   if(array_remaining==1) begin
    array_remaining<=0; array_active<=0; array_result<=1;
    have_result<=1; saved0<=0; saved1<=0; wrapper_state<=WR_READY;
   end else begin
    next_index=array_index+1'b1;
    array_index<=next_index; array_remaining<=array_remaining-1'b1;
    ramp_row<=next_index[9:5]; ramp_col<=next_index[4:0];
    ramp_index<=0; ramp_pulses<=0; ramp_confirm_count<=0;
    ramp_met<=0; ramp_clip_seen<=0; array_retry<=0; array_retry_pulses<=0;
    ramp_hold<=array_base_hold;
    issue_read(next_index[9:5],next_index[4:0]);
    wrapper_state<=WR_ARRAY_PRE;
   end
  end
 endtask
 task complete_array_sweep;
  input cell_met; input signed [15:0] current_code; input [7:0] sweep_pulses;
  reg [9:0] total_pulses;
  reg [9:0] next_index;
  begin
   total_pulses=array_retry_pulses+sweep_pulses;
   if(!cell_met && !array_set && array_reliable && array_retry<array_retry_limit) begin
    array_retry<=array_retry+1'b1; array_retry_pulses<=total_pulses;
    ramp_index<=0; ramp_pulses<=0; ramp_confirm_count<=0;
    // Escalate width only after a complete safe-voltage sweep fails.  With
    // the default 2400 clocks: 1.2 ms, 2.4 ms, then 6.0 ms at 2 MHz.
    ramp_hold<=array_retry==0 ?
               (array_base_hold>6000 ? 15'd12000 : array_base_hold<<1) :
               15'd12000;
    ramp_met<=0; wrapper_state<=WR_ADVANCE;
   end else begin
    array_write_enable<=1; array_write_addr<=array_index;
    array_write_data<={4'd0,ramp_clip_seen,cell_met,total_pulses,current_code};
    array_completed<=array_completed+1'b1;
    if(!cell_met) array_failures<=array_failures+1'b1;
    array_total_pulses<=array_total_pulses+total_pulses;
    array_last_current<=current_code; array_last_pulses<=total_pulses;
    array_last_met<=cell_met; array_last_clip<=ramp_clip_seen;
    if(array_remaining==1) begin
     array_remaining<=0; array_active<=0; array_result<=1;
     have_result<=1; saved0<=0; saved1<=0; wrapper_state<=WR_READY;
    end else begin
     next_index=array_index+1'b1;
     array_index<=next_index; array_remaining<=array_remaining-1'b1;
     ramp_row<=next_index[9:5]; ramp_col<=next_index[4:0];
     ramp_index<=0; ramp_pulses<=0; ramp_confirm_count<=0;
     ramp_met<=0; ramp_clip_seen<=0; array_retry<=0; array_retry_pulses<=0;
     ramp_hold<=array_base_hold;
     issue_read(next_index[9:5],next_index[4:0]);
     wrapper_state<=WR_ARRAY_PRE;
    end
   end
  end
 endtask

 always @* begin
  result0=have_result ? saved0 : inner_result0;
  result1=have_result ? saved1 : inner_result1;
  result0[15]=host_seen;
  if(wrapper_state==WR_READY) begin result0[8]=0; result0[9]=have_result; end
  else begin result0[8]=1; result0[9]=0; end
  if(ramp_result && have_result && !ramp_view) begin
   result1[223:216]=ramp_pulses;
   result1[215]=ramp_met;
   result1[214]=ramp_clip_seen;
   result1[213:207]=ramp_index;
   result1[206:203]=ramp_confirm_count;
  end
  if((array_active || array_result) && !array_view) begin
   result1=0;
   result1[255:224]=array_active ? 32'h41525032 : 32'h41525232; // ARP2 / ARR2
   result1[223:213]=array_completed;
   result1[212:202]=array_failures;
   result1[201:183]=array_total_pulses;
   result1[182:173]=array_index;
   result1[172]=array_set; result1[171]=array_reliable;
   result1[170:161]=array_start;
   result1[160:151]=array_end;
   result1[150:135]=array_last_current;
   result1[134:125]=array_last_pulses;
   result1[124]=array_last_met; result1[123]=array_last_clip;
   result1[122:121]=array_retry; result1[120:119]=array_retry_limit;
  end
 end

 // A single registered write port plus synchronous read port infers one
 // 1024x32 block RAM instead of 32K individual result flip-flops.
 always @(posedge clk) begin
  if(array_write_enable) array_result_memory[array_write_addr]<=array_write_data;
  array_read_data<=array_result_memory[array_read_addr];
 end

 always @(posedge clk) begin
  array_write_enable<=0;
  // One synchronous trace-memory read per cycle.  A 28-cycle page transfer
  // replaces 28 parallel 96:1 muxes and keeps the 200 MHz path short.
  if(ramp_read_addr<96) ramp_read_data<=ramp_current_code[ramp_read_addr];
  else ramp_read_data<=0;
  if(!command[62]) begin
   // Dropping control ownership invalidates the provenance of every open
   // evidence epoch, even when a clean pre bank had already completed.
   if(bulk_epoch_valid) bulk_any_fault<=1;
   if(bulk_scan_busy) begin
    if(bulk_phase) begin bulk_post_valid<=0; bulk_post_done<=0; end
    else begin bulk_pre_valid<=0; bulk_pre_done<=0; end
    bulk_any_fault<=1;
   end
   if(wrapper_state==WR_WAIT && inner_command[61:58]==3) begin
    bulk_wb_done<=0; bulk_wb_valid<=0; bulk_wb_crc<=0;
    bulk_wb_echo<=0; bulk_wb_protocol_error<=0; bulk_any_fault<=1;
   end
   if(bulk_scan_program_inflight) begin
    bulk_scan_program_inflight<=0; bulk_scan_program_done<=0;
   end
   wrapper_state<=WR_READY; host_seen<=command[63]; inner_command<=0;
   have_result<=0; ramp_result<=0; ramp_view<=0;
   array_active<=0; array_result<=0; array_view<=0;
  end else if(wrapper_state!=WR_READY && command[63]!=host_seen &&
              command[61:58]==4) begin
   // Abort has global priority over every active wrapper state.  In
   // particular WR_WAIT may contain a WB SET/RESET, and WR_ADVANCE would
   // otherwise launch another program step before seeing host cleanup.
   if(bulk_epoch_valid) bulk_any_fault<=1;
   if(wrapper_state==WR_BULK_WAIT || wrapper_state==WR_BULK_CRC) begin
    if(bulk_phase) begin bulk_post_valid<=0; bulk_post_done<=0; end
    else begin bulk_pre_valid<=0; bulk_pre_done<=0; end
    bulk_any_fault<=1;
   end
   if(wrapper_state==WR_WAIT && inner_command[61:58]==3) begin
    bulk_wb_done<=0; bulk_wb_valid<=0; bulk_wb_crc<=0;
    bulk_wb_echo<=0; bulk_wb_protocol_error<=0; bulk_any_fault<=1;
   end
   if(bulk_scan_program_inflight) begin
    bulk_scan_program_inflight<=0; bulk_scan_program_done<=0;
   end
   host_seen<=command[63]; have_result<=0; ramp_result<=0; ramp_view<=0;
   array_active<=0; array_result<=0; array_view<=0;
   inner_command<={~inner_command[63],command[62:0]}; wrapper_state<=WR_ABORT;
  end else case(wrapper_state)
   WR_READY: if(command[63]!=host_seen) begin
    host_seen<=command[63]; ramp_view<=0; array_view<=0;
    // Legacy programming-capable commands remain available, but using one
    // while an evidence epoch is open poisons that epoch. Guarded forming
    // has the dedicated one-shot op15 path below.
    if(bulk_epoch_valid && bulk_untracked_mutator) bulk_any_fault<=1;
    if(command[61:58]==9 || command[61:58]==11) begin
     have_result<=0; ramp_result<=0; array_result<=0; ramp_set<=command[57];
     ramp_fast<=command[61:58]==11;
     ramp_row<=command[4:0]; ramp_col<=command[9:5]; ramp_hold<=command[24:10];
     ramp_max_vcc<=command[56:41]; ramp_max_wl<=command[40:25];
     ramp_index<=0; ramp_pulses<=0; ramp_confirm_count<=0;
     ramp_met<=0; ramp_clip_seen<=0;
     wrapper_state<=WR_ADVANCE;
    end else if(command[61:58]==10) begin
     ramp_view<=1; have_result<=0; saved0<=0; saved1<=0;
     saved0[23:16]<=ramp_pulses; saved0[24]<=ramp_met;
     saved0[25]<=ramp_clip_seen; saved0[27:26]<=command[1:0];
     saved1[255:224]<=32'h524D5032; // RMP2
     ramp_read_addr<={command[1:0],5'd0}-{command[1:0],2'd0}; // page*28
     ramp_page_cursor<=0; ramp_page_primed<=0; wrapper_state<=WR_PAGE;
    end else if(command[61:58]==12 && array_command_end<11'd1024) begin
     have_result<=0; ramp_result<=0; array_result<=0;
     array_active<=1; array_set<=command[57]; array_reliable<=command[56];
     array_start<=command[55:46]; array_index<=command[55:46];
     array_end<=array_command_end[9:0]; array_remaining<=array_command_count;
     array_completed<=0; array_failures<=0; array_total_pulses<=0;
     array_last_current<=0; array_last_pulses<=0;
     array_last_met<=0; array_last_clip<=0;
     array_retry<=0; array_retry_pulses<=0; array_retry_limit<=command[20:19];
     ramp_set<=command[57]; ramp_fast<=command[57] || !command[56];
     ramp_row<=command[55:51]; ramp_col<=command[50:46];
     ramp_hold<=command[35:21]; array_base_hold<=command[35:21];
     ramp_max_vcc<=command[57] ? 16'd30147 : 16'd39321;
     ramp_max_wl<=command[57] ? 16'd22938 : 16'd37749;
     ramp_index<=0; ramp_pulses<=0; ramp_confirm_count<=0;
     ramp_met<=0; ramp_clip_seen<=0;
     issue_read(command[55:51],command[50:46]);
     wrapper_state<=WR_ARRAY_PRE;
    end else if(command[61:58]==12) begin
     array_active<=0; array_result<=0; have_result<=1;
     saved0<=0; saved1<=0; saved0[10]<=1; saved0[14:11]<=4'd7;
     saved1[255:224]<=32'h41524532; // ARE2: invalid physical range
    end else if(command[61:58]==13) begin
     array_view<=1; have_result<=0; saved0<=0; saved1<=0;
     saved0[31:16]<=({3'd0,command[6:0],4'd0}-{6'd0,command[6:0],1'b0}); // page*14
     saved1[255:224]<=32'h41524732; // ARG2
     array_read_addr<=({3'd0,command[6:0],4'd0}-{6'd0,command[6:0],1'b0});
     array_page_cursor<=0; array_page_primed<=0; wrapper_state<=WR_ARRAY_PAGE;
    end else if(command[61:58]==15) begin
    // Purpose-specific guarded forming command. The host supplies immutable
    // epoch identity, target and one reviewed VCC_SET ladder index. Fabric
    // fixes WL_SET=2.50 V and 1200 clocks and translates exactly one pulse to
    // core op8. The high WL rail turns on the selected access device.
     if(!bulk_clean_pre || !bulk_pre_order || bulk_hold!=15'd2400 || command[57] ||
        command[56:41]!=bulk_job || command[40:38]>3'd6 || command[37:10]!=0 ||
        command[9:5]!=bulk_col || command[4:0]!=bulk_row ||
        bulk_wb_started || bulk_post_started || bulk_scan_program_started) begin
      have_result<=1; saved0<=0; saved1<=0;
      saved0[10]<=1; saved0[14:11]<=4'd10;
      saved1[255:224]<=32'h47504531; // GPE1: guarded-program epoch mismatch
      bulk_any_fault<=1; bulk_job_mismatch<=1;
     end else begin
      have_result<=0; ramp_result<=0; array_result<=0;
      saved0<=0; saved1<=0;
      bulk_scan_program_started<=1; bulk_scan_program_done<=0;
      bulk_scan_program_inflight<=1;
      inner_command<={~inner_command[63],1'b1,4'd8,1'b1,
                      guarded_form_vcc_code(command[40:38]),16'd32768,15'd1200,
                      command[9:5],command[4:0]};
      wrapper_state<=WR_WAIT;
     end
    end else if(command[61:58]==14) begin
     if(command[24:10]<1 || command[24:10]>12000 || command[39:25]!=0 ||
        (command[57] && (!bulk_clean_pre ||
         command[56:41]!=bulk_job || command[24:10]!=bulk_hold ||
         command[9:5]!=bulk_col || command[4:0]!=bulk_row ||
         bulk_post_started || (bulk_wb_started && !bulk_wb_done) ||
         (bulk_scan_program_started && !bulk_scan_program_done)))) begin
      have_result<=1; saved0<=0; saved1<=0;
      saved0[10]<=1; saved0[14:11]<=4'd8;
      saved1[255:224]<=32'h42394531; // B9E1: invalid/mismatched phase
      if(command[57]) bulk_job_mismatch<=1;
     end else begin
      have_result<=0; ramp_result<=0; array_result<=0;
      saved0<=0; saved1<=0;
      bulk_phase<=command[57]; bulk_sentinel_first<=command[40];
      bulk_exec_index<=0; bulk_row<=command[4:0]; bulk_col<=command[9:5];
      bulk_hold<=command[24:10]; bulk_job<=command[56:41];
      bulk_crc_work<=32'hFFFFFFFF; bulk_crc_prefix<=0;
      bulk_crc_frame<=0; bulk_crc_word<=0;
      if(!command[57]) begin
       bulk_sequence<=bulk_sequence+1'b1;
       bulk_epoch_valid<=1;
       bulk_pre_order<=command[40]; bulk_post_order<=0;
       bulk_pre_valid<=0; bulk_post_valid<=0;
       bulk_pre_clip<=0; bulk_post_clip<=0;
       bulk_pre_crc<=0; bulk_post_crc<=0;
       bulk_any_fault<=0; bulk_job_mismatch<=0;
       bulk_pre_done<=0; bulk_wb_done<=0; bulk_post_done<=0;
       bulk_wb_job<=0; bulk_wb_valid<=0; bulk_wb_crc<=0;
       bulk_wb_echo<=0; bulk_wb_protocol_error<=0;
       bulk_wb_started<=0; bulk_post_started<=0;
       bulk_scan_program_started<=0; bulk_scan_program_done<=0;
       bulk_scan_program_inflight<=0;
      end else begin
       // Consume the post-bank slot before issuing its first inner read. An
       // abort/fault cannot be retried against the same pre/program evidence.
       bulk_post_started<=1;
       bulk_post_order<=command[40]; bulk_post_valid<=0;
       bulk_post_clip<=0; bulk_post_crc<=0; bulk_post_done<=0;
      end
      issue_bulk_read(0,command[40],command[4:0],command[9:5],command[24:10]);
      wrapper_state<=WR_BULK_WAIT;
     end
    end else if(command[61:58]==3 && !command[40] &&
                (!bulk_clean_pre || command[56:41]!=bulk_job ||
                 command[29:25]!=bulk_row || command[24:20]!=bulk_col ||
                 bulk_wb_started || bulk_post_started ||
                 bulk_scan_program_started)) begin
     // A WB command that claims an existing scan epoch is rejected before it
     // reaches the core unless the complete phase-0 bank and immutable
     // job/target identity match.  This is fail-closed for SET/RESET: a stale
     // or mismatched host packet cannot issue a programming pulse.
     have_result<=1; ramp_result<=0; array_result<=0;
     saved0<=0; saved1<=0; saved0[10]<=1; saved0[14:11]<=4'd9;
     saved1[255:224]<=32'h57424531; // WBE1: WB epoch/target mismatch
     bulk_job_mismatch<=1; bulk_any_fault<=1;
    end else begin
     have_result<=0; ramp_result<=0; array_result<=0;
     if(command[61:58]==3 && command[40]) begin
      // Standalone WB transaction: open a fresh evidence epoch without a
      // preceding scan block.  Bit 40 is outside the packet/tag payload and is
      // ignored by cell_control_core.
      bulk_sequence<=bulk_sequence+1'b1; bulk_epoch_valid<=1;
      bulk_job<=command[56:41]; bulk_row<=command[29:25]; bulk_col<=command[24:20];
      bulk_pre_order<=0; bulk_post_order<=0;
      bulk_pre_valid<=0; bulk_post_valid<=0;
      bulk_pre_clip<=0; bulk_post_clip<=0;
      bulk_pre_crc<=0; bulk_post_crc<=0;
      bulk_any_fault<=0; bulk_job_mismatch<=0;
      bulk_pre_done<=0; bulk_wb_done<=0; bulk_post_done<=0;
      bulk_wb_job<=0; bulk_wb_valid<=0; bulk_wb_crc<=0;
      bulk_wb_echo<=0; bulk_wb_protocol_error<=0;
      bulk_wb_started<=0; bulk_post_started<=0;
      bulk_scan_program_started<=0; bulk_scan_program_done<=0;
      bulk_scan_program_inflight<=0;
     end else if(command[61:58]==3 &&
                 (!bulk_epoch_valid || command[56:41]!=bulk_job)) begin
      bulk_job_mismatch<=1;
     end
     if(command[61:58]==3 && !command[40]) begin
      // Consume the complete phase-0 authorization before the WB command is
      // forwarded. It remains consumed after abort, timeout or bad echo.
      bulk_wb_started<=1;
      bulk_wb_done<=0; bulk_wb_valid<=0; bulk_wb_crc<=0;
      bulk_wb_echo<=0; bulk_wb_protocol_error<=0;
     end
     inner_command<={~inner_command[63],command[62:0]}; wrapper_state<=WR_WAIT;
    end
   end
   WR_WAIT: if(inner_complete) begin
    saved0<=inner_result0; saved1<=inner_result1; have_result<=1; wrapper_state<=WR_READY;
    // Any intervening inner-operation fault breaks the provenance of an open
    // pre epoch, not only faults reported by the WB operation itself.
    if(bulk_epoch_valid && inner_result0[10]) bulk_any_fault<=1;
    if(bulk_scan_program_inflight) begin
     bulk_scan_program_inflight<=0;
     // A program-pulse clip (bit 44) is retained in the normal feedback for
     // host safety accounting, but post verification must still run. Only a
     // core fault or clipped restored read invalidates this one-shot step.
     if(inner_result0[10] || inner_result0[37]) begin
      bulk_scan_program_done<=0; bulk_any_fault<=1;
     end else bulk_scan_program_done<=1;
    end
    if(inner_command[61:58]==3) begin
     bulk_wb_job<=inner_command[56:41]; bulk_wb_valid<=inner_wb_valid;
     bulk_wb_crc<=inner_wb_crc; bulk_wb_echo<=inner_wb_echo_match;
     bulk_wb_protocol_error<=inner_result1[56] || !inner_wb_echo_match;
     bulk_wb_done<=(inner_wb_valid==15'h7FFF) && inner_wb_echo_match &&
                   !inner_result1[56] && !inner_result0[10];
     if(!inner_wb_echo_match || inner_result1[56] || inner_result0[10])
      bulk_any_fault<=1;
     if(bulk_epoch_valid && inner_command[56:41]!=bulk_job) bulk_job_mismatch<=1;
    end
   end
   WR_ABORT: if(inner_complete) begin
    saved0<=inner_result0; saved1<=inner_result1; have_result<=1; wrapper_state<=WR_READY;
   end
   WR_ADVANCE: begin
    if(ramp_index>=(ramp_fast ? 3 : 96)) begin
     if(array_active) complete_array_sweep(1'b0,ramp_current_compact,ramp_pulses);
     else begin ramp_met<=0; ramp_result<=1; have_result<=1; wrapper_state<=WR_READY; end
    end else if(ramp_vcc_code(ramp_set,ramp_fast,ramp_index)<=ramp_max_vcc &&
                ramp_wl_code(ramp_set,ramp_fast,ramp_index)<=ramp_max_wl) begin
     issue_step(ramp_set,ramp_index,ramp_hold,ramp_row,ramp_col);
     wrapper_state<=WR_STEP;
    end else ramp_index<=ramp_index+1'b1;
   end
   WR_PAGE: begin
    if(!ramp_page_primed) begin
     // Prime the synchronous read and point at the following sample.
     ramp_page_primed<=1; ramp_read_addr<=ramp_read_addr+1'b1;
    end else begin
     if(ramp_page_cursor<14)
      saved0[255:32]<={ramp_read_data,saved0[255:48]};
     else
      saved1[223:0]<={ramp_read_data,saved1[223:16]};
     if(ramp_page_cursor==27) begin
      have_result<=1; wrapper_state<=WR_READY;
     end else begin
      ramp_page_cursor<=ramp_page_cursor+1'b1;
      ramp_read_addr<=ramp_read_addr+1'b1;
     end
    end
   end
   WR_ARRAY_PAGE: begin
    if(!array_page_primed) begin
     array_page_primed<=1; array_read_addr<=array_read_addr+1'b1;
    end else begin
     if(array_page_cursor<7)
      saved0[255:32]<={array_read_data,saved0[255:64]};
     else
      saved1[223:0]<={array_read_data,saved1[223:32]};
     if(array_page_cursor==13) begin
      have_result<=1; wrapper_state<=WR_READY;
     end else begin
      array_page_cursor<=array_page_cursor+1'b1;
      array_read_addr<=array_read_addr+1'b1;
     end
    end
   end
   WR_BULK_WAIT: if(inner_complete) begin
    bulk_frame[(bulk_phase ? 9 : 0)+bulk_store_slot]<={inner_result1,inner_result0};
    if(bulk_phase) begin
     bulk_post_valid[bulk_store_slot]<=1;
     bulk_post_clip[bulk_store_slot]<=inner_result0[37];
    end else begin
     bulk_pre_valid[bulk_store_slot]<=1;
     bulk_pre_clip[bulk_store_slot]<=inner_result0[37];
    end
    bulk_any_fault<=bulk_any_fault|inner_result0[10];
    if(inner_result0[10]) begin
     if(bulk_phase) begin bulk_post_valid<=0; bulk_post_done<=0; end
     else begin bulk_pre_valid<=0; bulk_pre_done<=0; end
     saved0<=inner_result0; saved1<=0;
     saved1[255:224]<=32'h42394532; // B9E2: inner read fault
     have_result<=1; wrapper_state<=WR_READY;
    end else if(bulk_exec_index==8) begin
     bulk_crc_work<=32'hFFFFFFFF; bulk_crc_prefix<=0;
     bulk_crc_frame<=0; bulk_crc_word<=0;
     wrapper_state<=WR_BULK_CRC;
    end else begin
     bulk_exec_index<=bulk_exec_index+1'b1;
     issue_bulk_read(bulk_exec_index+1'b1,bulk_sentinel_first,
                     bulk_row,bulk_col,bulk_hold);
    end
   end
   WR_BULK_CRC: begin
    if(bulk_crc_prefix<2) begin
     bulk_crc_work<=bulk_crc_next;
     bulk_crc_prefix<=bulk_crc_prefix+1'b1;
    end else if(bulk_crc_word==15) begin
     if(bulk_crc_frame==8) begin
      if(bulk_phase) begin
       bulk_post_crc<=bulk_crc_next^32'hFFFFFFFF; bulk_post_done<=1;
      end else begin
       bulk_pre_crc<=bulk_crc_next^32'hFFFFFFFF; bulk_pre_done<=1;
      end
      saved0<=bulk_crc_selected_frame[255:0]; saved1<=0;
      saved1[255:224]<=32'h42395231; // B9R1
      saved1[223:192]<=bulk_crc_next^32'hFFFFFFFF;
      saved1[191:160]<=bulk_sequence;
      saved1[159:144]<=bulk_job;
      saved1[143:135]<=bulk_phase ? bulk_post_valid : bulk_pre_valid;
      saved1[134]<=bulk_phase; saved1[133]<=bulk_sentinel_first;
      saved1[132:128]<=bulk_row; saved1[127:123]<=bulk_col;
      saved1[122:108]<=bulk_hold;
      have_result<=1; wrapper_state<=WR_READY;
     end else begin
      bulk_crc_work<=bulk_crc_next; bulk_crc_frame<=bulk_crc_frame+1'b1;
      bulk_crc_word<=0;
     end
    end else begin
     bulk_crc_work<=bulk_crc_next; bulk_crc_word<=bulk_crc_word+1'b1;
    end
   end
   WR_ARRAY_PRE: if(inner_complete) begin
    saved0<=inner_result0; saved1<=inner_result1;
    if(inner_result0[10] || inner_result0[37]) begin
     saved0[10]<=1; saved0[14:11]<=4'd1; array_active<=0;
     have_result<=1; wrapper_state<=WR_READY;
    end else if(array_initial_pass) begin
     ramp_confirm_count<=0; issue_read(ramp_row,ramp_col);
     wrapper_state<=WR_ARRAY_CONFIRM;
    end else begin
     ramp_index<=0; ramp_pulses<=0; ramp_confirm_count<=0;
     wrapper_state<=WR_ADVANCE;
    end
   end
   WR_ARRAY_CONFIRM: if(inner_complete) begin
    saved0<=inner_result0; saved1<=inner_result1;
    if(inner_result0[10] || inner_result0[37]) begin
     saved0[10]<=1; saved0[14:11]<=4'd1; array_active<=0;
     have_result<=1; wrapper_state<=WR_READY;
    end else if(array_initial_pass && ramp_confirm_count==9) begin
     finish_array_cell(1'b1,ramp_current_compact,8'd0);
    end else if(array_initial_pass) begin
     ramp_confirm_count<=ramp_confirm_count+1'b1;
     issue_read(ramp_row,ramp_col);
    end else begin
     ramp_index<=0; ramp_pulses<=0; ramp_confirm_count<=0;
     wrapper_state<=WR_ADVANCE;
    end
   end
   WR_STEP: if(inner_complete) begin
    saved0<=inner_result0; saved1<=inner_result1;
    ramp_clip_seen<=ramp_clip_seen|inner_result0[44];
    // Trace-only encoding: delta is 48000 times the average ADC-code
    // difference.  Store delta/2^14 exactly with a wire shift; the host
    // restores the 2^14/48000 scale.  This keeps <0.43 uA quantization while
    // avoiding a wide constant divider on the 200 MHz RAM write path.
    ramp_current_code[ramp_index]<=ramp_delta>>>14;
    ramp_pulses<=ramp_pulses+1'b1;
    if(inner_result0[10] || inner_result0[37]) begin
     saved0[10]<=1; saved0[14:11]<=4'd1;
     if(array_active) array_active<=0;
     have_result<=1; wrapper_state<=WR_READY;
    end else if(ramp_pass) begin
     ramp_confirm_count<=0; issue_read(ramp_row,ramp_col); wrapper_state<=WR_CONFIRM;
    end else if(ramp_index<(ramp_fast ? 2 : 95)) begin
     ramp_index<=ramp_index+1'b1; wrapper_state<=WR_ADVANCE;
    end else begin
     if(array_active) complete_array_sweep(1'b0,ramp_current_compact,ramp_pulses+1'b1);
     else begin ramp_met<=0; ramp_result<=1; have_result<=1; wrapper_state<=WR_READY; end
    end
   end
   WR_CONFIRM: if(inner_complete) begin
    saved0<=inner_result0; saved1<=inner_result1;
    if(inner_result0[10] || inner_result0[37]) begin
     saved0[10]<=1; saved0[14:11]<=4'd1;
     if(array_active) array_active<=0;
     have_result<=1; wrapper_state<=WR_READY;
    end else if(ramp_pass && ramp_confirm_count==9) begin
     if(array_active) complete_array_sweep(1'b1,ramp_current_compact,ramp_pulses);
     else begin
      ramp_met<=1; ramp_confirm_count<=10; ramp_result<=1; have_result<=1; wrapper_state<=WR_READY;
     end
    end else if(ramp_pass) begin
     ramp_confirm_count<=ramp_confirm_count+1'b1; issue_read(ramp_row,ramp_col);
    end else if(ramp_index<(ramp_fast ? 2 : 95)) begin
     ramp_confirm_count<=0; ramp_index<=ramp_index+1'b1;
     wrapper_state<=WR_ADVANCE;
    end else begin
     if(array_active) complete_array_sweep(1'b0,ramp_current_compact,ramp_pulses);
     else begin ramp_met<=0; ramp_result<=1; have_result<=1; wrapper_state<=WR_READY; end
    end
   end
   default: wrapper_state<=WR_READY;
  endcase
 end
endmodule
