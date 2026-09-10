`timescale 1ns / 1ps

// Small 8-N-1 UART receiver for the Caravel management UART.  The first byte
// of every result frame is 0xA5, whose start bit is followed by a high data
// bit.  That clean first low pulse lets the receiver measure the actual UART
// bit period, whether Caravel emits nominal 9600 baud or scales it with XCLK.
module uart_rx_8n1 #(
    parameter integer CLOCK_HZ = 10000000,
    parameter integer BAUD = 9600
) (
    input  wire       clk_i,
    input  wire       reset_i,
    input  wire       rx_i,
    output reg  [7:0] byte_o = 8'd0,
    output reg        byte_valid_o = 1'b0,
    output reg        framing_error_o = 1'b0
);
    localparam integer NOMINAL_CLKS_PER_BIT = (CLOCK_HZ + (BAUD / 2)) / BAUD;

    localparam [2:0] RX_IDLE      = 3'd0;
    localparam [2:0] RX_CALIBRATE = 3'd1;
    localparam [2:0] RX_START     = 3'd2;
    localparam [2:0] RX_DATA      = 3'd3;
    localparam [2:0] RX_STOP      = 3'd4;

    reg rx_meta = 1'b1;
    reg rx_sync = 1'b1;
    reg [2:0] state = RX_IDLE;
    reg [15:0] clock_count = 16'd0;
    reg [15:0] clocks_per_bit = NOMINAL_CLKS_PER_BIT;
    reg [15:0] start_low_count = 16'd0;
    reg timing_valid = 1'b0;
    reg [2:0] bit_index = 3'd0;
    reg [7:0] shift = 8'd0;
    reg [5:0] idle_high_count = 6'd0;
    reg armed = 1'b0;

    always @(posedge clk_i) begin
        rx_meta <= rx_i;
        rx_sync <= rx_meta;
        byte_valid_o <= 1'b0;
        framing_error_o <= 1'b0;

        if (reset_i) begin
            state <= RX_IDLE;
            clock_count <= 16'd0;
            clocks_per_bit <= NOMINAL_CLKS_PER_BIT;
            start_low_count <= 16'd0;
            timing_valid <= 1'b0;
            bit_index <= 3'd0;
            shift <= 8'd0;
            byte_o <= 8'd0;
            idle_high_count <= 6'd0;
            armed <= 1'b0;
        end else if (!armed) begin
            state <= RX_IDLE;
            timing_valid <= 1'b0;
            if (rx_sync) begin
                if (idle_high_count == 6'd31) begin
                    armed <= 1'b1;
                    idle_high_count <= 6'd0;
                end else begin
                    idle_high_count <= idle_high_count + 1'b1;
                end
            end else begin
                idle_high_count <= 6'd0;
            end
        end else case (state)
            RX_IDLE: begin
                clock_count <= 16'd0;
                bit_index <= 3'd0;
                if (!rx_sync) begin
                    if (timing_valid) begin
                        clock_count <= (clocks_per_bit >> 1) - 1'b1;
                        state <= RX_START;
                    end else begin
                        start_low_count <= 16'd1;
                        state <= RX_CALIBRATE;
                    end
                end
            end

            RX_CALIBRATE: begin
                // The first frame byte is 0xA5, so its start low lasts exactly
                // one bit and rises at data bit zero.  Measure that interval.
                if (!rx_sync) begin
                    if (start_low_count != 16'hFFFF)
                        start_low_count <= start_low_count + 1'b1;
                end else if (start_low_count >= 16'd8 && start_low_count != 16'hFFFF) begin
                    clocks_per_bit <= start_low_count;
                    clock_count <= (start_low_count >> 1) - 1'b1;
                    bit_index <= 3'd0;
                    state <= RX_DATA;
                end else begin
                    state <= RX_IDLE;
                end
            end

            RX_START: begin
                if (clock_count != 0) begin
                    clock_count <= clock_count - 1'b1;
                end else if (!rx_sync) begin
                    clock_count <= clocks_per_bit - 1'b1;
                    bit_index <= 3'd0;
                    state <= RX_DATA;
                end else begin
                    state <= RX_IDLE;
                end
            end

            RX_DATA: begin
                if (clock_count != 0) begin
                    clock_count <= clock_count - 1'b1;
                end else begin
                    shift[bit_index] <= rx_sync;
                    clock_count <= clocks_per_bit - 1'b1;
                    if (bit_index == 3'd7) begin
                        state <= RX_STOP;
                    end else begin
                        bit_index <= bit_index + 1'b1;
                    end
                end
            end

            RX_STOP: begin
                if (clock_count != 0) begin
                    clock_count <= clock_count - 1'b1;
                end else begin
                    if (rx_sync) begin
                        byte_o <= shift;
                        byte_valid_o <= 1'b1;
                        timing_valid <= 1'b1;
                    end else begin
                        framing_error_o <= 1'b1;
                        timing_valid <= 1'b0;
                    end
                    state <= RX_IDLE;
                end
            end

            default: state <= RX_IDLE;
        endcase
    end
endmodule
