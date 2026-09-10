`timescale 1ns / 1ps

// Safe first image for a powered-down bench. It configures the Si5351 to
// 2 MHz without touching DAC SPI, holds Caravel reset during clock startup,
// then releases reset so the permanent firmware can be flashed.
module caravel_clock_bootstrap #(
    parameter [31:0] RESET_HOLD_50M_CYCLES = 32'd600000
) (
    input  wire sys_clk_50m_i,
    inout  wire si5351_sda_io,
    output wire si5351_scl_io,
    output reg  caravel_resetb_o = 1'b0,
    output wire busy_o,
    output wire done_o
);
    wire clock_ready;
    wire clock_active_10m;
    wire clock_busy;
    wire clock_error;
    reg [31:0] reset_count = 32'd0;

    si5351_mode_controller clock_controller (
        .sys_clk_i(sys_clk_50m_i),
        .mode_10m_req_i(1'b0),
        .si5351_sda_io(si5351_sda_io),
        .si5351_scl_io(si5351_scl_io),
        .ready_o(clock_ready),
        .active_10m_o(clock_active_10m),
        .busy_o(clock_busy),
        .error_o(clock_error)
    );

    assign busy_o = clock_busy;
    assign done_o = clock_ready && !clock_error;

    always @(posedge sys_clk_50m_i) begin
        if (!clock_ready || clock_error) begin
            caravel_resetb_o <= 1'b0;
            reset_count <= 32'd0;
        end else if (reset_count + 32'd1 >= RESET_HOLD_50M_CYCLES) begin
            caravel_resetb_o <= 1'b1;
        end else begin
            reset_count <= reset_count + 32'd1;
        end
    end
endmodule
