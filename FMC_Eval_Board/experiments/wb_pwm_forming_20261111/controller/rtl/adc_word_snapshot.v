`timescale 1ns/1ps
// Source packets arrive every 100 ns. The source holds each 60-bit packet
// while its toggle passes through a three-stage 50 MHz synchronizer.
// The destination latches at least three destination cycles after launch.
// This transfers every 10 MSPS word with coherent bits, without clocking ILA
// from a returned clock that is absent during ADC reset/startup.
module adc_word_snapshot (
    input wire source_clk,
    input wire source_valid,
    input wire [59:0] source_words,
    input wire destination_clk,
    output reg [59:0] destination_words = 0,
    output reg destination_valid = 0
);
    reg [59:0] held_words = 0;
    reg source_toggle = 0;
    (* ASYNC_REG = "TRUE" *) reg [2:0] toggle_sync = 0;
    reg previous_toggle = 0;
    always @(posedge source_clk) begin
        if (source_valid) begin
            held_words <= source_words;
            source_toggle <= ~source_toggle;
        end
    end
    always @(posedge destination_clk) begin
        toggle_sync <= {toggle_sync[1:0], source_toggle};
        previous_toggle <= toggle_sync[2];
        destination_valid <= 0;
        if (toggle_sync[2] != previous_toggle) begin
            destination_words <= held_words;
            destination_valid <= 1;
        end
    end
endmodule
