`timescale 1ns / 1ps

module caravel_scan_debug_fpga #(
    parameter [4:0] WL_SEL = 5'd1,
    parameter [4:0] BL_SEL = 5'd0,
    parameter [4:0] SL_SEL = 5'd1,
    parameter       OP_SET = 1'b0,
    parameter [31:0] RESET_RELEASE_FALLBACK_CYCLES = 32'd2000,
    parameter [31:0] POST_RESET_WAIT_CYCLES = 32'd128,
    parameter [31:0] TM_SETUP_CYCLES = 32'd0,
    parameter [31:0] POST_DR_TM_HOLD_CYCLES = 32'd5,
    parameter [31:0] REPEAT_AFTER_DONE_CYCLES = 32'd0,
    parameter        SEQUENCE_MODE = 1'b0,
    parameter [31:0] INITIAL_SEQUENCE_DELAY_CYCLES = 32'd0,
    parameter [4:0]  SEQ_START_ROW = 5'd3,
    parameter [4:0]  SEQ_START_COL = 5'd0,
    parameter [31:0] FPGA_RESET_ASSERT_CYCLES = 32'd240000,
    parameter        MANUAL_RESET_MODE = 1'b0,
    parameter integer DAC_VCC_SET_MV = 500,
    parameter integer DAC_VCC_WL_SET_MV = 2500
) (
    input  wire       wb_clk_i,
    input  wire       caravel_ready_i,
    output wire       caravel_resetb_o,
    output wire       caravel_tm_o,
    output wire       caravel_scan_se_o,
    output wire       caravel_scan_si_o,
    output wire       caravel_scan_cc_o,
    output wire       dac_sclk_o,
    output wire       dac_sdi_o,
    output wire       dac_cs_n_o,
    output wire       dac_ldac_n_o,
    output reg        busy_o,
    output reg        done_o
);

    localparam [15:0] SCAN_WORD = {OP_SET, SL_SEL, BL_SEL, WL_SEL};

    localparam [3:0] ST_INIT_DELAY = 4'd0;
    localparam [3:0] ST_FPGA_RESET = 4'd1;
    localparam [3:0] ST_RESET_WAIT = 4'd2;
    localparam [3:0] ST_POST_WAIT  = 4'd3;
    localparam [3:0] ST_TM_SETUP   = 4'd4;
    localparam [3:0] ST_SHIFT      = 4'd5;
    localparam [3:0] ST_TM_TAIL    = 4'd6;
    localparam [3:0] ST_DONE       = 4'd7;
    localparam [3:0] ST_DAC_INIT   = 4'd8;

    reg [31:0] wait_count = 32'd0;
    reg [4:0]  scan_cycle = 5'd0;
    reg [4:0]  seq_row = SEQ_START_ROW;
    reg [4:0]  seq_col = SEQ_START_COL;
    reg        seq_finished = 1'b0;
    reg        caravel_resetb_r = 1'b0;
    reg        caravel_tm_r = 1'b0;
    reg        caravel_scan_se_r = 1'b1;
    reg        caravel_scan_si_r = 1'b0;
    reg        caravel_scan_cc_r = 1'b0;
    reg        ready_low_seen_r = 1'b0;
    reg [3:0]  state = ST_DAC_INIT;

    wire [15:0] active_scan_word = SEQUENCE_MODE ? {OP_SET, seq_row, seq_col, seq_row} : SCAN_WORD;
    wire dac_ready;

    dac81416_spi #(
        .VCC_SET_MV(DAC_VCC_SET_MV),
        .VCC_WL_SET_MV(DAC_VCC_WL_SET_MV)
    ) dac_controller (
        .clk_i(wb_clk_i),
        .sclk_o(dac_sclk_o),
        .sdi_o(dac_sdi_o),
        .cs_n_o(dac_cs_n_o),
        .ldac_n_o(dac_ldac_n_o),
        .ready_o(dac_ready)
    );

    assign caravel_resetb_o  = MANUAL_RESET_MODE ? 1'bz : caravel_resetb_r;
    assign caravel_tm_o      = caravel_tm_r;
    assign caravel_scan_se_o = caravel_scan_se_r;
    assign caravel_scan_si_o = caravel_scan_si_r;
    assign caravel_scan_cc_o = caravel_scan_cc_r;

    always @(negedge wb_clk_i) begin
        case (state)
                ST_DAC_INIT: begin
                    // Keep Caravel in reset until every DAC range, power and
                    // output register has been written.
                    caravel_resetb_r   <= 1'b0;
                    caravel_tm_r       <= 1'b0;
                    caravel_scan_se_r  <= 1'b1;
                    caravel_scan_si_r  <= 1'b0;
                    caravel_scan_cc_r  <= 1'b0;
                    busy_o             <= 1'b0;
                    done_o             <= 1'b0;
                    ready_low_seen_r   <= 1'b0;
                    wait_count         <= 32'd0;
                    scan_cycle         <= 5'd0;
                    if (dac_ready) begin
                        state <= SEQUENCE_MODE ? ST_INIT_DELAY : ST_FPGA_RESET;
                    end
                end

                ST_INIT_DELAY: begin
                    caravel_resetb_r   <= 1'b1;
                    caravel_tm_r       <= 1'b0;
                    caravel_scan_se_r  <= 1'b1;
                    caravel_scan_si_r  <= 1'b0;
                    caravel_scan_cc_r  <= 1'b0;
                    busy_o             <= 1'b0;
                    done_o             <= 1'b0;
                    ready_low_seen_r   <= 1'b0;
                    scan_cycle         <= 5'd0;
                    seq_row            <= SEQ_START_ROW;
                    seq_col            <= SEQ_START_COL;
                    seq_finished       <= 1'b0;
                    if (wait_count >= INITIAL_SEQUENCE_DELAY_CYCLES) begin
                        wait_count <= 32'd0;
                        state      <= ST_FPGA_RESET;
                    end else begin
                        wait_count <= wait_count + 32'd1;
                    end
                end

                ST_FPGA_RESET: begin
                    caravel_resetb_r   <= 1'b0;
                    caravel_tm_r       <= 1'b0;
                    caravel_scan_se_r  <= 1'b1;
                    caravel_scan_si_r  <= 1'b0;
                    caravel_scan_cc_r  <= 1'b0;
                    busy_o             <= 1'b0;
                    done_o             <= 1'b0;
                    ready_low_seen_r   <= 1'b0;
                    scan_cycle         <= 5'd0;
                    if (wait_count >= FPGA_RESET_ASSERT_CYCLES) begin
                        caravel_resetb_r <= 1'b1;
                        wait_count       <= 32'd0;
                        state            <= ST_RESET_WAIT;
                    end else begin
                        wait_count <= wait_count + 32'd1;
                    end
                end

                ST_RESET_WAIT: begin
                    caravel_resetb_r   <= 1'b1;
                    caravel_tm_r      <= 1'b0;
                    caravel_scan_se_r <= 1'b1;
                    caravel_scan_si_r <= 1'b0;
                    caravel_scan_cc_r <= 1'b0;
                    busy_o            <= 1'b0;
                    done_o            <= 1'b0;
                    scan_cycle        <= 5'd0;
                    if (ready_low_seen_r && caravel_ready_i) begin
                        ready_low_seen_r <= 1'b0;
                        wait_count       <= 32'd0;
                        state            <= ST_POST_WAIT;
                    end else if (wait_count >= RESET_RELEASE_FALLBACK_CYCLES) begin
                        ready_low_seen_r <= 1'b0;
                        wait_count <= 32'd0;
                        state      <= ST_POST_WAIT;
                    end else begin
                        if (!caravel_ready_i) begin
                            ready_low_seen_r <= 1'b1;
                        end
                        wait_count <= wait_count + 32'd1;
                    end
                end

                ST_POST_WAIT: begin
                    caravel_resetb_r   <= 1'b1;
                    caravel_tm_r      <= 1'b0;
                    caravel_scan_se_r <= 1'b1;
                    caravel_scan_si_r <= 1'b0;
                    caravel_scan_cc_r <= 1'b0;
                    busy_o            <= 1'b0;
                    done_o            <= 1'b0;
                    ready_low_seen_r  <= 1'b0;
                    if (wait_count >= POST_RESET_WAIT_CYCLES) begin
                        wait_count <= 32'd0;
                        state      <= ST_TM_SETUP;
                    end else begin
                        wait_count <= wait_count + 32'd1;
                    end
                end

                ST_TM_SETUP: begin
                    caravel_resetb_r   <= 1'b1;
                    caravel_tm_r      <= 1'b1;
                    caravel_scan_se_r <= 1'b1;
                    caravel_scan_si_r <= 1'b0;
                    caravel_scan_cc_r <= 1'b0;
                    busy_o            <= 1'b1;
                    done_o            <= 1'b0;
                    ready_low_seen_r  <= 1'b0;
                    if (wait_count >= TM_SETUP_CYCLES) begin
                        wait_count <= 32'd0;
                        scan_cycle <= 5'd0;
                        state      <= ST_SHIFT;
                    end else begin
                        wait_count <= wait_count + 32'd1;
                    end
                end

                ST_SHIFT: begin
                    caravel_resetb_r   <= 1'b1;
                    caravel_tm_r      <= 1'b1;
                    caravel_scan_se_r <= 1'b0;
                    caravel_scan_cc_r <= 1'b0;
                    busy_o            <= 1'b1;
                    done_o            <= 1'b0;
                    ready_low_seen_r  <= 1'b0;

                    if (scan_cycle == 5'd0) begin
                        // Dummy/setup bit: this enables the scan-debug internal counter.
                        caravel_scan_si_r <= 1'b0;
                    end else if (scan_cycle <= 5'd16) begin
                        caravel_scan_si_r <= active_scan_word[scan_cycle - 5'd1];
                    end else begin
                        caravel_scan_si_r <= 1'b0;
                    end

                    if (scan_cycle == 5'd17) begin
                        wait_count <= 32'd0;
                        state      <= ST_TM_TAIL;
                    end else begin
                        scan_cycle <= scan_cycle + 5'd1;
                    end
                end

                ST_TM_TAIL: begin
                    caravel_resetb_r   <= 1'b1;
                    caravel_tm_r      <= 1'b1;
                    caravel_scan_se_r <= 1'b1;
                    caravel_scan_si_r <= 1'b0;
                    caravel_scan_cc_r <= 1'b0;
                    busy_o            <= 1'b1;
                    done_o            <= 1'b0;
                    ready_low_seen_r  <= 1'b0;
                    if (wait_count >= POST_DR_TM_HOLD_CYCLES - 1'b1) begin
                        wait_count <= 32'd0;
                        state      <= ST_DONE;
                    end else begin
                        wait_count <= wait_count + 32'd1;
                    end
                end

                ST_DONE: begin
                    caravel_resetb_r   <= 1'b1;
                    caravel_tm_r      <= 1'b0;
                    caravel_scan_se_r <= 1'b1;
                    caravel_scan_si_r <= 1'b0;
                    caravel_scan_cc_r <= 1'b0;
                    busy_o            <= 1'b0;
                    done_o            <= 1'b1;
                    ready_low_seen_r  <= 1'b0;
                    if (SEQUENCE_MODE && seq_finished) begin
                        state <= ST_DONE;
                    end else if (REPEAT_AFTER_DONE_CYCLES != 32'd0) begin
                        if (wait_count >= REPEAT_AFTER_DONE_CYCLES) begin
                            wait_count <= 32'd0;
                            scan_cycle <= 5'd0;
                            if (SEQUENCE_MODE) begin
                                if (seq_col == 5'd0 && seq_row < 5'd31) begin
                                    seq_row <= seq_row + 5'd1;
                                end else if (seq_col == 5'd0) begin
                                    seq_row <= 5'd0;
                                    seq_col <= 5'd1;
                                end else if (seq_row < 5'd31) begin
                                    seq_row <= seq_row + 5'd1;
                                end else if (seq_col < 5'd31) begin
                                    seq_row <= 5'd0;
                                    seq_col <= seq_col + 5'd1;
                                end else begin
                                    seq_finished <= 1'b1;
                                    state        <= ST_DONE;
                                end
                            end
                            if (!SEQUENCE_MODE || seq_col != 5'd31 || seq_row != 5'd31) begin
                                state <= ST_FPGA_RESET;
                            end
                        end else begin
                            wait_count <= wait_count + 32'd1;
                            state      <= ST_DONE;
                        end
                    end else begin
                        state <= ST_DONE;
                    end
                end

                default: begin
                    caravel_resetb_r   <= 1'b0;
                    caravel_tm_r      <= 1'b0;
                    caravel_scan_se_r <= 1'b1;
                    caravel_scan_si_r <= 1'b0;
                    caravel_scan_cc_r <= 1'b0;
                    busy_o            <= 1'b0;
                    done_o            <= 1'b0;
                    ready_low_seen_r  <= 1'b0;
                    state             <= ST_DAC_INIT;
                    wait_count        <= 32'd0;
                    scan_cycle        <= 5'd0;
                    seq_row           <= SEQ_START_ROW;
                    seq_col           <= SEQ_START_COL;
                    seq_finished      <= 1'b0;
                end
            endcase
    end
endmodule
