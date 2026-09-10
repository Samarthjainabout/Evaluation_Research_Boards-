`timescale 1ns / 1ps

// Configure the bench Si5351 from the AX7020's independent 50 MHz PL clock.
// CLK0 goes directly to Caravel and CLK1 returns to wb_clk_i on J10-16.
// The register values intentionally match the previously verified Teensy
// Adafruit_SI5351 setup: 25 MHz * 36 = 900 MHz, divided by 450 for
// scan-debug (2 MHz) or by 90 for native Wishbone (10 MHz).
module si5351_mode_controller #(
    parameter integer SYS_CLK_HZ = 50000000,
    parameter integer I2C_HZ = 400000,
    parameter integer POWERUP_DELAY_CYCLES = 2500000,
    parameter integer SETTLE_DELAY_CYCLES = 250000
) (
    input  wire sys_clk_i,
    input  wire mode_10m_req_i,
    inout  wire si5351_sda_io,
    output wire si5351_scl_io,
    output reg  ready_o = 1'b0,
    output reg  active_10m_o = 1'b0,
    output reg  busy_o = 1'b1,
    output reg  error_o = 1'b0
);
    localparam [2:0] CFG_POWERUP = 3'd0;
    localparam [2:0] CFG_ISSUE   = 3'd1;
    localparam [2:0] CFG_WAIT    = 3'd2;
    localparam [2:0] CFG_SETTLE  = 3'd3;
    localparam [2:0] CFG_READY   = 3'd4;

    reg [2:0] cfg_state = CFG_POWERUP;
    reg [31:0] delay_count = 32'd0;
    reg [5:0] sequence_index = 6'd0;
    reg full_initialize = 1'b1;
    reg target_10m = 1'b0;
    reg i2c_start = 1'b0;

    reg mode_meta = 1'b0;
    reg mode_sync = 1'b0;
    always @(posedge sys_clk_i) begin
        mode_meta <= mode_10m_req_i;
        mode_sync <= mode_meta;
    end

    wire i2c_busy;
    wire i2c_done;
    wire i2c_ack_error;
    reg [7:0] current_reg;
    reg [7:0] current_data;
    integer sequence_offset;
    integer byte_offset;

    // One register is written per I2C transaction. This is slower than a
    // burst but keeps the controller compact and makes every ACK observable.
    always @* begin
        current_reg = 8'd3;
        current_data = 8'hFF;
        sequence_offset = 0;
        byte_offset = 0;
        if (full_initialize) begin
            if (sequence_index == 6'd0) begin
                current_reg = 8'd3;
                current_data = 8'hFF;
            end else if (sequence_index >= 6'd1 && sequence_index <= 6'd8) begin
                current_reg = 8'd15 + sequence_index;
                current_data = 8'h80;
            end else if (sequence_index == 6'd9) begin
                current_reg = 8'd183;
                current_data = 8'hD2;
            end else if (sequence_index == 6'd10) begin
                current_reg = 8'd149;
                current_data = 8'h00;
            end else if (sequence_index >= 6'd11 && sequence_index <= 6'd18) begin
                current_reg = 8'd26 + (sequence_index - 6'd11);
                case (sequence_index - 6'd11)
                    0: current_data = 8'h00;
                    1: current_data = 8'h01;
                    2: current_data = 8'h00;
                    3: current_data = 8'h10;
                    default: current_data = 8'h00;
                endcase
            end else if (sequence_index >= 6'd19 && sequence_index <= 6'd42) begin
                sequence_offset = sequence_index - 6'd19;
                current_reg = 8'd42 + sequence_offset;
                byte_offset = sequence_offset & 7;
                case (byte_offset)
                    0: current_data = 8'h00;
                    1: current_data = 8'h01;
                    2: current_data = 8'h00;
                    3: current_data = target_10m ? 8'h2B : 8'hDF;
                    default: current_data = 8'h00;
                endcase
            end else if (sequence_index >= 6'd43 && sequence_index <= 6'd45) begin
                current_reg = 8'd16 + (sequence_index - 6'd43);
                current_data = 8'h4F;
            end else if (sequence_index == 6'd46) begin
                current_reg = 8'd177;
                current_data = 8'hAC;
            end else begin
                current_reg = 8'd3;
                current_data = 8'hF8;
            end
        end else begin
            if (sequence_index == 6'd0) begin
                current_reg = 8'd3;
                current_data = 8'hFF;
            end else if (sequence_index >= 6'd1 && sequence_index <= 6'd24) begin
                sequence_offset = sequence_index - 6'd1;
                current_reg = 8'd42 + sequence_offset;
                byte_offset = sequence_offset & 7;
                case (byte_offset)
                    0: current_data = 8'h00;
                    1: current_data = 8'h01;
                    2: current_data = 8'h00;
                    3: current_data = target_10m ? 8'h2B : 8'hDF;
                    default: current_data = 8'h00;
                endcase
            end else if (sequence_index == 6'd25) begin
                current_reg = 8'd177;
                current_data = 8'hAC;
            end else begin
                current_reg = 8'd3;
                current_data = 8'hF8;
            end
        end
    end

    i2c_register_writer #(
        .SYS_CLK_HZ(SYS_CLK_HZ),
        .I2C_HZ(I2C_HZ),
        .DEVICE_WRITE_ADDRESS(8'hC0)
    ) i2c_writer (
        .clk_i(sys_clk_i),
        .start_i(i2c_start),
        .register_i(current_reg),
        .data_i(current_data),
        .sda_io(si5351_sda_io),
        .scl_io(si5351_scl_io),
        .busy_o(i2c_busy),
        .done_o(i2c_done),
        .ack_error_o(i2c_ack_error)
    );

    always @(posedge sys_clk_i) begin
        i2c_start <= 1'b0;
        case (cfg_state)
            CFG_POWERUP: begin
                ready_o <= 1'b0;
                busy_o <= 1'b1;
                if (delay_count + 32'd1 >= POWERUP_DELAY_CYCLES) begin
                    delay_count <= 32'd0;
                    sequence_index <= 6'd0;
                    full_initialize <= 1'b1;
                    target_10m <= 1'b0;
                    cfg_state <= CFG_ISSUE;
                end else begin
                    delay_count <= delay_count + 32'd1;
                end
            end

            CFG_ISSUE: begin
                if (!i2c_busy) begin
                    i2c_start <= 1'b1;
                    cfg_state <= CFG_WAIT;
                end
            end

            CFG_WAIT: begin
                if (i2c_done) begin
                    if (i2c_ack_error)
                        error_o <= 1'b1;
                    if (sequence_index == (full_initialize ? 6'd47 : 6'd26)) begin
                        delay_count <= 32'd0;
                        cfg_state <= CFG_SETTLE;
                    end else begin
                        sequence_index <= sequence_index + 6'd1;
                        cfg_state <= CFG_ISSUE;
                    end
                end
            end

            CFG_SETTLE: begin
                if (delay_count + 32'd1 >= SETTLE_DELAY_CYCLES) begin
                    delay_count <= 32'd0;
                    active_10m_o <= target_10m;
                    ready_o <= !error_o;
                    busy_o <= 1'b0;
                    cfg_state <= CFG_READY;
                end else begin
                    delay_count <= delay_count + 32'd1;
                end
            end

            CFG_READY: begin
                if (mode_sync != active_10m_o) begin
                    ready_o <= 1'b0;
                    busy_o <= 1'b1;
                    error_o <= 1'b0;
                    target_10m <= mode_sync;
                    full_initialize <= 1'b0;
                    sequence_index <= 6'd0;
                    cfg_state <= CFG_ISSUE;
                end
            end

            default: cfg_state <= CFG_POWERUP;
        endcase
    end
endmodule

// Open-drain, write-only I2C master for one address/register/data transaction.
module i2c_register_writer #(
    parameter integer SYS_CLK_HZ = 50000000,
    parameter integer I2C_HZ = 400000,
    parameter [7:0] DEVICE_WRITE_ADDRESS = 8'hC0
) (
    input  wire       clk_i,
    input  wire       start_i,
    input  wire [7:0] register_i,
    input  wire [7:0] data_i,
    inout  wire       sda_io,
    output wire       scl_io,
    output reg        busy_o = 1'b0,
    output reg        done_o = 1'b0,
    output reg        ack_error_o = 1'b0
);
    localparam integer HALF_PERIOD_CYCLES = SYS_CLK_HZ / (I2C_HZ * 2);
    localparam [3:0] I2C_IDLE         = 4'd0;
    localparam [3:0] I2C_START_HIGH   = 4'd1;
    localparam [3:0] I2C_START_LOW    = 4'd2;
    localparam [3:0] I2C_BIT_LOW      = 4'd3;
    localparam [3:0] I2C_BIT_HIGH     = 4'd4;
    localparam [3:0] I2C_BIT_ADVANCE  = 4'd5;
    localparam [3:0] I2C_ACK_LOW      = 4'd6;
    localparam [3:0] I2C_ACK_HIGH     = 4'd7;
    localparam [3:0] I2C_ACK_SAMPLE   = 4'd8;
    localparam [3:0] I2C_STOP_LOW     = 4'd9;
    localparam [3:0] I2C_STOP_HIGH    = 4'd10;
    localparam [3:0] I2C_STOP_RELEASE = 4'd11;

    reg [3:0] state = I2C_IDLE;
    reg [15:0] divider_count = 16'd0;
    reg [1:0] byte_index = 2'd0;
    reg [2:0] bit_index = 3'd7;
    reg [7:0] register_latched = 8'd0;
    reg [7:0] data_latched = 8'd0;
    reg sda_drive_low = 1'b0;
    reg scl_drive_low = 1'b0;

    wire [7:0] active_byte = byte_index == 2'd0 ? DEVICE_WRITE_ADDRESS
        : (byte_index == 2'd1 ? register_latched : data_latched);
    wire half_period_tick = divider_count + 16'd1 >= HALF_PERIOD_CYCLES;

    assign sda_io = sda_drive_low ? 1'b0 : 1'bz;
    assign scl_io = scl_drive_low ? 1'b0 : 1'bz;

    always @(posedge clk_i) begin
        done_o <= 1'b0;
        if (state == I2C_IDLE) begin
            divider_count <= 16'd0;
            sda_drive_low <= 1'b0;
            scl_drive_low <= 1'b0;
            if (start_i) begin
                register_latched <= register_i;
                data_latched <= data_i;
                byte_index <= 2'd0;
                bit_index <= 3'd7;
                ack_error_o <= 1'b0;
                busy_o <= 1'b1;
                state <= I2C_START_HIGH;
            end
        end else if (half_period_tick) begin
            divider_count <= 16'd0;
            case (state)
                I2C_START_HIGH: begin
                    sda_drive_low <= 1'b0;
                    scl_drive_low <= 1'b0;
                    state <= I2C_START_LOW;
                end
                I2C_START_LOW: begin
                    sda_drive_low <= 1'b1;
                    scl_drive_low <= 1'b0;
                    state <= I2C_BIT_LOW;
                end
                I2C_BIT_LOW: begin
                    scl_drive_low <= 1'b1;
                    sda_drive_low <= !active_byte[bit_index];
                    state <= I2C_BIT_HIGH;
                end
                I2C_BIT_HIGH: begin
                    scl_drive_low <= 1'b0;
                    state <= I2C_BIT_ADVANCE;
                end
                I2C_BIT_ADVANCE: begin
                    scl_drive_low <= 1'b1;
                    if (bit_index == 3'd0)
                        state <= I2C_ACK_LOW;
                    else begin
                        bit_index <= bit_index - 3'd1;
                        state <= I2C_BIT_LOW;
                    end
                end
                I2C_ACK_LOW: begin
                    scl_drive_low <= 1'b1;
                    sda_drive_low <= 1'b0;
                    state <= I2C_ACK_HIGH;
                end
                I2C_ACK_HIGH: begin
                    scl_drive_low <= 1'b0;
                    state <= I2C_ACK_SAMPLE;
                end
                I2C_ACK_SAMPLE: begin
                    if (sda_io)
                        ack_error_o <= 1'b1;
                    scl_drive_low <= 1'b1;
                    if (byte_index == 2'd2) begin
                        sda_drive_low <= 1'b1;
                        state <= I2C_STOP_LOW;
                    end else begin
                        byte_index <= byte_index + 2'd1;
                        bit_index <= 3'd7;
                        state <= I2C_BIT_LOW;
                    end
                end
                I2C_STOP_LOW: begin
                    scl_drive_low <= 1'b1;
                    sda_drive_low <= 1'b1;
                    state <= I2C_STOP_HIGH;
                end
                I2C_STOP_HIGH: begin
                    scl_drive_low <= 1'b0;
                    state <= I2C_STOP_RELEASE;
                end
                I2C_STOP_RELEASE: begin
                    sda_drive_low <= 1'b0;
                    busy_o <= 1'b0;
                    done_o <= 1'b1;
                    state <= I2C_IDLE;
                end
                default: state <= I2C_IDLE;
            endcase
        end else begin
            divider_count <= divider_count + 16'd1;
        end
    end
endmodule
