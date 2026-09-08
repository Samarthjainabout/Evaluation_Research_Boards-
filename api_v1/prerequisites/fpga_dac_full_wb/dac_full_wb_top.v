`timescale 1ns/1ps
// One-shot complete WB bench DAC profile. All channels are explicitly loaded;
// unused channels remain powered down through register 0x09 = 0x4158.
module dac_full_wb_top(
    input wire wb_clk_i,
    output reg dac_sclk_o = 0, output reg dac_sdi_o = 0,
    output reg dac_cs_n_o = 1, output wire dac_ldac_n_o,
    output wire busy_o, output wire done_o
);
    reg [2:0] state=0;
    reg [4:0] index=0, bit_index=23;
    reg [23:0] frame=0;
    reg done=0;
    assign dac_ldac_n_o=1;
    assign busy_o=!done;
    assign done_o=done;
    function [23:0] profile_frame;
      input [4:0] n;
      begin case(n)
        0:  profile_frame=24'h09FFFF; // Disable all outputs while configuring.
        1:  profile_frame=24'h030004;
        2:  profile_frame=24'h060000; // Asynchronous updates.
        3:  profile_frame=24'h070000;
        4:  profile_frame=24'h080000;
        5:  profile_frame=24'h0A0000; // DAC15..12: 0..5 V.
        6:  profile_frame=24'h0B0000; // DAC11..8:  0..5 V.
        7:  profile_frame=24'h0C0000; // DAC7..4:   0..5 V.
        8:  profile_frame=24'h0D0000; // DAC3..0:   0..5 V.
        9:  profile_frame=24'h10199A; // DAC0  = 0.5 V.
        10: profile_frame=24'h118000; // DAC1  = 2.5 V.
        11: profile_frame=24'h1275C3; // DAC2  = 2.3 V.
        12: profile_frame=24'h130000; // DAC3  = 0 V, powered down.
        13: profile_frame=24'h140000; // DAC4  = 0 V, powered down.
        14: profile_frame=24'h1575C3; // DAC5  = 2.3 V.
        15: profile_frame=24'h160000; // DAC6  = 0 V, powered down.
        16: profile_frame=24'h17CCCC; // DAC7  = 4.0 V VDDIO.
        17: profile_frame=24'h180000; // DAC8  = 0 V, powered down.
        18: profile_frame=24'h19199A; // DAC9  = 0.5 V Iref.
        19: profile_frame=24'h1A2E14; // DAC10 = 0.9 V Vcomp.
        20: profile_frame=24'h1B1EB8; // DAC11 = 0.6 V Bias_comp2.
        21: profile_frame=24'h1C51EC; // DAC12 = 1.6 V Vbias.
        22: profile_frame=24'h1D3333; // DAC13 = 1.0 V dc_bias.
        23: profile_frame=24'h1E0000; // DAC14 = 0 V, powered down.
        24: profile_frame=24'h1F6B85; // DAC15 = 2.1 V VCCD2.
        25: profile_frame=24'h094158; // Activate 0,1,2,5,7,9..13,15.
        default: profile_frame=24'h000000;
      endcase end
    endfunction
    always @(posedge wb_clk_i) begin
      case(state)
        0: begin frame<=profile_frame(index); bit_index<=23; dac_cs_n_o<=0; dac_sclk_o<=0; state<=1; end
        1: begin dac_sdi_o<=frame[bit_index]; dac_sclk_o<=1; state<=2; end
        2: begin
          dac_sclk_o<=0;
          if(bit_index==0) state<=3;
          else begin bit_index<=bit_index-1'b1; state<=1; end
        end
        3: begin
          dac_cs_n_o<=1; dac_sdi_o<=0;
          if(index==25) begin done<=1; state<=4; end
          else begin index<=index+1'b1; state<=0; end
        end
        default: begin dac_cs_n_o<=1; dac_sclk_o<=0; dac_sdi_o<=0; end
      endcase
    end
endmodule
