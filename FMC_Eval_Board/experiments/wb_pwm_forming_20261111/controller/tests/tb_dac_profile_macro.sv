`timescale 1ns/1ps

module tb_dac_profile_macro;
 reg clk=0, feedback_clk=0;
 reg ready_i=1, reset_i=1, uart_rx=1, dac_ready=1, adc_healthy=1;
 reg [63:0] command=0;
 reg sample_valid=0;
 reg [59:0] samples=0;
 reg dac_done=1;
 wire interface_enable, reset_low, tm, se, si, scan_tristate, si_tristate;
 wire dac_start;
 wire [23:0] dac_frame;
 wire [255:0] result0, result1;
 wire [479:0] wb_words_flat;
 wire [14:0] wb_words_valid;
 wire [31:0] wb_words_crc32;

 reg toggle=0;
 reg [23:0] observed[0:63];
 integer observed_count=0;
 integer errors=0;

 always #5 clk=~clk;
 always #13 feedback_clk=~feedback_clk;

 cell_control_core #(
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
  .result0(result0),.result1(result1),
  .wb_words_flat(wb_words_flat),.wb_words_valid(wb_words_valid),
  .wb_words_crc32(wb_words_crc32));

 always @(posedge clk) begin
  #1;
  if(dac_start) begin
   observed[observed_count]=dac_frame;
   observed_count=observed_count+1;
  end
 end

 task automatic fail(input [8*120-1:0] message);
  begin $display("FAIL %0s",message); errors=errors+1; end
 endtask

 function automatic [23:0] expected_frame(
  input [1:0] kind,input integer index,input [15:0] voltage);
  begin
   if(kind==0) begin
    case(index)
     0: expected_frame=24'h10199A;
     1: expected_frame=24'h118000;
     2: expected_frame=24'h12199A;
     3: expected_frame=24'h138000;
     4: expected_frame=24'h140000;
     5: expected_frame=24'h1575C3;
     6: expected_frame=24'h160000;
     7: expected_frame=24'h192E14;
     8: expected_frame=24'h1A2E14;
     9: expected_frame=24'h1B1EB8;
     10: expected_frame=24'h1C51EC;
     11: expected_frame=24'h1D3333;
     12: expected_frame=24'h0901D0;
     default: expected_frame=24'hFFFFFF;
    endcase
   end else begin
    case(index)
     0: expected_frame=24'h100000;
     1: expected_frame=24'h110000;
     2: expected_frame=kind==1 ? {8'h12,voltage} : 24'h120000;
     3: expected_frame=kind==1 ? {8'h13,voltage} : 24'h130000;
     4: expected_frame=kind==2 ? {8'h14,voltage} : 24'h140000;
     5: expected_frame=kind==2 ? {8'h15,voltage} : 24'h150000;
     6: expected_frame=24'h160000;
     7: expected_frame=kind==1 ? 24'h0901F3 : 24'h0901CF;
     8: expected_frame=24'h193333;
     9: expected_frame=24'h1A2E14;
     10: expected_frame=24'h1B1EB8;
     11: expected_frame=24'h1C51EC;
     12: expected_frame=24'h1D4CCD;
     default: expected_frame=24'hFFFFFF;
    endcase
   end
  end
 endfunction

 task automatic issue_profile(input [1:0] kind,input [15:0] voltage);
  reg [57:0] payload;
  integer start_count;
  integer i;
  begin
   start_count=observed_count;
   payload={10'h2AD,kind,voltage,30'd0};
   toggle=~toggle;
   @(negedge clk); command={toggle,1'b1,4'd2,payload};
   while(result0[15]!==toggle || !result0[9]) @(posedge clk);
   #2;
   if(result0[10]) fail("valid profile command faulted");
   if(observed_count-start_count!=13) fail("profile did not emit exactly thirteen frames");
   for(i=0;i<13;i=i+1)
    if(observed[start_count+i]!==expected_frame(kind,i,voltage)) begin
     $display("FAIL kind=%0d frame=%0d got=%06X expected=%06X",
              kind,i,observed[start_count+i],expected_frame(kind,i,voltage));
     errors=errors+1;
    end
   if(result1[255:224]!==32'h44504631 || result1[1:0]!==kind ||
      result1[5:2]!==4'd12 || result1[21:6]!==voltage ||
      result1[22]!==1'b0 || result1[23]!==1'b1)
    fail("profile completion receipt is incomplete or mismatched");
  end
 endtask

 task automatic issue_invalid(input [57:0] payload);
  integer start_count;
  begin
   start_count=observed_count;
   toggle=~toggle;
   @(negedge clk); command={toggle,1'b1,4'd2,payload};
   while(result0[15]!==toggle || !result0[9]) @(posedge clk);
   #2;
   if(!result0[10] || result0[14:11]!==4'd5)
    fail("invalid profile was not rejected with protected-DAC fault");
   if(observed_count!=start_count) fail("invalid profile emitted a DAC frame");
  end
 endtask

 initial begin
  repeat(4) @(posedge clk);
  issue_profile(0,16'h0000);
  issue_profile(1,16'h5A82); // 1.767 V, verifies dynamic SET code plumbing
  issue_profile(2,16'h70A4); // 2.200 V, verifies dynamic RESET code plumbing
  issue_invalid({10'h2AD,2'd0,16'h0001,30'd0}); // scan profile must be fixed
  issue_invalid({10'h2AD,2'd3,16'h0000,30'd0}); // reserved profile kind
  issue_invalid({10'h001,24'h10199A,24'd0});    // malformed legacy upper bits
  if(errors==0) $display("PASS dac_profile_macro frames=39 receipts=3 invalid=3");
  else $display("FAIL dac_profile_macro errors=%0d",errors);
  $finish(errors!=0);
 end
endmodule
