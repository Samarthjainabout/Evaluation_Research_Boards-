`timescale 1ns / 1ps

// DAC81416 controller for the universal scan-debug bitstream.
//
// By default FPGA configuration restores the complete bench DAC setup.
// BOOT_INITIALIZE=0 starts with the SPI pins idle and preserves live DAC
// registers. Later, update_i can explicitly select a scan or WB profile.
module dac81416_runtime_spi #(
    // The WB/reset-capable runtime must not rewrite live DAC registers merely
    // because the FPGA bitstream is loaded. Runtime scan commands can still
    // request an explicit profile update through update_i.
    parameter BOOT_INITIALIZE = 1'b1,
    // Keep a conservative startup delay before any other DAC output is
    // enabled. At the current 200 kHz bench clock this is about 100 ms.
    parameter [31:0] VDDIO_STARTUP_DELAY_CYCLES = 32'd20000
) (
    input  wire        clk_i,
    input  wire        update_i,
    input  wire        wb_profile_i,
    input  wire [15:0] vcc_set_code_i,
    input  wire [15:0] vcc_wl_set_code_i,
    output reg         sclk_o = 1'b0,
    output reg         sdi_o = 1'b0,
    output reg         cs_n_o = 1'b1,
    output wire        ldac_n_o,
    output reg         initialized_o = BOOT_INITIALIZE ? 1'b0 : 1'b1,
    output reg         ready_o = BOOT_INITIALIZE ? 1'b0 : 1'b1
);
    localparam [2:0] SPI_IDLE = 3'd0;
    localparam [2:0] SPI_LOAD = 3'd1;
    localparam [2:0] SPI_RISE = 3'd2;
    localparam [2:0] SPI_FALL = 3'd3;
    localparam [2:0] SPI_END  = 3'd4;
    localparam [2:0] SPI_BOOT_DELAY = 3'd5;

    // Power-up defaults: Vcc_set=0.5 V on the 0..10 V range and
    // Vcc_wl_set=2.5 V on the 0..5 V range.
    localparam [15:0] DEFAULT_VCC_SET_CODE = 16'h0CCD;
    localparam [15:0] DEFAULT_VCC_WL_CODE  = 16'h8000;
    // Support rails from the known-good complete bench profile.
    localparam [15:0] DEFAULT_DAC0_CODE    = 16'h199A; // 0.5 V
    localparam [15:0] DEFAULT_DAC1_CODE    = 16'h8000; // 2.5 V
    localparam [15:0] DEFAULT_DAC5_CODE    = 16'h75C3; // 2.3 V
    // Restore the earlier bench rail profile requested on 2026-09-03.
    // DAC7 uses the 0..5 V range: 0xCCCC = 4.0 V VDDIO.
    localparam [15:0] DEFAULT_VDDIO_CODE   = 16'hCCCC;

    // Existing scan-debug values for DAC9..DAC13.  Reapply these after a WB
    // operation so selecting scan mode restores the previous configuration.
    localparam [15:0] SCAN_DAC9_CODE  = 16'h0CCD;
    localparam [15:0] SCAN_DAC10_CODE = 16'h170A;
    localparam [15:0] SCAN_DAC11_CODE = 16'h0F5C;
    localparam [15:0] SCAN_DAC12_CODE = 16'h28F6;
    localparam [15:0] SCAN_DAC13_CODE = 16'hFFFF;

    // WB-only voltages.  DAC9/11/12/13 use the configured 0..5 V span;
    // DAC10 uses its configured 0..10 V span.
    localparam [15:0] WB_IREF_DAC9_CODE       = 16'h199A; // 0.5 V
    localparam [15:0] WB_VCOMP_DAC10_CODE     = 16'h170A; // 0.9 V
    localparam [15:0] WB_BIAS_COMP2_DAC11_CODE = 16'h1EB8; // 0.6 V
    localparam [15:0] WB_VBIAS_DAC12_CODE     = 16'h51EB; // 1.6 V
    localparam [15:0] WB_DC_BIAS_DAC13_CODE   = 16'h3333; // 1.0 V

    reg [2:0] state = BOOT_INITIALIZE ? SPI_LOAD : SPI_IDLE;
    reg [4:0] frame_index = 5'd0;
    reg [4:0] last_frame_index = 5'd24;
    reg [4:0] bit_index = 5'd23;
    reg [31:0] boot_delay_count = 32'd0;
    reg [23:0] frame = 24'd0;
    reg runtime_update = 1'b0;
    reg wb_profile = 1'b0;
    reg [15:0] vcc_set_code = DEFAULT_VCC_SET_CODE;
    reg [15:0] vcc_wl_set_code = DEFAULT_VCC_WL_CODE;

    // Channels operate asynchronously, so LDAC remains inactive/high.
    assign ldac_n_o = 1'b1;

    function [23:0] initialization_frame;
        input [4:0] index;
        begin
            case (index)
                // Keep every output off while ranges and data are configured.
                5'd0:  initialization_frame = 24'h09FFFF;
                // DAC2 remains on 0..5 V; DAC6 uses the proven pre-WB 0..10 V
                // range so its 0..10 V runtime code produces the requested rail.
                5'd1:  initialization_frame = 24'h0A0000;
                5'd2:  initialization_frame = 24'h0B0101;
                5'd3:  initialization_frame = 24'h0C0101;
                5'd4:  initialization_frame = 24'h0D0000;
                5'd5:  initialization_frame = 24'h030004;
                5'd6:  initialization_frame = 24'h05FFFF;
                // Power VDDIO first, then pause in SPI_BOOT_DELAY.
                5'd7:  initialization_frame = {8'h17, DEFAULT_VDDIO_CODE};
                5'd8:  initialization_frame = 24'h09FF7F;
                5'd9:  initialization_frame = {8'h10, DEFAULT_DAC0_CODE};
                5'd10: initialization_frame = {8'h11, DEFAULT_DAC1_CODE};
                // Mirror Vcc_set on both the established DAC2 bench route and
                // the newer DAC6 scan route. This keeps either wiring revision
                // at the requested safe read voltage.
                5'd11: initialization_frame = {8'h12, DEFAULT_DAC0_CODE};
                5'd12: initialization_frame = {8'h13, DEFAULT_VCC_WL_CODE};
                5'd13: initialization_frame = 24'h140000;
                5'd14: initialization_frame = {8'h15, DEFAULT_DAC5_CODE};
                5'd15: initialization_frame = {8'h16, DEFAULT_VCC_SET_CODE};
                5'd16: initialization_frame = 24'h180000;
                5'd17: initialization_frame = {8'h19, SCAN_DAC9_CODE};
                5'd18: initialization_frame = {8'h1A, SCAN_DAC10_CODE};
                5'd19: initialization_frame = {8'h1B, SCAN_DAC11_CODE};
                5'd20: initialization_frame = {8'h1C, SCAN_DAC12_CODE};
                5'd21: initialization_frame = {8'h1D, SCAN_DAC13_CODE};
                5'd22: initialization_frame = 24'h1E0000;
                // Legacy bench setting, not nominal: VCCD2 is normally 1.8 V.
                5'd23: initialization_frame = 24'h1F6B85; // DAC15 VCCD2 ~= 2.1 V / 5 V
                // Enable the scan profile only after every code is loaded.
                5'd24: initialization_frame = 24'h094110;
                default: initialization_frame = 24'h000000;
            endcase
        end
    endfunction

    function [23:0] update_frame;
        input [4:0] index;
        begin
            if (wb_profile) begin
                case (index)
                    5'd0: update_frame = {8'h19, WB_IREF_DAC9_CODE};
                    5'd1: update_frame = {8'h1A, WB_VCOMP_DAC10_CODE};
                    5'd2: update_frame = {8'h1B, WB_BIAS_COMP2_DAC11_CODE};
                    5'd3: update_frame = {8'h1C, WB_VBIAS_DAC12_CODE};
                    5'd4: update_frame = {8'h1D, WB_DC_BIAS_DAC13_CODE};
                    // WB mode does not use the two scan-programming rails.
                    5'd5: update_frame = 24'h094158;
                    default: update_frame = 24'h000000;
                endcase
            end else begin
                case (index)
                    // Restore the complete scan range map before loading data.
                    // DAC2 is a 0..5 V mirror, so double/saturate the 0..10 V
                    // Vcc_set code used directly by the established DAC6 route.
                    5'd0: update_frame = 24'h0A0000;
                    5'd1: update_frame = 24'h0B0101;
                    5'd2: update_frame = 24'h0C0101;
                    5'd3: update_frame = 24'h0D0000;
                    5'd4: update_frame = {8'h12, vcc_set_code[15] ? 16'hFFFF : {vcc_set_code[14:0], 1'b0}};
                    5'd5: update_frame = {8'h13, vcc_wl_set_code};
                    5'd6: update_frame = {8'h16, vcc_set_code};
                    5'd7: update_frame = {8'h19, SCAN_DAC9_CODE};
                    5'd8: update_frame = {8'h1A, SCAN_DAC10_CODE};
                    5'd9: update_frame = {8'h1B, SCAN_DAC11_CODE};
                    5'd10: update_frame = {8'h1C, SCAN_DAC12_CODE};
                    5'd11: update_frame = {8'h1D, SCAN_DAC13_CODE};
                    // Keep WB-unused DAC4/8/14 off; re-enable DAC3 and DAC6.
                    5'd12: update_frame = 24'h094110;
                    default: update_frame = 24'h000000;
                endcase
            end
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
                    wb_profile      <= wb_profile_i;
                    runtime_update  <= 1'b1;
                    frame_index     <= 5'd0;
                    last_frame_index <= wb_profile_i ? 5'd5 : 5'd12;
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
                if (!runtime_update && frame_index == 5'd8) begin
                    frame_index <= 5'd9;
                    boot_delay_count <= 32'd0;
                    state <= SPI_BOOT_DELAY;
                end else if (frame_index == last_frame_index) begin
                    initialized_o <= 1'b1;
                    ready_o        <= 1'b1;
                    runtime_update <= 1'b0;
                    state          <= SPI_IDLE;
                end else begin
                    frame_index <= frame_index + 5'd1;
                    state <= SPI_LOAD;
                end
            end

            SPI_BOOT_DELAY: begin
                sclk_o <= 1'b0;
                sdi_o  <= 1'b0;
                cs_n_o <= 1'b1;
                if ((VDDIO_STARTUP_DELAY_CYCLES == 32'd0) ||
                    (boot_delay_count + 32'd1 >= VDDIO_STARTUP_DELAY_CYCLES)) begin
                    state <= SPI_LOAD;
                end else begin
                    boot_delay_count <= boot_delay_count + 32'd1;
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
