`timescale 1ns / 1ps

// DAC81416 controller for the universal scan-debug bitstream.
//
// At FPGA configuration it restores the complete bench DAC setup.  Later,
// update_i rewrites only DAC3 (Vcc_wl_set) and DAC6 (Vcc_set), allowing the
// API to change pulse rails over JTAG/VIO without rebuilding the bitstream.
module dac81416_runtime_spi (
    input  wire        clk_i,
    input  wire        update_i,
    input  wire [15:0] vcc_set_code_i,
    input  wire [15:0] vcc_wl_set_code_i,
    output reg         sclk_o = 1'b0,
    output reg         sdi_o = 1'b0,
    output reg         cs_n_o = 1'b1,
    output wire        ldac_n_o,
    output reg         initialized_o = 1'b0,
    output reg         ready_o = 1'b0
);
    localparam [2:0] SPI_IDLE = 3'd0;
    localparam [2:0] SPI_LOAD = 3'd1;
    localparam [2:0] SPI_RISE = 3'd2;
    localparam [2:0] SPI_FALL = 3'd3;
    localparam [2:0] SPI_END  = 3'd4;

    // Power-up defaults: Vcc_set=0.5 V on the 0..10 V range and
    // Vcc_wl_set=2.5 V on the 0..5 V range.
    localparam [15:0] DEFAULT_VCC_SET_CODE = 16'h0CCD;
    localparam [15:0] DEFAULT_VCC_WL_CODE  = 16'h8000;

    reg [2:0] state = SPI_LOAD;
    reg [4:0] frame_index = 5'd0;
    reg [4:0] last_frame_index = 5'd23;
    reg [4:0] bit_index = 5'd23;
    reg [23:0] frame = 24'd0;
    reg runtime_update = 1'b0;
    reg [15:0] vcc_set_code = DEFAULT_VCC_SET_CODE;
    reg [15:0] vcc_wl_set_code = DEFAULT_VCC_WL_CODE;

    // Channels operate asynchronously, so LDAC remains inactive/high.
    assign ldac_n_o = 1'b1;

    function [23:0] initialization_frame;
        input [4:0] index;
        begin
            case (index)
                5'd0:  initialization_frame = 24'h0A0101;
                5'd1:  initialization_frame = 24'h0B0101;
                5'd2:  initialization_frame = 24'h0C0101;
                5'd3:  initialization_frame = 24'h0D0000;
                5'd4:  initialization_frame = 24'h030004;
                5'd5:  initialization_frame = 24'h0D0000;
                5'd6:  initialization_frame = 24'h090000;
                5'd7:  initialization_frame = 24'h05FFFF;
                5'd8:  initialization_frame = 24'h100000;
                5'd9:  initialization_frame = 24'h110000;
                5'd10: initialization_frame = 24'h120000;
                5'd11: initialization_frame = {8'h13, DEFAULT_VCC_WL_CODE};
                5'd12: initialization_frame = 24'h140000;
                5'd13: initialization_frame = 24'h150000;
                5'd14: initialization_frame = {8'h16, DEFAULT_VCC_SET_CODE};
                5'd15: initialization_frame = 24'h17CCCC;
                5'd16: initialization_frame = 24'h180000;
                5'd17: initialization_frame = 24'h190CCD;
                5'd18: initialization_frame = 24'h1A170A;
                5'd19: initialization_frame = 24'h1B0F5C;
                5'd20: initialization_frame = 24'h1C28F6;
                5'd21: initialization_frame = 24'h1DFFFF;
                5'd22: initialization_frame = 24'h1E0000;
                5'd23: initialization_frame = 24'h1F6B85;
                default: initialization_frame = 24'h000000;
            endcase
        end
    endfunction

    function [23:0] update_frame;
        input [4:0] index;
        begin
            case (index)
                5'd0: update_frame = {8'h13, vcc_wl_set_code};
                5'd1: update_frame = {8'h16, vcc_set_code};
                default: update_frame = 24'h000000;
            endcase
        end
    endfunction

    always @(posedge clk_i) begin
        case (state)
            SPI_IDLE: begin
                sclk_o <= 1'b0;
                sdi_o  <= 1'b0;
                cs_n_o <= 1'b1;
                if (update_i) begin
                    vcc_set_code    <= vcc_set_code_i;
                    vcc_wl_set_code <= vcc_wl_set_code_i;
                    runtime_update  <= 1'b1;
                    frame_index     <= 5'd0;
                    last_frame_index <= 5'd1;
                    ready_o         <= 1'b0;
                    state           <= SPI_LOAD;
                end
            end

            SPI_LOAD: begin
                frame     <= runtime_update ? update_frame(frame_index) : initialization_frame(frame_index);
                bit_index <= 5'd23;
                sclk_o    <= 1'b0;
                sdi_o     <= 1'b0;
                cs_n_o    <= 1'b0;
                state     <= SPI_RISE;
            end

            SPI_RISE: begin
                // DAC81416 samples SDI on the falling SCLK edge (SPI mode 1).
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
                if (frame_index == last_frame_index) begin
                    initialized_o <= 1'b1;
                    ready_o        <= 1'b1;
                    runtime_update <= 1'b0;
                    state          <= SPI_IDLE;
                end else begin
                    frame_index <= frame_index + 5'd1;
                    state <= SPI_LOAD;
                end
            end

            default: begin
                sclk_o <= 1'b0;
                sdi_o  <= 1'b0;
                cs_n_o <= 1'b1;
                ready_o <= 1'b0;
                state <= SPI_LOAD;
            end
        endcase
    end
endmodule
