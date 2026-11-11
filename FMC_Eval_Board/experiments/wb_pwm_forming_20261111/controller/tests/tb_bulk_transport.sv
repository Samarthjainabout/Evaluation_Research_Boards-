`timescale 1ns/1ps

module tb_bulk_transport;
 reg clk=0, feedback_clk=0;
 reg ready_i=1, reset_i=1, uart_rx=1, dac_ready=1, adc_healthy=1;
 reg [63:0] command=0;
 reg sample_valid=0;
 reg [59:0] samples=0;
 reg dac_done=0;
 wire interface_enable, reset_low, tm, se, si, scan_tristate, si_tristate;
 wire dac_start;
 wire [23:0] dac_frame;
 wire [255:0] result0, result1, bulk_header;
 wire [9215:0] bulk_scan_frames;
 wire [479:0] bulk_tdc_words;
 reg [255:0] guard_header_raw=0;
 reg [479:0] guard_tdc_raw=0;
 reg guard_keepalive=0;
 wire [255:0] guard_header,guard_tdc_0_7,guard_tdc_8_14;
 reg [9215:0] pager_frames=0;
 reg [3:0] pager_control=0;
 wire [255:0] pager_header,pager_descriptor;
 wire [2559:0] pager_data;

 reg [255:0] mock_r0=0, mock_r1=0;
 reg mock_complete=0;
 reg [479:0] mock_wb_words=0;
 reg [14:0] mock_wb_valid=0;
 reg [31:0] mock_wb_crc=0;
 reg toggle=0;
 reg inner_toggle_before=0;
 reg [511:0] post0_before=0;
 reg [511:0] expected_pre[0:8];
 reg [511:0] expected_post[0:8];
 integer i, errors=0, block_clip_exec=-1;

 bulk_vio_fail_closed_guard guard_dut(
  .header_raw(guard_header_raw),.tdc_words_raw(guard_tdc_raw),
  .keepalive(guard_keepalive),.header(guard_header),
  .tdc_0_7(guard_tdc_0_7),.tdc_8_14(guard_tdc_8_14));
 bulk_vio_pager pager_dut(
  .clk(clk),.header_raw(guard_header_raw),.scan_frames_raw(pager_frames),
  .tdc_words_raw(guard_tdc_raw),.page_control(pager_control),
  .header(pager_header),.page_descriptor(pager_descriptor),.page_data(pager_data));

 always #5 clk=~clk;
 always #13 feedback_clk=~feedback_clk;

 cell_control_engine #(
  .RESET_SYS(4),.POST_CYCLES(2),.READY_FALLBACK_CYCLES(2),
  .WATCHDOG_CYCLES(100000),.WB_ZERO(2),.WB_ONE(3),.WB_HIGH(2),
  .WB_END(2),.CLOCK_MISSING(100000),.DAC_SETTLE_CYCLES(2)
 ) dut (
  .clk(clk),.feedback_clk(feedback_clk),.ready_i(ready_i),.reset_i(reset_i),
  .uart_rx(uart_rx),.dac_ready(dac_ready),.adc_healthy(adc_healthy),
  .command(command),.sample_valid(sample_valid),.samples(samples),
  .interface_enable(interface_enable),.reset_low(reset_low),.tm(tm),.se(se),.si(si),
  .scan_tristate(scan_tristate),.si_tristate(si_tristate),
  .dac_start(dac_start),.dac_frame(dac_frame),.dac_done(dac_done),
  .result0(result0),.result1(result1),.bulk_scan_frames(bulk_scan_frames),
  .bulk_tdc_words(bulk_tdc_words),.bulk_header(bulk_header));

 function automatic [31:0] crc_word(input [31:0] crc_in,input [31:0] data);
  reg [31:0] c; integer b;
  begin
   c=crc_in;
   for(b=0;b<32;b=b+1)
    c=(c[0]^data[b]) ? ((c>>1)^32'hEDB88320) : (c>>1);
   crc_word=c;
  end
 endfunction

 task automatic fail(input [8*96-1:0] message);
  begin $display("FAIL %0s",message); errors=errors+1; end
 endtask

 task automatic finish_wb(input [31:0] echo,input bit protocol,input bit fault);
  begin
   mock_r0=0; mock_r1=0;
   mock_r0[9]=1; mock_r0[10]=fault; mock_r0[15]=dut.inner_command[63];
   mock_r1[55]=1; mock_r1[56]=protocol; mock_r1[95:64]=echo;
   @(negedge clk); mock_complete=1;
   @(posedge clk); #1;
   @(negedge clk); mock_complete=0;
  end
 endtask

 task automatic issue(input [3:0] op,input [57:0] payload);
  begin
   toggle=~toggle;
   @(negedge clk); command={toggle,1'b1,op,payload};
   @(posedge clk); #1;
  end
 endtask

 task automatic finish_inner(input integer tag,input bit clip);
  begin
   mock_r0=0; mock_r1=0;
   mock_r0[9]=1; mock_r0[15]=dut.inner_command[63];
   mock_r0[37]=clip; mock_r0[31:16]=16'h1000+tag;
   mock_r0[255:224]=32'hA0000000+tag;
   mock_r1[255:224]=32'h53435232;
   mock_r1[31:0]=32'hB0000000+tag;
   @(negedge clk); mock_complete=1;
   @(posedge clk); #1;
   @(negedge clk); mock_complete=0;
  end
 endtask

 task automatic finish_generic_fault;
  begin
   mock_r0=0; mock_r1=0;
   mock_r0[9]=1; mock_r0[10]=1; mock_r0[15]=dut.inner_command[63];
   @(negedge clk); mock_complete=1;
   @(posedge clk); #1;
   @(negedge clk); mock_complete=0;
  end
 endtask

 task automatic finish_guarded_program(input bit pulse_clip,input bit read_clip,
                                        input bit fault);
  begin
   mock_r0=0; mock_r1=0;
   mock_r0[9]=1; mock_r0[10]=fault; mock_r0[15]=dut.inner_command[63];
   mock_r0[37]=read_clip; mock_r0[44]=pulse_clip;
   mock_r0[31:16]=16'h2468; mock_r1[255:224]=32'h53435232;
   @(negedge clk); mock_complete=1;
   @(posedge clk); #1;
   @(negedge clk); mock_complete=0;
  end
 endtask

 task automatic check_read_address(input integer exec,input bit sentinel_first,
                                    input [4:0] row,input [4:0] col);
  reg [4:0] er,ec; integer sidx;
  begin
   er=row; ec=col;
   if((sentinel_first && exec<4) || (!sentinel_first && exec>=5)) begin
    sidx=sentinel_first ? exec : exec-5;
    case(sidx)
     0: er=row+8;
     1: er=row+16;
     2: er=row+24;
     default: ec=col+16;
    endcase
   end
   if(dut.inner_command[61:58]!==4'd1 || dut.inner_command[57]!==1'b0 ||
      dut.inner_command[4:0]!==er || dut.inner_command[9:5]!==ec)
    fail("op14 emitted wrong/non-READ inner address");
  end
 endtask

 task automatic run_block(input bit phase,input bit sentinel_first,
                           input [15:0] job,input [4:0] row,input [4:0] col,
                           input [14:0] hold,input integer tag_base);
  reg [57:0] payload; integer exec,slot;
  begin
   payload=0; payload[57]=phase; payload[56:41]=job;
   payload[40]=sentinel_first; payload[24:10]=hold;
   payload[9:5]=col; payload[4:0]=row;
   issue(14,payload);
   for(exec=0;exec<9;exec=exec+1) begin
    if(dut.wrapper_state!==10) fail("op14 did not wait for inner read");
    check_read_address(exec,sentinel_first,row,col);
    finish_inner(tag_base+exec,(exec==block_clip_exec));
    slot=sentinel_first ? (exec<4 ? exec+5 : exec-4) : exec;
    if(phase) expected_post[slot]={mock_r1,mock_r0};
    else expected_pre[slot]={mock_r1,mock_r0};
   end
   block_clip_exec=-1;
   wait(dut.wrapper_state==0 && result0[9]);
   @(posedge clk); #1;
  end
 endtask

 task automatic validate_crc(input bit phase,input bit sentinel_first,
                              input [15:0] job,input [4:0] row,input [4:0] col,
                              input [14:0] hold);
  reg [31:0] c,descriptor; reg [511:0] frame; integer f,w;
  begin
   descriptor={5'd1,phase,sentinel_first,row,col,hold};
   c=crc_word(32'hFFFFFFFF,descriptor);
   c=crc_word(c,{16'hB901,job});
   for(f=0;f<9;f=f+1) begin
    frame=phase ? expected_post[f] : expected_pre[f];
    for(w=0;w<16;w=w+1) c=crc_word(c,frame[32*w+:32]);
   end
   c=c^32'hFFFFFFFF;
   if((phase ? bulk_header[95:64] : bulk_header[159:128])!==c)
    fail("scan payload CRC mismatch");
  end
 endtask

 task automatic abort_at(input integer abort_index);
  reg [57:0] payload; integer exec;
  begin
   payload=0; payload[56:41]=16'h7000+abort_index;
   payload[24:10]=15'd2400; payload[9:5]=5'd1; payload[4:0]=5'd7;
   issue(14,payload);
   for(exec=0;exec<abort_index;exec=exec+1) finish_inner(300+exec,0);
   if(dut.wrapper_state!==10) fail("bulk abort test not in wait state");
   issue(4,0);
   finish_inner(399,0);
   wait(dut.wrapper_state==0 && result0[9]);
   if(bulk_header[63:55]!==0 || bulk_header[208]!==0)
    fail("abort published a complete/partially-valid pre bank");
  end
 endtask

 initial begin
  // The otherwise inert VIO output must be exactly transparent at zero and
  // independently observable as both any_fault and nonzero padding at one.
  guard_header_raw=256'h42554C4B020000001234567889ABCDEF00112233445566778899AABBCCDDEEFF;
  guard_header_raw[211]=0;
  guard_tdc_raw=480'h123456789ABCDEF00112233445566778899AABBCCDDEEFF102132435465768798A9BACBDCEDFE0F00123456789ABCDEF001122334455667;
  guard_keepalive=0; #1;
  if(guard_header!==guard_header_raw || guard_tdc_0_7!==guard_tdc_raw[255:0] ||
     guard_tdc_8_14!=={32'd0,guard_tdc_raw[479:256]})
   fail("zero keepalive changed bulk evidence");
  guard_keepalive=1; #1;
  if(!guard_header[211] || !guard_tdc_8_14[224] ||
     guard_header[210:0]!==guard_header_raw[210:0] ||
     guard_header[255:212]!==guard_header_raw[255:212] ||
     guard_tdc_8_14[223:0]!==guard_tdc_raw[479:256] ||
     guard_tdc_8_14[255:225]!==0)
   fail("nonzero keepalive did not fail closed");
  guard_keepalive=0;

  // Paged-v2 exact mapping, descriptor binding, TDC validity gating, invalid
  // selector behavior and keepalive fail-closed behavior.
  guard_header_raw=0;
  guard_header_raw[255:224]=32'h42554C4B;
  guard_header_raw[223:216]=8'd2;
  guard_header_raw[208]=1;
  guard_header_raw[207:176]=32'h12345678;
  guard_header_raw[175:160]=16'hBEEF;
  guard_header_raw[159:128]=32'h89ABCDEF;
  guard_header_raw[63:55]=9'h1FF;
  guard_header_raw[12]=1;
  guard_header_raw[10:6]=5'd7;
  guard_header_raw[5:1]=5'd1;
  guard_header_raw[0]=1;
  for(i=0;i<36;i=i+1) pager_frames[256*i+:256]=i+1;
  pager_control=0; repeat(3) @(posedge clk); #1;
  if(pager_header!==guard_header_raw || pager_descriptor[255:224]!==32'h42504732 ||
     pager_descriptor[223:216]!==2 || pager_descriptor[215:208]!==0 ||
     pager_descriptor[207:176]!==32'h12345678 || pager_descriptor[175:160]!==16'hBEEF ||
     pager_descriptor[159:152]!==2 || !pager_descriptor[151] ||
     !pager_descriptor[150] || pager_descriptor[149:140]!==10'h3FF ||
     pager_descriptor[139:135]!==7 || pager_descriptor[134:130]!==1 ||
     pager_descriptor[69:0]!==0)
   fail("paged-v2 page0 descriptor binding invalid");
  for(i=0;i<10;i=i+1)
   if(pager_data[256*i+:256]!==i+1) fail("paged-v2 page0 pre mapping invalid");
  pager_control=1; repeat(3) @(posedge clk); #1;
  for(i=0;i<8;i=i+1)
   if(pager_data[256*i+:256]!==i+11) fail("paged-v2 page1 pre mapping invalid");
  if(pager_data[2048+:512]!==0) fail("paged-v2 pre exposed stale TDC payload");
  guard_header_raw[209]=1;
  guard_header_raw[45:31]=15'h7FFF;
  guard_header_raw[127:96]=32'h10203040;
  #1;
  if(pager_data[2048+:256]!==guard_tdc_raw[255:0] ||
     pager_data[2304+:256]!=={32'd0,guard_tdc_raw[479:256]})
   fail("paged-v2 page1 WB TDC mapping invalid");
  pager_control=2; repeat(3) @(posedge clk); #1;
  for(i=0;i<10;i=i+1)
   if(pager_data[256*i+:256]!==i+19) fail("paged-v2 page2 post mapping invalid");
  pager_control=3; repeat(3) @(posedge clk); #1;
  for(i=0;i<8;i=i+1)
   if(pager_data[256*i+:256]!==i+29) fail("paged-v2 page3 post mapping invalid");
  if(pager_data[2048+:256]!==guard_tdc_raw[255:0] ||
     pager_data[2304+:256]!=={32'd0,guard_tdc_raw[479:256]})
   fail("paged-v2 page3 TDC mapping invalid");
  pager_control=4; repeat(3) @(posedge clk); #1;
  if(pager_descriptor!==0 || pager_data!==0 || !pager_header[211])
   fail("paged-v2 invalid selector did not return zero/fault");
  pager_control=8; repeat(3) @(posedge clk); #1;
  if(pager_descriptor!==0 || pager_data!==0 || !pager_header[211])
   fail("paged-v2 keepalive did not fail closed");
  pager_control=0;

  force dut.inner_result0=mock_r0;
  force dut.inner_result1=mock_r1;
  force dut.inner_complete=mock_complete;
  force dut.inner_wb_words=mock_wb_words;
  force dut.inner_wb_valid=mock_wb_valid;
  force dut.inner_wb_crc=mock_wb_crc;
  repeat(4) @(posedge clk);

  // Guarded pre: sentinels execute first, but retained slots stay canonical.
  run_block(0,1,16'h1234,5'd7,5'd1,15'd2400,0);
  if(bulk_header[255:224]!==32'h42554C4B || bulk_header[223:216]!==2 ||
     bulk_header[63:55]!==9'h1FF || !bulk_header[208] || !bulk_header[12] ||
     bulk_header[175:160]!==16'h1234 || bulk_header[10:6]!==7 ||
     bulk_header[5:1]!==1 || bulk_header[207:176]!==1)
   fail("pre header identity/status invalid");
  for(i=0;i<9;i=i+1)
   if(bulk_scan_frames[512*i+:512]!==expected_pre[i]) fail("pre canonical frame mismatch");
  validate_crc(0,1,16'h1234,7,1,2400);

  // Standard post: target first, same immutable epoch and identity.
  run_block(1,0,16'h1234,5'd7,5'd1,15'd2400,100);
  if(bulk_header[54:46]!==9'h1FF || !bulk_header[210] || bulk_header[11] ||
     bulk_header[207:176]!==1) fail("post header identity/status invalid");
  for(i=0;i<9;i=i+1) begin
   if(bulk_scan_frames[512*i+:512]!==expected_pre[i]) fail("post changed immutable pre bank");
   if(bulk_scan_frames[512*(i+9)+:512]!==expected_post[i]) fail("post canonical frame mismatch");
  end
  validate_crc(1,0,16'h1234,7,1,2400);

  // A completed post bank is immutable. A second phase1 in the same epoch is
  // rejected before an inner read and cannot overwrite the retained frame.
  post0_before=bulk_scan_frames[4608+:512];
  inner_toggle_before=dut.inner_command[63];
  issue(14,{1'b1,16'h1234,1'b0,15'd0,15'd2400,5'd1,5'd7});
  if(!result0[10] || dut.inner_command[63]!==inner_toggle_before ||
     bulk_scan_frames[4608+:512]!==post0_before)
   fail("second phase1 overwrote completed post bank");

  // A mismatched post is rejected before any read and marks the epoch.
  issue(14,{1'b1,16'h1235,1'b0,15'd0,15'd2400,5'd1,5'd7});
  if(!result0[10] || !bulk_header[213]) fail("mismatched phase1 was not rejected");

  // Starting phase1 consumes the post slot even if op4 aborts its first read.
  // Neither another post nor a late matched WB may reuse that epoch.
  run_block(0,0,16'h1A2B,5'd7,5'd1,15'd2400,150);
  issue(14,{1'b1,16'h1A2B,1'b0,15'd0,15'd2400,5'd1,5'd7});
  if(dut.wrapper_state!==10) fail("post abort test did not enter bulk wait");
  issue(4,0); finish_inner(199,0);
  wait(dut.wrapper_state==0 && result0[9]);
  if(bulk_header[210] || bulk_header[54:46]!==0)
   fail("aborted post published a valid bank");
  post0_before=bulk_scan_frames[4608+:512];
  inner_toggle_before=dut.inner_command[63];
  issue(14,{1'b1,16'h1A2B,1'b0,15'd0,15'd2400,5'd1,5'd7});
  if(!result0[10] || dut.inner_command[63]!==inner_toggle_before ||
     bulk_scan_frames[4608+:512]!==post0_before)
   fail("aborted phase1 was reusable");
  inner_toggle_before=dut.inner_command[63];
  issue(3,{1'b1,16'h1A2B,1'b0,8'h80,2'b11,5'd7,5'd1,20'h2AAF0});
  if(!result0[10] || dut.inner_command[63]!==inner_toggle_before)
   fail("matched WB was accepted after post started");

  // A WB command claiming an existing epoch must match the complete pre bank,
  // job and target. A mismatch is rejected without toggling the inner core,
  // which proves no SET/RESET pulse can escape this guard.
  run_block(0,0,16'h2222,5'd7,5'd1,15'd2400,200);
  inner_toggle_before=dut.inner_command[63];
  issue(3,{1'b1,16'h2222,1'b0,8'h80,2'b11,5'd8,5'd1,20'h2AAF0});
  if(!result0[10] || !bulk_header[213] || !bulk_header[211] ||
     dut.wrapper_state!==0 || dut.inner_command[63]!==inner_toggle_before)
   fail("mismatched WB epoch/target reached inner core");
  inner_toggle_before=dut.inner_command[63];
  issue(3,{1'b1,16'h2222,1'b0,8'h80,2'b11,5'd7,5'd1,20'h2AAF0});
  if(!result0[10] || dut.inner_command[63]!==inner_toggle_before)
   fail("poisoned epoch authorized a matched WB program");
  inner_toggle_before=dut.inner_command[63];
  issue(14,{1'b1,16'h2222,1'b0,15'd0,15'd2400,5'd1,5'd7});
  if(!result0[10] || dut.inner_command[63]!==inner_toggle_before)
   fail("poisoned epoch authorized phase1");

  // A complete pre bank with any clipped frame is evidence, but is never an
  // authorization to pulse. The reject must happen before the core toggle.
  block_clip_exec=2;
  run_block(0,0,16'h2C2C,5'd7,5'd1,15'd2400,225);
  if(bulk_header[30:22]===0) fail("clipped pre test did not publish clip mask");
  inner_toggle_before=dut.inner_command[63];
  issue(3,{1'b1,16'h2C2C,1'b0,8'h80,2'b11,5'd7,5'd1,20'h2AAF0});
  if(!result0[10] || dut.inner_command[63]!==inner_toggle_before)
   fail("clipped pre authorized a matched WB program");
  block_clip_exec=2;
  run_block(0,0,16'h2D2D,5'd7,5'd1,15'd2400,235);
  inner_toggle_before=dut.inner_command[63];
  issue(14,{1'b1,16'h2D2D,1'b0,15'd0,15'd2400,5'd1,5'd7});
  if(!result0[10] || dut.inner_command[63]!==inner_toggle_before)
   fail("clipped pre authorized phase1");

  // Guarded forming has a dedicated one-shot outer op15. The host supplies
  // job/target plus a reviewed VCC ladder index; WL and hold remain fixed.
  // A program-pulse clip remains visible in feedback but must still permit
  // mandatory post verification when the restored read itself is clean.
  run_block(0,1,16'hF001,5'd7,5'd1,15'd2400,245);
  issue(15,{1'b0,16'hF001,3'd4,28'd0,5'd1,5'd7});
  if(dut.wrapper_state!==1 || dut.inner_command[61:58]!==4'd8 ||
     dut.inner_command[57]!==1'b1 || dut.inner_command[56:41]!==16'd32768 ||
     dut.inner_command[40:25]!==16'd32768 || dut.inner_command[24:10]!==15'd1200 ||
     dut.inner_command[9:5]!==5'd1 || dut.inner_command[4:0]!==5'd7 || bulk_header[0])
   fail("valid op15 did not issue exact ladder guarded SET");
  finish_guarded_program(1,0,0);
  wait(dut.wrapper_state==0 && result0[9]);
  if(!dut.bulk_scan_program_started || !dut.bulk_scan_program_done ||
     bulk_header[211] || !result0[44])
   fail("op15 program clip incorrectly blocked post evidence");
  run_block(1,0,16'hF001,5'd7,5'd1,15'd2400,255);
  if(!bulk_header[210]) fail("successful op15 did not authorize phase1");

  // A completed op15 consumes the epoch's only programming authorization;
  // a matched WB must be rejected before its inner toggle.
  run_block(0,1,16'hF002,5'd7,5'd1,15'd2400,265);
  issue(15,{1'b0,16'hF002,3'd0,28'd0,5'd1,5'd7});
  finish_guarded_program(0,0,0);
  wait(dut.wrapper_state==0 && result0[9]);
  inner_toggle_before=dut.inner_command[63];
  issue(3,{1'b1,16'hF002,1'b0,8'h80,2'b11,5'd7,5'd1,20'h2AAF0});
  if(!result0[10] || dut.inner_command[63]!==inner_toggle_before)
   fail("successful op15 authorized a second matched-WB pulse");

  // op15 itself is one-shot even after a clean successful completion.
  run_block(0,1,16'hF003,5'd7,5'd1,15'd2400,275);
  issue(15,{1'b0,16'hF003,3'd0,28'd0,5'd1,5'd7});
  finish_guarded_program(0,0,0);
  wait(dut.wrapper_state==0 && result0[9]);
  inner_toggle_before=dut.inner_command[63];
  issue(15,{1'b0,16'hF003,3'd0,28'd0,5'd1,5'd7});
  if(!result0[10] || dut.inner_command[63]!==inner_toggle_before)
   fail("second op15 reused one guarded-program authorization");

  // Aborting the one-shot keeps it consumed and leaves no success evidence.
  run_block(0,1,16'hF004,5'd7,5'd1,15'd2400,285);
  issue(15,{1'b0,16'hF004,3'd0,28'd0,5'd1,5'd7});
  if(dut.wrapper_state!==1) fail("op15 abort test did not enter WR_WAIT");
  issue(4,0); finish_inner(294,0);
  wait(dut.wrapper_state==0 && result0[9]);
  if(!dut.bulk_scan_program_started || dut.bulk_scan_program_done || !bulk_header[211])
   fail("aborted op15 retained success or lost poison");
  inner_toggle_before=dut.inner_command[63];
  issue(15,{1'b0,16'hF004,3'd0,28'd0,5'd1,5'd7});
  if(!result0[10] || dut.inner_command[63]!==inner_toggle_before)
   fail("aborted op15 was replayable");
  inner_toggle_before=dut.inner_command[63];
  issue(14,{1'b1,16'hF004,1'b0,15'd0,15'd2400,5'd1,5'd7});
  if(!result0[10] || dut.inner_command[63]!==inner_toggle_before)
   fail("aborted op15 authorized phase1");

  // The approved guarded profile is sentinel-first with a 2400-clock pre.
  // Wrong hold, wrong physical order, or nonzero reserved payload must fail
  // before the inner op8 can toggle.
  run_block(0,1,16'hF005,5'd7,5'd1,15'd2399,295);
  inner_toggle_before=dut.inner_command[63];
  issue(15,{1'b0,16'hF005,3'd0,28'd0,5'd1,5'd7});
  if(!result0[10] || dut.inner_command[63]!==inner_toggle_before)
   fail("wrong-hold op15 reached inner core");
  run_block(0,0,16'hF006,5'd7,5'd1,15'd2400,305);
  inner_toggle_before=dut.inner_command[63];
  issue(15,{1'b0,16'hF006,3'd0,28'd0,5'd1,5'd7});
  if(!result0[10] || dut.inner_command[63]!==inner_toggle_before)
   fail("target-first pre authorized guarded op15");
  run_block(0,1,16'hF007,5'd7,5'd1,15'd2400,315);
  inner_toggle_before=dut.inner_command[63];
  issue(15,{1'b0,16'hF007,3'd7,28'd0,5'd1,5'd7});
  if(!result0[10] || dut.inner_command[63]!==inner_toggle_before)
   fail("reserved op15 payload reached inner core");

  // Direct outer op1 is conservatively treated as a possible reset/program
  // because rails determine its physical meaning. It remains available, but
  // accepting it poisons the open evidence epoch before any later WB/post.
  run_block(0,0,16'hF008,5'd7,5'd1,15'd2400,325);
  issue(1,58'd0);
  if(!bulk_header[211]) fail("direct op1 acceptance did not poison epoch");
  finish_inner(334,0); wait(dut.wrapper_state==0 && result0[9]);
  inner_toggle_before=dut.inner_command[63];
  issue(3,{1'b1,16'hF008,1'b0,8'h80,2'b11,5'd7,5'd1,20'h2AAF0});
  if(!result0[10] || dut.inner_command[63]!==inner_toggle_before)
   fail("direct op1 epoch authorized matched WB");
  run_block(0,0,16'hF009,5'd7,5'd1,15'd2400,335);
  issue(1,58'd0); finish_inner(344,0);
  wait(dut.wrapper_state==0 && result0[9]);
  inner_toggle_before=dut.inner_command[63];
  issue(14,{1'b1,16'hF009,1'b0,15'd0,15'd2400,5'd1,5'd7});
  if(!result0[10] || dut.inner_command[63]!==inner_toggle_before)
   fail("direct op1 epoch authorized phase1");

  // A successful DAC/profile op2 is the intended pre-to-program transition
  // and must not poison the epoch. A faulting op2 is tested separately below.
  mock_wb_valid=15'h7FFF; mock_wb_crc=32'h13579BDF;
  run_block(0,0,16'hF00A,5'd7,5'd1,15'd2400,345);
  issue(2,58'd1048576); finish_inner(354,0);
  wait(dut.wrapper_state==0 && result0[9]);
  if(bulk_header[211]) fail("successful op2 poisoned clean epoch");
  issue(3,{1'b1,16'hF00A,1'b0,8'h80,2'b11,5'd7,5'd1,20'h2AAF0});
  if(dut.wrapper_state!==1) fail("successful op2 blocked matched WB");
  finish_wb(dut.inner_command[31:0],0,0);
  wait(dut.wrapper_state==0 && result0[9]);
  if(!bulk_header[209]) fail("post-op2 matched WB did not complete");

  // A matched WB authorization is one-shot. Publish one exact-echo result,
  // then prove a second program command cannot toggle the inner core.
  mock_wb_valid=15'h7FFF; mock_wb_crc=32'h13579BDF;
  run_block(0,0,16'h3333,5'd7,5'd1,15'd2400,250);
  issue(3,{1'b1,16'h3333,1'b0,8'h80,2'b11,5'd7,5'd1,20'h2AAF0});
  if(bulk_header[0]) fail("header valid stayed high during WB WR_WAIT");
  finish_wb(dut.inner_command[31:0],0,0);
  wait(dut.wrapper_state==0 && result0[9]);
  if(!bulk_header[0] || !bulk_header[214] || !bulk_header[209] ||
     bulk_header[215] || bulk_header[211])
   fail("exact WB echo did not publish valid metadata");
  inner_toggle_before=dut.inner_command[63];
  issue(3,{1'b1,16'h3333,1'b0,8'h80,2'b11,5'd7,5'd1,20'h2AAF0});
  if(!result0[10] || dut.inner_command[63]!==inner_toggle_before)
   fail("second matched WB reused one pre authorization");

  // A seen but nonmatching packet echo must never claim WB success.
  run_block(0,0,16'h4444,5'd7,5'd1,15'd2400,275);
  issue(3,{1'b1,16'h4444,1'b0,8'h80,2'b11,5'd7,5'd1,20'h2AAF0});
  finish_wb(dut.inner_command[31:0]^32'h1,0,0);
  wait(dut.wrapper_state==0 && result0[9]);
  if(!bulk_header[0] || bulk_header[214] || bulk_header[209] ||
     !bulk_header[215] || !bulk_header[211])
   fail("mismatched WB echo claimed success");

  // A clean exact-echo WB is the only matched-WB path that may proceed to a
  // post block. This also proves the wb_started&&!wb_done guard releases on
  // the same atomic publication as wb_done.
  run_block(0,0,16'h5555,5'd7,5'd1,15'd2400,290);
  issue(3,{1'b1,16'h5555,1'b0,8'h80,2'b11,5'd7,5'd1,20'h2AAF0});
  finish_wb(dut.inner_command[31:0],0,0);
  wait(dut.wrapper_state==0 && result0[9]);
  if(!bulk_header[209]) fail("clean matched WB did not publish done");
  run_block(1,0,16'h5555,5'd7,5'd1,15'd2400,310);
  if(!bulk_header[210] || bulk_header[54:46]!==9'h1FF)
   fail("clean WB epoch did not authorize phase1");

  // A control-enable drop breaks ownership/reset provenance. A formerly clean
  // pre bank must not authorize either WB or post after re-enable.
  run_block(0,0,16'hD111,5'd7,5'd1,15'd2400,330);
  @(negedge clk); command={toggle,1'b0,62'd0}; @(posedge clk); #1;
  if(!bulk_header[211]) fail("enable drop did not poison clean epoch");
  inner_toggle_before=dut.inner_command[63];
  issue(3,{1'b1,16'hD111,1'b0,8'h80,2'b11,5'd7,5'd1,20'h2AAF0});
  if(!result0[10] || dut.inner_command[63]!==inner_toggle_before)
   fail("enable-dropped epoch authorized matched WB");
  run_block(0,0,16'hD112,5'd7,5'd1,15'd2400,340);
  @(negedge clk); command={toggle,1'b0,62'd0}; @(posedge clk); #1;
  inner_toggle_before=dut.inner_command[63];
  issue(14,{1'b1,16'hD112,1'b0,15'd0,15'd2400,5'd1,5'd7});
  if(!result0[10] || dut.inner_command[63]!==inner_toggle_before)
   fail("enable-dropped epoch authorized phase1");

  // Dropping enable during WB clears every publishable WB field, retains the
  // consumed one-shot authorization, and prevents replay after re-enable.
  run_block(0,0,16'hD222,5'd7,5'd1,15'd2400,350);
  issue(3,{1'b1,16'hD222,1'b0,8'h80,2'b11,5'd7,5'd1,20'h2AAF0});
  if(dut.wrapper_state!==1) fail("enable-drop WB test did not enter WR_WAIT");
  @(negedge clk); command={toggle,1'b0,62'd0}; @(posedge clk); #1;
  if(!bulk_header[211] || bulk_header[209] || bulk_header[45:31]!==0 ||
     bulk_header[127:96]!==0 || bulk_header[214] || bulk_header[215])
   fail("in-flight WB enable drop retained publishable evidence");
  inner_toggle_before=dut.inner_command[63];
  issue(3,{1'b1,16'hD222,1'b0,8'h80,2'b11,5'd7,5'd1,20'h2AAF0});
  if(!result0[10] || dut.inner_command[63]!==inner_toggle_before)
   fail("enable-dropped in-flight WB was replayable");

  // Faults from any intervening inner operation poison an open pre epoch.
  // This covers restore DACs and legacy scan-program/profile operations, not
  // only the matched WB path.
  run_block(0,0,16'hE111,5'd7,5'd1,15'd2400,370);
  issue(2,58'd1048576); finish_generic_fault();
  wait(dut.wrapper_state==0 && result0[9]);
  if(!bulk_header[211]) fail("generic inner fault did not poison epoch");
  inner_toggle_before=dut.inner_command[63];
  issue(3,{1'b1,16'hE111,1'b0,8'h80,2'b11,5'd7,5'd1,20'h2AAF0});
  if(!result0[10] || dut.inner_command[63]!==inner_toggle_before)
   fail("generic-fault epoch authorized matched WB");
  run_block(0,0,16'hE112,5'd7,5'd1,15'd2400,380);
  issue(2,58'd1048576); finish_generic_fault();
  wait(dut.wrapper_state==0 && result0[9]);
  inner_toggle_before=dut.inner_command[63];
  issue(14,{1'b1,16'hE112,1'b0,15'd0,15'd2400,5'd1,5'd7});
  if(!result0[10] || dut.inner_command[63]!==inner_toggle_before)
   fail("generic-fault epoch authorized phase1");
  run_block(0,0,16'hE113,5'd7,5'd1,15'd2400,390);
  issue(2,58'd1048576);
  issue(4,0);
  if(dut.wrapper_state!==4) fail("generic op4 did not enter WR_ABORT");
  finish_inner(398,0);
  wait(dut.wrapper_state==0 && result0[9]);
  if(!bulk_header[211]) fail("generic op4 abort did not poison epoch");
  inner_toggle_before=dut.inner_command[63];
  issue(3,{1'b1,16'hE113,1'b0,8'h80,2'b11,5'd7,5'd1,20'h2AAF0});
  if(!result0[10] || dut.inner_command[63]!==inner_toggle_before)
   fail("generic-abort epoch authorized matched WB");

  // Abort at every possible physical read index; no valid bank may publish.
  for(i=0;i<9;i=i+1) abort_at(i);

  // The next locally-opened phase0 must clear the stale abort fault and make
  // recovery possible without reloading the FPGA.
  run_block(0,0,16'h8888,5'd7,5'd1,15'd2400,400);
  if(bulk_header[211] || !bulk_header[208] || bulk_header[63:55]!==9'h1FF)
   fail("fresh phase0 did not recover stale abort fault");

  // Global op4 priority must stop an in-flight matched WB program before the
  // original op3 completes. The wrapper must forward a new toggle/op4 to the
  // core rather than waiting for the potentially destructive transaction.
  issue(3,{1'b1,16'h8888,1'b0,8'h80,2'b11,5'd7,5'd1,20'h2AAF0});
  if(dut.wrapper_state!==1 || dut.inner_command[61:58]!==3 || bulk_header[0])
   fail("matched WB did not enter WR_WAIT");
  inner_toggle_before=dut.inner_command[63];
  issue(4,0);
  if(dut.wrapper_state!==4 || dut.inner_command[61:58]!==4 || bulk_header[0] ||
     dut.inner_command[63]===inner_toggle_before)
   fail("op4 did not preempt in-flight WR_WAIT WB");
  finish_inner(500,0);
  wait(dut.wrapper_state==0 && result0[9]);
  if(!bulk_header[211] || bulk_header[209] || bulk_header[45:31]!==0)
   fail("aborted WB published valid bulk evidence");
  inner_toggle_before=dut.inner_command[63];
  issue(3,{1'b1,16'h8888,1'b0,8'h80,2'b11,5'd7,5'd1,20'h2AAF0});
  if(!result0[10] || dut.inner_command[63]!==inner_toggle_before)
   fail("aborted matched WB authorization was reusable");
  inner_toggle_before=dut.inner_command[63];
  issue(14,{1'b1,16'h8888,1'b0,15'd0,15'd2400,5'd1,5'd7});
  if(!result0[10] || dut.inner_command[63]!==inner_toggle_before)
   fail("aborted WB authorized phase1");

  // WR_ADVANCE must also be preempted before it can issue the next program
  // step. This covers the scheduler gap independently of WR_WAIT.
  issue(9,0);
  if(dut.wrapper_state!==5) fail("ramp did not enter WR_ADVANCE");
  inner_toggle_before=dut.inner_command[63];
  issue(4,0);
  if(dut.wrapper_state!==4 || dut.inner_command[61:58]!==4 ||
     dut.inner_command[63]===inner_toggle_before)
   fail("op4 did not preempt WR_ADVANCE");
  finish_inner(501,0);
  wait(dut.wrapper_state==0 && result0[9]);

  // Clear the deliberate abort fault before the standalone-WB case.
  run_block(0,0,16'h9999,5'd7,5'd1,15'd2400,600);
  if(bulk_header[211]) fail("post-abort phase0 recovery failed");

  // Standalone WB open_epoch creates a new epoch and retains packed TDC facts.
  mock_wb_words=480'h0000000F0000000E0000000D0000000C0000000B0000000A000000090000000800000007000000060000000500000004000000030000000200000001;
  mock_wb_valid=15'h7FFF; mock_wb_crc=32'h89ABCDEF;
  issue(3,{1'b0,16'hABCD,1'b1,8'h80,2'b01,5'd12,5'd3,20'h2AAFF});
  if(bulk_header[0]) fail("standalone WB exposed valid header while active");
  finish_wb(dut.inner_command[31:0],0,0);
  wait(dut.wrapper_state==0 && result0[9]);
  if(bulk_header[175:160]!==16'hABCD || !bulk_header[209] ||
     bulk_header[45:31]!==15'h7FFF || bulk_header[127:96]!==32'h89ABCDEF ||
     bulk_header[10:6]!==12 || bulk_header[5:1]!==3 || !bulk_header[0] ||
     bulk_tdc_words!==mock_wb_words) fail("standalone WB bulk epoch invalid");

  if(errors==0) $display("TB_BULK_TRANSPORT_PASS");
  else $display("TB_BULK_TRANSPORT_FAIL errors=%0d",errors);
  $finish(errors!=0);
 end
endmodule
