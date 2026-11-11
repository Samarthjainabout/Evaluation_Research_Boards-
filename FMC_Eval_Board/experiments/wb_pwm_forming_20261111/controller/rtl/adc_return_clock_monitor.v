`timescale 1ns/1ps
// Passive edge counters clocked by a dedicated 200 MHz PLL output.
// The 1 ms observation window expects 60 MHz LCLK and 10 MHz ADCLK.
// These do not use the ADC clock network or drive any physical pin.
module adc_return_clock_monitor #(
    parameter integer WINDOW_CYCLES = 200000
) (
    input wire clk,
    input wire enable,
    input wire lclk_async,
    input wire adclk_async,
    output reg window_valid = 0,
    output reg lclk_seen = 0,
    output reg adclk_seen = 0,
    output reg lclk_60m = 0,
    output reg adclk_10m = 0
);
    (* ASYNC_REG = "TRUE" *) reg [1:0] lclk_sync = 0;
    (* ASYNC_REG = "TRUE" *) reg [1:0] adclk_sync = 0;
    reg lclk_previous = 0, adclk_previous = 0;
    reg [31:0] window_count = 0;
    reg [31:0] lclk_edges = 0, adclk_edges = 0;
    wire lclk_rising = lclk_sync[1] && !lclk_previous;
    wire adclk_rising = adclk_sync[1] && !adclk_previous;
    wire [31:0] lclk_total = lclk_edges + lclk_rising;
    wire [31:0] adclk_total = adclk_edges + adclk_rising;
    localparam integer LCLK_EXPECTED = WINDOW_CYCLES * 3 / 10;
    localparam integer ADCLK_EXPECTED = WINDOW_CYCLES / 20;
    localparam integer LCLK_MARGIN = LCLK_EXPECTED / 50 + 2;
    localparam integer ADCLK_MARGIN = ADCLK_EXPECTED / 50 + 2;
    always @(posedge clk) begin
        lclk_sync <= {lclk_sync[0], lclk_async};
        adclk_sync <= {adclk_sync[0], adclk_async};
        lclk_previous <= lclk_sync[1];
        adclk_previous <= adclk_sync[1];
        if (!enable) begin
            window_count <= 0;
            lclk_edges <= 0;
            adclk_edges <= 0;
            window_valid <= 0;
            lclk_seen <= 0;
            adclk_seen <= 0;
            lclk_60m <= 0;
            adclk_10m <= 0;
        end else if (window_count == WINDOW_CYCLES-1) begin
            window_count <= 0;
            lclk_edges <= 0;
            adclk_edges <= 0;
            window_valid <= 1;
            // Avoid routing the full 32-bit incrementer through the nonzero
            // status test.  These expressions are logically identical to
            // (edge_total != 0) and keep the 200 MHz monitor path shallow.
            lclk_seen <= (lclk_edges != 0) || lclk_rising;
            adclk_seen <= (adclk_edges != 0) || adclk_rising;
            // Do not put the asynchronous-edge bit through a 32-bit increment
            // carry chain and then both range comparators in one 200-MHz path.
            // Select between the two equivalent registered-count ranges.
            lclk_60m <= lclk_rising ?
                (lclk_edges >= LCLK_EXPECTED-LCLK_MARGIN-1 &&
                 lclk_edges <= LCLK_EXPECTED+LCLK_MARGIN-1) :
                (lclk_edges >= LCLK_EXPECTED-LCLK_MARGIN &&
                 lclk_edges <= LCLK_EXPECTED+LCLK_MARGIN);
            adclk_10m <= adclk_rising ?
                (adclk_edges >= ADCLK_EXPECTED-ADCLK_MARGIN-1 &&
                 adclk_edges <= ADCLK_EXPECTED+ADCLK_MARGIN-1) :
                (adclk_edges >= ADCLK_EXPECTED-ADCLK_MARGIN &&
                 adclk_edges <= ADCLK_EXPECTED+ADCLK_MARGIN);
        end else begin
            window_count <= window_count + 1'b1;
            lclk_edges <= lclk_total;
            adclk_edges <= adclk_total;
        end
    end
endmodule
