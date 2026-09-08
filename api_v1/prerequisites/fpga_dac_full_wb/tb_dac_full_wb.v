`timescale 1ns/1ps
module tb_dac_full_wb;
  reg clk=0; always #250 clk=!clk;
  wire sclk,sdi,cs,ldac,busy,done;
  dac_full_wb_top dut(.wb_clk_i(clk),.dac_sclk_o(sclk),.dac_sdi_o(sdi),
    .dac_cs_n_o(cs),.dac_ldac_n_o(ldac),.busy_o(busy),.done_o(done));
  integer nframes=0,nbits=0;
  reg [23:0] spi=0;
  reg [23:0] expected[0:25];
  always @(negedge sclk) if(!cs) begin spi={spi[22:0],sdi}; nbits=nbits+1; end
  always @(posedge cs) if(nbits) begin
    if(nbits!=24 || nframes>25 || spi!==expected[nframes])
      $fatal(1,"SPI frame %0d=%h bits=%0d expected=%h",nframes,spi,nbits,expected[nframes]);
    nframes=nframes+1; nbits=0;
  end
  initial begin
    expected[0]=24'h09FFFF; expected[1]=24'h030004; expected[2]=24'h060000;
    expected[3]=24'h070000; expected[4]=24'h080000; expected[5]=24'h0A0000;
    expected[6]=24'h0B0000; expected[7]=24'h0C0000; expected[8]=24'h0D0000;
    expected[9]=24'h10199A; expected[10]=24'h118000; expected[11]=24'h1275C3;
    expected[12]=24'h130000; expected[13]=24'h140000; expected[14]=24'h1575C3;
    expected[15]=24'h160000; expected[16]=24'h17CCCC; expected[17]=24'h180000;
    expected[18]=24'h19199A; expected[19]=24'h1A2E14; expected[20]=24'h1B1EB8;
    expected[21]=24'h1C51EC; expected[22]=24'h1D3333; expected[23]=24'h1E0000;
    expected[24]=24'h1F6B85; expected[25]=24'h094158;
    wait(done); #1;
    if(nframes!=26 || busy || !ldac) $fatal(1,"Completion state failed");
    $display("PASS: complete 16-channel WB DAC profile; final mask 0x4158");
    $finish;
  end
  initial begin #10000000; $fatal(1,"Timeout"); end
endmodule
