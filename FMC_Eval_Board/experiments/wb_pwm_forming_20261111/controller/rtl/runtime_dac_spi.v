`timescale 1ns/1ps
// DAC81416 samples on falling SCLK. CS stays low for exactly 24 edges.
module runtime_dac_spi #(parameter HALF=50, GAP=1000)(
 input wire clk, enable, start, input wire [23:0] data,
 output reg busy=0, done=0, output reg sclk=0, sdata=0, ncs=1
);
 reg [23:0] frame=0;
 reg [4:0] bit_index=23;
 reg [15:0] count=0;
 reg [1:0] state=0;
 always @(posedge clk) begin
  done<=0;
  if(!enable) begin busy<=0; sclk<=0; sdata<=0; ncs<=1; state<=0; end
  else case(state)
   0: if(start) begin
    frame<=data; bit_index<=23; sdata<=data[23]; ncs<=0; busy<=1;
    count<=HALF-1; state<=1;
   end
   1: if(count!=0) count<=count-1;
      else if(!sclk) begin sclk<=1; count<=HALF-1; end
      else begin
       sclk<=0; count<=HALF-1;
       if(bit_index==0) begin state<=2; end
       else begin bit_index<=bit_index-1; end
      end
   2: if(count!=0) count<=count-1;
      else begin ncs<=1; count<=GAP-1; state<=3; end
   3: if(count!=0) count<=count-1;
      else begin busy<=0; done<=1; state<=0; end
  endcase
  // Change data halfway through the low phase, after the previous falling edge.
  if(state==1 && !sclk && count==HALF/2) sdata<=frame[bit_index];
 end
endmodule
