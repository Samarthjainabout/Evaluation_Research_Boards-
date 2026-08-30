`timescale 1ns / 1ps

// DAC81416 power-up and rail programming controller.
//
// The DAC samples SDI on falling SCLK edges.  With the 10 MHz wb clock used
// by the scan driver, this controller produces a 5 MHz SPI mode-1 waveform.
// Each write is a 24-bit {register, data} frame.  The frame list mirrors the
// proven Teensy initialization and then writes all sixteen output channels.
module dac81416_spi #(
    parameter integer VCC_SET_MV = 500,
    parameter integer VCC_WL_SET_MV = 2500
) (
    input  wire clk_i,
    output reg  sclk_o = 1'b0,
    output reg  sdi_o = 1'b0,
    output reg  cs_n_o = 1'b1,
    output wire ldac_n_o,
    output reg  ready_o = 1'b0
);
    localparam [2:0] SPI_LOAD = 3'd0;
    localparam [2:0] SPI_RISE = 3'd1;
    localparam [2:0] SPI_FALL = 3'd2;
    localparam [2:0] SPI_END  = 3'd3;
    localparam [2:0] SPI_DONE = 3'd4;

    // Straight-binary codes matching the ranges programmed below.
    localparam integer VCC_SET_CLAMP_MV =
        (VCC_SET_MV < 0) ? 0 : ((VCC_SET_MV > 10000) ? 10000 : VCC_SET_MV);
    localparam integer VCC_WL_SET_CLAMP_MV =
        (VCC_WL_SET_MV < 0) ? 0 : ((VCC_WL_SET_MV > 5000) ? 5000 : VCC_WL_SET_MV);
    localparam [15:0] VCC_SET_CODE = ((VCC_SET_CLAMP_MV * 65535) + 5000) / 10000;
    localparam [15:0] VCC_WL_SET_CODE = ((VCC_WL_SET_CLAMP_MV * 65535) + 2500) / 5000;

    reg [2:0] state = SPI_LOAD;
    reg [4:0] frame_index = 5'd0;
    reg [4:0] bit_index = 5'd23;
    reg [23:0] frame = 24'd0;

    // Channels stay asynchronous, so LDAC is held inactive.  The pin remains
    // routed to the FPGA for future simultaneous-update operation.
    assign ldac_n_o = 1'b1;

    function [23:0] frame_for_index;
        input [4:0] index;
        begin
            case (index)
                // DAC range registers.  The nibbles reproduce the existing
                // bench configuration: channels 3/7/11/15 etc. use 0..5 V,
                // while channels 2/6/10/14 etc. use 0..10 V.
                5'd0:  frame_for_index = 24'h0A0101;
                5'd1:  frame_for_index = 24'h0B0101;
                5'd2:  frame_for_index = 24'h0C0101;
                5'd3:  frame_for_index = 24'h0D0000;
                // Active device, CRC/streaming disabled, SDO enabled.
                5'd4:  frame_for_index = 24'h030004;
                5'd5:  frame_for_index = 24'h0D0000;
                // Power up every channel and retain broadcast enable defaults.
                5'd6:  frame_for_index = 24'h090000;
                5'd7:  frame_for_index = 24'h05FFFF;

                // DAC0..DAC15.  Variable scan rails are DAC3 and DAC6.
                5'd8:  frame_for_index = 24'h100000;                  // DAC0
                5'd9:  frame_for_index = 24'h110000;                  // DAC1
                5'd10: frame_for_index = 24'h120000;                  // DAC2
                5'd11: frame_for_index = {8'h13, VCC_WL_SET_CODE};     // DAC3
                5'd12: frame_for_index = 24'h140000;                  // DAC4
                5'd13: frame_for_index = 24'h150000;                  // DAC5
                5'd14: frame_for_index = {8'h16, VCC_SET_CODE};        // DAC6
                5'd15: frame_for_index = 24'h17CCCC;                  // DAC7  4.0 V / 5 V
                5'd16: frame_for_index = 24'h180000;                  // DAC8
                5'd17: frame_for_index = 24'h190CCD;                  // DAC9  legacy 5% code
                5'd18: frame_for_index = 24'h1A170A;                  // DAC10 legacy 9% code
                5'd19: frame_for_index = 24'h1B0F5C;                  // DAC11 legacy 6% code
                5'd20: frame_for_index = 24'h1C28F6;                  // DAC12 legacy 16% code
                5'd21: frame_for_index = 24'h1DFFFF;                  // DAC13 5.0 V / 5 V
                5'd22: frame_for_index = 24'h1E0000;                  // DAC14
                5'd23: frame_for_index = 24'h1F6B85;                  // DAC15 2.1 V / 5 V
                default: frame_for_index = 24'h000000;
            endcase
        end
    endfunction

    always @(posedge clk_i) begin
        case (state)
            SPI_LOAD: begin
                frame     <= frame_for_index(frame_index);
                bit_index <= 5'd23;
                sclk_o    <= 1'b0;
                sdi_o     <= 1'b0;
                cs_n_o    <= 1'b0;
                state     <= SPI_RISE;
            end

            SPI_RISE: begin
                // Mode 1 changes data on the rising edge and the DAC samples
                // it on the following falling edge.
                sdi_o  <= frame[bit_index];
                sclk_o <= 1'b1;
                state  <= SPI_FALL;
            end

            SPI_FALL: begin
                sclk_o <= 1'b0;
                if (bit_index == 5'd0) begin
                    state <= SPI_END;
                end else begin
                    bit_index <= bit_index - 5'd1;
                    state <= SPI_RISE;
                end
            end

            SPI_END: begin
                cs_n_o <= 1'b1;
                sdi_o  <= 1'b0;
                if (frame_index == 5'd23) begin
                    ready_o <= 1'b1;
                    state <= SPI_DONE;
                end else begin
                    frame_index <= frame_index + 5'd1;
                    state <= SPI_LOAD;
                end
            end

            default: begin
                sclk_o  <= 1'b0;
                sdi_o   <= 1'b0;
                cs_n_o  <= 1'b1;
                ready_o <= 1'b1;
                state   <= SPI_DONE;
            end
        endcase
    end
endmodule
