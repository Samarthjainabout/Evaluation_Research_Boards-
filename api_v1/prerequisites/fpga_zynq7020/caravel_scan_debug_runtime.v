`timescale 1ns / 1ps

// Universal scan-debug image.  A Vivado VIO core supplies one 64-bit runtime
// command over JTAG, so cell/operation/rail changes do not require synthesis.
//
// Command layout:
//   [63]    trigger toggle
//   [62]    scan OP_SET; runtime WB operation (0=read, 1=write)
//   [61:57] scan row / WL / SL
//   [56:52] scan column / BL
//   [51:41] scan packet count (1..1024)
//   [40:25] scan DAC6 Vcc_set code
//   [24:9]  scan DAC3 Vcc_wl_set code
//   [8]     runtime WB command marker
//   [7]     reset-only command (preserve DACs, do not issue a scan packet)
//   [6]     direct DAC update command (no scan/WB packet)
//
// For a runtime WB command, [61:30] is the 32-bit WB packet and [29:22] is
// the expected UART tag. A WB skew uses [2:0] as the bias selector (0..4,
// 7=nominal) and reconstructs its 16-bit DAC code from {[21:9],[5:3]}.
// Direct DAC updates use [61:58] as channel and [40:25] as the DAC code.
module caravel_scan_debug_runtime #(
    parameter [31:0] RESET_RELEASE_FALLBACK_CYCLES = 32'd2000,
    parameter [31:0] POST_RESET_WAIT_CYCLES = 32'd128,
    parameter [31:0] TM_SETUP_CYCLES = 32'd0,
    parameter [31:0] POST_DR_TM_HOLD_CYCLES = 32'd2400,
    parameter [31:0] REPEAT_AFTER_DONE_CYCLES = 32'd1,
    parameter [31:0] FPGA_RESET_ASSERT_CYCLES = 32'd24000,
    // Deliberately slow pulse-width command link.  The management CPU polls
    // GPIO through the housekeeping bus, so millisecond-scale pulses did not
    // leave enough margin on hardware even though they looked correct on LA.
    parameter [31:0] WB_ZERO_LOW_CYCLES = 32'd200000,
    parameter [31:0] WB_ONE_LOW_CYCLES = 32'd800000,
    parameter [31:0] WB_SEPARATOR_CYCLES = 32'd500000,
    parameter [31:0] WB_TERMINATOR_CYCLES = 32'd500000
) (
    input  wire sys_clk_50m_i,
    input  wire wb_clk_i,
    inout  wire si5351_sda_io,
    output wire si5351_scl_io,
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
    localparam [3:0] ST_WB_TX_LOW  = 4'd11;
    localparam [3:0] ST_WB_TX_HIGH = 4'd12;
    localparam [3:0] ST_WB_TX_END  = 4'd13;
    localparam [3:0] ST_WB_UART    = 4'd14;
    localparam [3:0] ST_WB_TX_ACK  = 4'd15;

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
    reg direct_dac_update = 1'b0;
    reg [3:0] direct_dac_channel = 4'd0;
    reg [15:0] direct_dac_code = 16'd0;
    reg [2:0] wb_skew_select = 3'd7;
    reg [15:0] wb_skew_code = 16'd0;
    // Passive by default so loading this image cannot drive Caravel's test
    // pins. WB commands retain high impedance; scan commands enable them.
    reg wb_controls_high_z = 1'b1;
    reg wb_command_drive = 1'b0;
    reg wb_command_data = 1'b0;
    reg [63:0] wb_command_frame = 64'd0;
    reg [6:0] wb_command_bit = 7'd63;
    reg [7:0] wb_expected_tag = 8'd0;
    reg wb_ready_ack_seen = 1'b0;
    reg clock_mode_10m_req = 1'b0;

    reg [15:0] vcc_set_code = 16'h0CCD;
    reg [15:0] vcc_wl_set_code = 16'h8000;
    reg dac_update_req = 1'b0;
    reg dac_update_started = 1'b0;
    reg dac_busy_seen = 1'b0;

    // Assert reset from the first configured FPGA state so Caravel cannot
    // observe partially sequenced DAC rails during VDDIO startup.
    reg caravel_resetb_r = 1'b0;
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

    wire si5351_ready_sys;
    wire si5351_active_10m_sys;
    wire si5351_busy_sys;
    wire si5351_error_sys;
    reg si5351_ready_meta = 1'b0;
    reg si5351_ready_sync = 1'b0;
    reg si5351_mode_meta = 1'b0;
    reg si5351_mode_sync = 1'b0;

    si5351_mode_controller clock_controller (
        .sys_clk_i(sys_clk_50m_i),
        .mode_10m_req_i(clock_mode_10m_req),
        .si5351_sda_io(si5351_sda_io),
        .si5351_scl_io(si5351_scl_io),
        .ready_o(si5351_ready_sys),
        .active_10m_o(si5351_active_10m_sys),
        .busy_o(si5351_busy_sys),
        .error_o(si5351_error_sys)
    );

    // The request is held as a level while CLK0/CLK1 are stopped. The
    // acknowledgement is synchronized after the newly selected clock returns.
    always @(posedge wb_clk_i) begin
        si5351_ready_meta <= si5351_ready_sys;
        si5351_ready_sync <= si5351_ready_meta;
        si5351_mode_meta <= si5351_active_10m_sys;
        si5351_mode_sync <= si5351_mode_meta;
    end

    // The VIO core follows the returned Si5351 clock copy: 2 MHz for scan
    // debug and 10 MHz for native Wishbone. The
    // API changes probe_out0 through JTAG and observes probe_in0 for diagnosis.
    vio_runtime_command runtime_vio (
        .clk(wb_clk_i),
        .probe_in0(runtime_status),
        .probe_out0(runtime_command)
    );

    dac81416_runtime_spi #(
        .BOOT_INITIALIZE(1'b1)
    ) dac_controller (
        .clk_i(wb_clk_i),
        .update_i(dac_update_req),
        .wb_profile_i(wb_profile),
        .direct_update_i(direct_dac_update),
        .direct_channel_i(direct_dac_channel),
        .direct_code_i(direct_dac_code),
        .wb_skew_select_i(wb_skew_select),
        .wb_skew_code_i(wb_skew_code),
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
        .CLOCK_HZ(10000000),
        .BAUD(9600)
    ) caravel_uart_receiver (
        .clk_i(wb_clk_i),
        .reset_i(!caravel_resetb_r),
        .rx_i(caravel_uart_tx_i),
        .byte_o(uart_rx_byte),
        .byte_valid_o(uart_rx_byte_valid),
        .framing_error_o(uart_rx_framing_error)
    );

    assign caravel_resetb_o  = caravel_resetb_r;
    assign caravel_tm_o      = wb_controls_high_z ? 1'bz : caravel_tm_r;
    assign caravel_scan_se_o = wb_controls_high_z ? 1'bz : caravel_scan_se_r;
    assign caravel_scan_si_o = wb_command_drive ? wb_command_data
        : (wb_controls_high_z ? 1'bz : caravel_scan_si_r);
    assign caravel_scan_cc_o = caravel_scan_cc_r;
    // Upper status bits retain the latest framed Caravel UART response:
    // [63] valid, [62] receive/checksum error, [61:54] tag,
    // [53:22] 32-bit result, [21:19] permanent-runtime image signature
    // (3'b011). Existing command status remains in [18:0].
    assign runtime_status = {
        uart_result_valid,
        uart_result_error,
        uart_result_tag,
        uart_result_data,
        3'b011,
        packets_remaining,
        state,
        command_counter,
        dac_ready,
        done_o,
        busy_o
    };

    function [7:0] wb_command_checksum;
        input write_i;
        input [7:0] tag_i;
        input [31:0] value_i;
        begin
            wb_command_checksum = 8'hA7 ^ {7'd0, write_i} ^ tag_i
                ^ value_i[31:24] ^ value_i[23:16]
                ^ value_i[15:8] ^ value_i[7:0];
        end
    endfunction

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
                // Hold Caravel reset while the DAC powers VDDIO first, waits
                // for it to settle, and then enables the remaining rails.
                caravel_resetb_r <= 1'b0;
                caravel_tm_r <= 1'b0;
                caravel_scan_se_r <= 1'b1;
                caravel_scan_si_r <= 1'b0;
                caravel_scan_cc_r <= 1'b0;
                busy_o <= 1'b0;
                done_o <= 1'b0;
                dac_update_req <= 1'b0;
                wb_command_drive <= 1'b0;
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
                    direct_dac_update <= runtime_command[6];
                    direct_dac_channel <= runtime_command[61:58];
                    direct_dac_code <= runtime_command[40:25];
                    wb_skew_select <= runtime_command[2:0];
                    wb_skew_code <= {runtime_command[21:9], runtime_command[5:3]};
                    wb_controls_high_z <= runtime_command[8] || runtime_command[7] || runtime_command[6];
                    wb_command_drive <= runtime_command[8];
                    wb_command_data <= runtime_command[8];
                    wb_command_frame <= {
                        8'hA7,
                        {7'd0, runtime_command[62]},
                        runtime_command[29:22],
                        runtime_command[61:30],
                        wb_command_checksum(
                            runtime_command[62],
                            runtime_command[29:22],
                            runtime_command[61:30]
                        )
                    };
                    wb_command_bit <= 7'd63;
                    wb_expected_tag <= runtime_command[29:22];
                    clock_mode_10m_req <= runtime_command[8];
                    // Reset is asserted before the independent 50 MHz
                    // controller is allowed to stop and retune XCLK.
                    caravel_resetb_r <= 1'b0;
                    dac_update_started <= 1'b0;
                    dac_busy_seen <= 1'b0;
                    busy_o <= 1'b1;
                    // WB applies its read-bias profile before resetting
                    // Caravel. Reset-only preserves DACs; direct updates stop
                    // after the requested DAC register has been written.
                    state <= runtime_command[7] && !runtime_command[8]
                        ? ST_FPGA_RESET : ST_DAC_UPDATE;
                end
            end

            ST_DAC_UPDATE: begin
                caravel_resetb_r <= 1'b0;
                busy_o <= 1'b1;
                done_o <= 1'b0;
                if (!si5351_ready_sync || si5351_mode_sync != wb_profile) begin
                    dac_update_req <= 1'b0;
                    dac_update_started <= 1'b0;
                    dac_busy_seen <= 1'b0;
                end else if (!dac_update_started) begin
                    dac_update_req <= 1'b1;
                    dac_update_started <= 1'b1;
                end else begin
                    dac_update_req <= 1'b0;
                    if (!dac_ready) begin
                        dac_busy_seen <= 1'b1;
                    end
                    if (dac_busy_seen && dac_ready) begin
                        wait_count <= 32'd0;
                        state <= direct_dac_update ? ST_FINISH : ST_FPGA_RESET;
                    end
                end
            end

            ST_FPGA_RESET: begin
                caravel_resetb_r <= 1'b0;
                caravel_tm_r <= 1'b0;
                caravel_scan_se_r <= 1'b1;
                caravel_scan_si_r <= wb_profile;
                caravel_scan_cc_r <= 1'b0;
                busy_o <= 1'b1;
                done_o <= 1'b0;
                ready_low_seen_r <= 1'b0;
                scan_cycle <= 5'd0;
                if (!si5351_ready_sync || si5351_mode_sync != wb_profile) begin
                    wait_count <= 32'd0;
                end else if (wait_count >= (wb_profile
                    ? (FPGA_RESET_ASSERT_CYCLES * 32'd5)
                    : FPGA_RESET_ASSERT_CYCLES)) begin
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
                caravel_scan_si_r <= wb_profile;
                caravel_scan_cc_r <= 1'b0;
                busy_o <= 1'b1;
                done_o <= 1'b0;
                if (ready_low_seen_r && caravel_ready_i) begin
                    ready_low_seen_r <= 1'b0;
                    wait_count <= 32'd0;
                    state <= ST_POST_WAIT;
                end else if (wait_count >= (wb_profile
                    ? (RESET_RELEASE_FALLBACK_CYCLES * 32'd5)
                    : RESET_RELEASE_FALLBACK_CYCLES)) begin
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
                caravel_scan_si_r <= wb_profile;
                caravel_scan_cc_r <= 1'b0;
                busy_o <= 1'b1;
                done_o <= 1'b0;
                if (wb_profile && uart_result_valid
                    && uart_result_tag == 8'hF1 && uart_result_data == 32'd1) begin
                    wait_count <= 32'd0;
                    wb_command_bit <= 7'd63;
                    wb_command_data <= wb_command_frame[63];
                    wb_ready_ack_seen <= caravel_ready_i;
                    state <= ST_WB_TX_LOW;
                end else if (!wb_profile && wait_count >= POST_RESET_WAIT_CYCLES) begin
                    wait_count <= 32'd0;
                    state <= reset_only ? ST_FINISH : ST_TM_SETUP;
                end else begin
                    wait_count <= wait_count + 32'd1;
                end
            end

            ST_WB_TX_LOW: begin
                caravel_resetb_r <= 1'b1;
                wb_command_drive <= 1'b1;
                wb_command_data <= 1'b0;
                busy_o <= 1'b1;
                done_o <= 1'b0;
                if (wait_count >= (wb_command_frame[wb_command_bit]
                    ? WB_ONE_LOW_CYCLES : WB_ZERO_LOW_CYCLES) - 1'b1) begin
                    wait_count <= 32'd0;
                    state <= ST_WB_TX_HIGH;
                end else begin
                    wait_count <= wait_count + 32'd1;
                end
            end

            ST_WB_TX_HIGH: begin
                caravel_resetb_r <= 1'b1;
                wb_command_drive <= 1'b1;
                wb_command_data <= 1'b1;
                busy_o <= 1'b1;
                done_o <= 1'b0;
                if (wait_count >= WB_SEPARATOR_CYCLES - 1'b1) begin
                    wait_count <= 32'd0;
                    state <= ST_WB_TX_ACK;
                end else begin
                    wait_count <= wait_count + 32'd1;
                end
            end

            ST_WB_TX_ACK: begin
                caravel_resetb_r <= 1'b1;
                wb_command_drive <= 1'b1;
                wb_command_data <= 1'b1;
                busy_o <= 1'b1;
                done_o <= 1'b0;
                if (caravel_ready_i != wb_ready_ack_seen) begin
                    wb_ready_ack_seen <= caravel_ready_i;
                    wait_count <= 32'd0;
                    if (wb_command_bit == 7'd0) begin
                        state <= ST_WB_TX_END;
                    end else begin
                        wb_command_bit <= wb_command_bit - 1'b1;
                        state <= ST_WB_TX_LOW;
                    end
                end
            end

            ST_WB_TX_END: begin
                caravel_resetb_r <= 1'b1;
                wb_command_drive <= 1'b1;
                wb_command_data <= 1'b0;
                busy_o <= 1'b1;
                done_o <= 1'b0;
                if (wait_count >= WB_TERMINATOR_CYCLES - 1'b1) begin
                    wait_count <= 32'd0;
                    wb_command_drive <= 1'b0;
                    state <= ST_WB_UART;
                end else begin
                    wait_count <= wait_count + 32'd1;
                end
            end

            ST_WB_UART: begin
                caravel_resetb_r <= 1'b1;
                wb_command_drive <= 1'b0;
                busy_o <= 1'b1;
                done_o <= 1'b0;
                // A fresh VIO toggle aborts a stalled command and starts a
                // clean Caravel reset without touching the DAC controller.
                // This also makes the API's one-time firmware migration retry
                // recoverable without reprogramming the FPGA (which would
                // otherwise replay the DAC boot profile).
                if (runtime_command[63] != command_toggle_seen) begin
                    command_toggle_seen <= runtime_command[63];
                    command_counter <= ~command_counter;
                    op_set <= runtime_command[62];
                    seq_row <= runtime_command[61:57];
                    seq_col <= runtime_command[56:52];
                    packets_remaining <= 11'd0;
                    wb_profile <= runtime_command[8];
                    reset_only <= runtime_command[7];
                    direct_dac_update <= runtime_command[6];
                    direct_dac_channel <= runtime_command[61:58];
                    direct_dac_code <= runtime_command[40:25];
                    wb_skew_select <= runtime_command[2:0];
                    wb_skew_code <= {runtime_command[21:9], runtime_command[5:3]};
                    wb_controls_high_z <= runtime_command[8] || runtime_command[7] || runtime_command[6];
                    wb_command_drive <= runtime_command[8];
                    wb_command_data <= runtime_command[8];
                    wb_command_frame <= {
                        8'hA7,
                        {7'd0, runtime_command[62]},
                        runtime_command[29:22],
                        runtime_command[61:30],
                        wb_command_checksum(
                            runtime_command[62],
                            runtime_command[29:22],
                            runtime_command[61:30]
                        )
                    };
                    wb_command_bit <= 7'd63;
                    wb_expected_tag <= runtime_command[29:22];
                    clock_mode_10m_req <= runtime_command[8];
                    caravel_resetb_r <= 1'b0;
                    wait_count <= 32'd0;
                    ready_low_seen_r <= 1'b0;
                    dac_update_started <= 1'b0;
                    dac_busy_seen <= 1'b0;
                    state <= runtime_command[7] && !runtime_command[8]
                        ? ST_FPGA_RESET : ST_DAC_UPDATE;
                end else if (uart_result_valid && uart_result_tag == wb_expected_tag) begin
                    wait_count <= 32'd0;
                    state <= ST_FINISH;
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
