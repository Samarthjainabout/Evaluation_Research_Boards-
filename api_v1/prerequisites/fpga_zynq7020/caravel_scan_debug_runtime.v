`timescale 1ns / 1ps

// Universal scan-debug image.  A Vivado VIO core supplies one 64-bit runtime
// command over JTAG, so cell/operation/rail changes do not require synthesis.
//
// Command layout:
//   [63]    trigger toggle
//   [62]    OP_SET (0=read/reset packet, 1=set packet)
//   [61:57] row / WL / SL
//   [56:52] column / BL
//   [51:41] packet count (1..1024)
//   [40:25] DAC6 Vcc_set straight-binary code (0..10 V range)
//   [24:9]  DAC3 Vcc_wl_set straight-binary code (0..5 V range)
//   [8]     DAC profile (0=existing scan-debug profile, 1=Wishbone profile)
//   [7]     reset-only command (preserve DACs, do not issue a scan packet)
//
// A Wishbone-profile command updates only the static support DACs and then
// releases Caravel reset without issuing a scan packet.  The next ordinary
// scan command restores the original scan-debug DAC values before scanning.
module caravel_scan_debug_runtime #(
    parameter [31:0] RESET_RELEASE_FALLBACK_CYCLES = 32'd2000,
    parameter [31:0] POST_RESET_WAIT_CYCLES = 32'd128,
    parameter [31:0] TM_SETUP_CYCLES = 32'd0,
    parameter [31:0] POST_DR_TM_HOLD_CYCLES = 32'd100,
    parameter [31:0] REPEAT_AFTER_DONE_CYCLES = 32'd1,
    parameter [31:0] FPGA_RESET_ASSERT_CYCLES = 32'd240000
) (
    input  wire wb_clk_i,
    input  wire caravel_ready_i,
    input  wire caravel_uart_tx_i,
    output wire caravel_resetb_o,
    output wire caravel_tm_o,
    output wire caravel_scan_se_o,
    output wire caravel_scan_si_o,
    output wire caravel_scan_cc_o,
    output wire dac_sclk_o,
    output wire dac_sdi_o,
    output wire dac_cs_n_o,
    output wire dac_ldac_n_o,
    output reg  busy_o = 1'b0,
    output reg  done_o = 1'b0
);
    localparam [3:0] ST_BOOT_DAC   = 4'd0;
    localparam [3:0] ST_IDLE       = 4'd1;
    localparam [3:0] ST_DAC_UPDATE = 4'd2;
    localparam [3:0] ST_FPGA_RESET = 4'd3;
    localparam [3:0] ST_RESET_WAIT = 4'd4;
    localparam [3:0] ST_POST_WAIT  = 4'd5;
    localparam [3:0] ST_TM_SETUP   = 4'd6;
    localparam [3:0] ST_SHIFT      = 4'd7;
    localparam [3:0] ST_TM_TAIL    = 4'd8;
    localparam [3:0] ST_DONE_GAP   = 4'd9;
    localparam [3:0] ST_FINISH     = 4'd10;

    wire [63:0] runtime_command;
    wire [63:0] runtime_status;

    reg [3:0] state = ST_BOOT_DAC;
    reg [31:0] wait_count = 32'd0;
    reg [4:0] scan_cycle = 5'd0;
    reg [4:0] seq_row = 5'd0;
    reg [4:0] seq_col = 5'd0;
    reg op_set = 1'b0;
    reg [10:0] packets_remaining = 11'd0;
    reg command_toggle_seen = 1'b0;
    reg command_counter = 1'b0;
    reg wb_profile = 1'b0;
    reg reset_only = 1'b0;
    // Passive by default so loading this image cannot drive Caravel's test
    // pins. WB commands retain high impedance; scan commands enable them.
    reg wb_controls_high_z = 1'b1;

    reg [15:0] vcc_set_code = 16'h0CCD;
    reg [15:0] vcc_wl_set_code = 16'h8000;
    reg dac_update_req = 1'b0;
    reg dac_update_started = 1'b0;
    reg dac_busy_seen = 1'b0;

    reg caravel_resetb_r = 1'b1;
    reg caravel_tm_r = 1'b0;
    reg caravel_scan_se_r = 1'b1;
    reg caravel_scan_si_r = 1'b0;
    reg caravel_scan_cc_r = 1'b0;
    reg ready_low_seen_r = 1'b0;

    wire dac_initialized;
    wire dac_ready;
    wire [7:0] uart_rx_byte;
    wire uart_rx_byte_valid;
    wire uart_rx_framing_error;
    reg [3:0] uart_frame_state = 4'd0;
    reg [7:0] uart_frame_checksum = 8'd0;
    reg [7:0] uart_result_tag = 8'd0;
    reg [31:0] uart_result_data = 32'd0;
    reg uart_result_valid = 1'b0;
    reg uart_result_error = 1'b0;
    reg uart_command_toggle_seen = 1'b0;
    wire [15:0] active_scan_word = {op_set, seq_row, seq_col, seq_row};

    // The generated VIO core is clocked by the same 2 MHz bench XCLK.  The
    // API changes probe_out0 through JTAG and observes probe_in0 for diagnosis.
    vio_runtime_command runtime_vio (
        .clk(wb_clk_i),
        .probe_in0(runtime_status),
        .probe_out0(runtime_command)
    );

    dac81416_runtime_spi #(
        .BOOT_INITIALIZE(1'b0)
    ) dac_controller (
        .clk_i(wb_clk_i),
        .update_i(dac_update_req),
        .wb_profile_i(wb_profile),
        .vcc_set_code_i(vcc_set_code),
        .vcc_wl_set_code_i(vcc_wl_set_code),
        .sclk_o(dac_sclk_o),
        .sdi_o(dac_sdi_o),
        .cs_n_o(dac_cs_n_o),
        .ldac_n_o(dac_ldac_n_o),
        .initialized_o(dac_initialized),
        .ready_o(dac_ready)
    );

    uart_rx_8n1 #(
        .CLOCK_HZ(2000000),
        .BAUD(9600)
    ) caravel_uart_receiver (
        .clk_i(wb_clk_i),
        .rx_i(caravel_uart_tx_i),
        .byte_o(uart_rx_byte),
        .byte_valid_o(uart_rx_byte_valid),
        .framing_error_o(uart_rx_framing_error)
    );

    assign caravel_resetb_o  = caravel_resetb_r;
    assign caravel_tm_o      = wb_controls_high_z ? 1'bz : caravel_tm_r;
    assign caravel_scan_se_o = wb_controls_high_z ? 1'bz : caravel_scan_se_r;
    assign caravel_scan_si_o = wb_controls_high_z ? 1'bz : caravel_scan_si_r;
    assign caravel_scan_cc_o = caravel_scan_cc_r;
    // Upper status bits retain the latest framed Caravel UART response:
    // [63] valid, [62] receive/checksum error, [61:54] tag,
    // [53:22] 32-bit result, [21:19] WB-high-Z UART image signature
    // (3'b101). Existing command status remains in [18:0].
    assign runtime_status = {
        uart_result_valid,
        uart_result_error,
        uart_result_tag,
        uart_result_data,
        3'b101,
        packets_remaining,
        state,
        command_counter,
        dac_ready,
        done_o,
        busy_o
    };

    // Binary response frame: A5 5A TAG DATA[31:24] DATA[23:16]
    // DATA[15:8] DATA[7:0] XOR(TAG, DATA bytes).
    always @(posedge wb_clk_i) begin
        if (runtime_command[63] != uart_command_toggle_seen) begin
            uart_command_toggle_seen <= runtime_command[63];
            uart_frame_state <= 4'd0;
            uart_frame_checksum <= 8'd0;
            uart_result_valid <= 1'b0;
            uart_result_error <= 1'b0;
        end else if (uart_rx_framing_error) begin
            uart_frame_state <= 4'd0;
            uart_result_error <= 1'b1;
        end else if (uart_rx_byte_valid) begin
            case (uart_frame_state)
                4'd0: begin
                    if (uart_rx_byte == 8'hA5)
                        uart_frame_state <= 4'd1;
                end
                4'd1: begin
                    if (uart_rx_byte == 8'h5A)
                        uart_frame_state <= 4'd2;
                    else if (uart_rx_byte != 8'hA5)
                        uart_frame_state <= 4'd0;
                end
                4'd2: begin
                    uart_result_tag <= uart_rx_byte;
                    uart_frame_checksum <= uart_rx_byte;
                    uart_frame_state <= 4'd3;
                end
                4'd3: begin
                    uart_result_data[31:24] <= uart_rx_byte;
                    uart_frame_checksum <= uart_frame_checksum ^ uart_rx_byte;
                    uart_frame_state <= 4'd4;
                end
                4'd4: begin
                    uart_result_data[23:16] <= uart_rx_byte;
                    uart_frame_checksum <= uart_frame_checksum ^ uart_rx_byte;
                    uart_frame_state <= 4'd5;
                end
                4'd5: begin
                    uart_result_data[15:8] <= uart_rx_byte;
                    uart_frame_checksum <= uart_frame_checksum ^ uart_rx_byte;
                    uart_frame_state <= 4'd6;
                end
                4'd6: begin
                    uart_result_data[7:0] <= uart_rx_byte;
                    uart_frame_checksum <= uart_frame_checksum ^ uart_rx_byte;
                    uart_frame_state <= 4'd7;
                end
                4'd7: begin
                    if (uart_rx_byte == uart_frame_checksum) begin
                        uart_result_valid <= 1'b1;
                        uart_result_error <= 1'b0;
                    end else begin
                        uart_result_error <= 1'b1;
                    end
                    uart_frame_state <= 4'd0;
                end
                default: uart_frame_state <= 4'd0;
            endcase
        end
    end

    task advance_cell;
        begin
            if (seq_row < 5'd31) begin
                seq_row <= seq_row + 5'd1;
            end else begin
                seq_row <= 5'd0;
                seq_col <= seq_col + 5'd1;
            end
        end
    endtask

    always @(negedge wb_clk_i) begin
        case (state)
            ST_BOOT_DAC: begin
                // Loading this runtime image must preserve the live Caravel
                // and DAC state. Reset is asserted only by an explicit VIO
                // command with runtime_command[7] set.
                caravel_resetb_r <= 1'b1;
                caravel_tm_r <= 1'b0;
                caravel_scan_se_r <= 1'b1;
                caravel_scan_si_r <= 1'b0;
                caravel_scan_cc_r <= 1'b0;
                busy_o <= 1'b0;
                done_o <= 1'b0;
                dac_update_req <= 1'b0;
                if (dac_initialized && dac_ready) begin
                    state <= ST_IDLE;
                end
            end

            ST_IDLE: begin
                // Hold the previous safe reset level: low before the first
                // command, high after a completed command.  The next command
                // asserts reset in ST_DAC_UPDATE before changing its rails.
                caravel_tm_r <= 1'b0;
                caravel_scan_se_r <= 1'b1;
                caravel_scan_si_r <= 1'b0;
                caravel_scan_cc_r <= 1'b0;
                busy_o <= 1'b0;
                done_o <= 1'b0;
                wait_count <= 32'd0;
                scan_cycle <= 5'd0;
                ready_low_seen_r <= 1'b0;
                dac_update_req <= 1'b0;
                if (runtime_command[63] != command_toggle_seen) begin
                    command_toggle_seen <= runtime_command[63];
                    command_counter <= ~command_counter;
                    op_set <= runtime_command[62];
                    seq_row <= runtime_command[61:57];
                    seq_col <= runtime_command[56:52];
                    packets_remaining <= runtime_command[7] ? 11'd0
                        : ((runtime_command[51:41] == 11'd0) ? 11'd1 : runtime_command[51:41]);
                    vcc_set_code <= runtime_command[40:25];
                    vcc_wl_set_code <= runtime_command[24:9];
                    wb_profile <= runtime_command[8];
                    reset_only <= runtime_command[7];
                    wb_controls_high_z <= runtime_command[8] || runtime_command[7];
                    dac_update_started <= 1'b0;
                    dac_busy_seen <= 1'b0;
                    busy_o <= 1'b1;
                    state <= runtime_command[7] ? ST_FPGA_RESET : ST_DAC_UPDATE;
                end
            end

            ST_DAC_UPDATE: begin
                caravel_resetb_r <= 1'b0;
                busy_o <= 1'b1;
                done_o <= 1'b0;
                if (!dac_update_started) begin
                    dac_update_req <= 1'b1;
                    dac_update_started <= 1'b1;
                end else begin
                    dac_update_req <= 1'b0;
                    if (!dac_ready) begin
                        dac_busy_seen <= 1'b1;
                    end
                    if (dac_busy_seen && dac_ready) begin
                        wait_count <= 32'd0;
                        // WB mode only selects its DAC profile.  Caravel's
                        // management CPU performs the native Wishbone access.
                        // Use the same reset assert/wait path as the scan
                        // runtime so the firmware starts from a clean reset.
                        if (wb_profile) begin
                            state <= ST_FPGA_RESET;
                        end else begin
                            state <= ST_FPGA_RESET;
                        end
                    end
                end
            end

            ST_FPGA_RESET: begin
                caravel_resetb_r <= 1'b0;
                caravel_tm_r <= 1'b0;
                caravel_scan_se_r <= 1'b1;
                caravel_scan_si_r <= 1'b0;
                caravel_scan_cc_r <= 1'b0;
                busy_o <= 1'b1;
                done_o <= 1'b0;
                ready_low_seen_r <= 1'b0;
                scan_cycle <= 5'd0;
                if (wait_count >= FPGA_RESET_ASSERT_CYCLES) begin
                    caravel_resetb_r <= 1'b1;
                    wait_count <= 32'd0;
                    state <= ST_RESET_WAIT;
                end else begin
                    wait_count <= wait_count + 32'd1;
                end
            end

            ST_RESET_WAIT: begin
                caravel_resetb_r <= 1'b1;
                caravel_tm_r <= 1'b0;
                caravel_scan_se_r <= 1'b1;
                caravel_scan_si_r <= 1'b0;
                caravel_scan_cc_r <= 1'b0;
                busy_o <= 1'b1;
                done_o <= 1'b0;
                if (ready_low_seen_r && caravel_ready_i) begin
                    ready_low_seen_r <= 1'b0;
                    wait_count <= 32'd0;
                    state <= ST_POST_WAIT;
                end else if (wait_count >= RESET_RELEASE_FALLBACK_CYCLES) begin
                    ready_low_seen_r <= 1'b0;
                    wait_count <= 32'd0;
                    state <= ST_POST_WAIT;
                end else begin
                    if (!caravel_ready_i) begin
                        ready_low_seen_r <= 1'b1;
                    end
                    wait_count <= wait_count + 32'd1;
                end
            end

            ST_POST_WAIT: begin
                caravel_resetb_r <= 1'b1;
                caravel_tm_r <= 1'b0;
                caravel_scan_se_r <= 1'b1;
                caravel_scan_si_r <= 1'b0;
                caravel_scan_cc_r <= 1'b0;
                busy_o <= 1'b1;
                done_o <= 1'b0;
                if (wait_count >= POST_RESET_WAIT_CYCLES) begin
                    wait_count <= 32'd0;
                    state <= (wb_profile || reset_only) ? ST_FINISH : ST_TM_SETUP;
                end else begin
                    wait_count <= wait_count + 32'd1;
                end
            end

            ST_TM_SETUP: begin
                caravel_resetb_r <= 1'b1;
                caravel_tm_r <= 1'b1;
                caravel_scan_se_r <= 1'b1;
                caravel_scan_si_r <= 1'b0;
                caravel_scan_cc_r <= 1'b0;
                busy_o <= 1'b1;
                done_o <= 1'b0;
                if (wait_count >= TM_SETUP_CYCLES) begin
                    wait_count <= 32'd0;
                    scan_cycle <= 5'd0;
                    state <= ST_SHIFT;
                end else begin
                    wait_count <= wait_count + 32'd1;
                end
            end

            ST_SHIFT: begin
                caravel_resetb_r <= 1'b1;
                caravel_tm_r <= 1'b1;
                caravel_scan_se_r <= 1'b0;
                caravel_scan_cc_r <= 1'b0;
                busy_o <= 1'b1;
                done_o <= 1'b0;
                if (scan_cycle == 5'd0) begin
                    caravel_scan_si_r <= 1'b0;
                end else if (scan_cycle <= 5'd16) begin
                    caravel_scan_si_r <= active_scan_word[scan_cycle - 5'd1];
                end else begin
                    caravel_scan_si_r <= 1'b0;
                end
                if (scan_cycle == 5'd17) begin
                    wait_count <= 32'd0;
                    state <= ST_TM_TAIL;
                end else begin
                    scan_cycle <= scan_cycle + 5'd1;
                end
            end

            ST_TM_TAIL: begin
                caravel_resetb_r <= 1'b1;
                caravel_tm_r <= 1'b1;
                caravel_scan_se_r <= 1'b1;
                caravel_scan_si_r <= 1'b0;
                caravel_scan_cc_r <= 1'b0;
                busy_o <= 1'b1;
                done_o <= 1'b0;
                if (wait_count >= POST_DR_TM_HOLD_CYCLES - 1'b1) begin
                    wait_count <= 32'd0;
                    state <= ST_DONE_GAP;
                end else begin
                    wait_count <= wait_count + 32'd1;
                end
            end

            ST_DONE_GAP: begin
                caravel_resetb_r <= 1'b1;
                caravel_tm_r <= 1'b0;
                caravel_scan_se_r <= 1'b1;
                caravel_scan_si_r <= 1'b0;
                caravel_scan_cc_r <= 1'b0;
                busy_o <= 1'b1;
                done_o <= 1'b1;
                if (wait_count >= REPEAT_AFTER_DONE_CYCLES) begin
                    wait_count <= 32'd0;
                    if (packets_remaining > 11'd1) begin
                        packets_remaining <= packets_remaining - 11'd1;
                        advance_cell();
                        state <= ST_FPGA_RESET;
                    end else begin
                        packets_remaining <= 11'd0;
                        state <= ST_FINISH;
                    end
                end else begin
                    wait_count <= wait_count + 32'd1;
                end
            end

            ST_FINISH: begin
                // Match the proven compile-time image: leave reset released
                // after the final TM falling edge and until the next command.
                caravel_resetb_r <= 1'b1;
                caravel_tm_r <= 1'b0;
                caravel_scan_se_r <= 1'b1;
                caravel_scan_si_r <= 1'b0;
                caravel_scan_cc_r <= 1'b0;
                busy_o <= 1'b0;
                done_o <= 1'b1;
                state <= ST_IDLE;
            end

            default: begin
                state <= ST_BOOT_DAC;
                caravel_resetb_r <= 1'b1;
                caravel_tm_r <= 1'b0;
                caravel_scan_se_r <= 1'b1;
                caravel_scan_si_r <= 1'b0;
                caravel_scan_cc_r <= 1'b0;
                busy_o <= 1'b0;
                done_o <= 1'b0;
                dac_update_req <= 1'b0;
            end
        endcase
    end
endmodule
