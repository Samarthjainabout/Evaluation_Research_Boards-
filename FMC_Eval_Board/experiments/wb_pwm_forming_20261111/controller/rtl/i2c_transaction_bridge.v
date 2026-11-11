module i2c_transaction_bridge #(
    parameter integer CLK_HZ = 200000000,
    parameter integer BUS_HZ = 100000,
    parameter integer TIMEOUT_CYCLES = 2000000
) (
    input wire clk,
    input wire reset,
    input wire [127:0] command,
    output wire [127:0] status,
    inout wire scl,
    inout wire sda
);
    localparam integer HALF_CYCLES = CLK_HZ / (2 * BUS_HZ);
    localparam [4:0] IDLE=0, CHECK_BUS=1, START_HOLD=2,
        TX_LOW=3, TX_HIGH=4, ACK_LOW=5, ACK_HIGH=6,
        RESTART_LOW=7, RESTART_HIGH=8, RX_LOW=9, RX_HIGH=10,
        RX_ACK_LOW=11, RX_ACK_HIGH=12, STOP_LOW=13,
        STOP_HIGH=14, STOP_RELEASE=15, WAIT_RISE=16;
    localparam [5:0] ERR_LENGTH=1, ERR_BUSY=2, ERR_STRETCH=3,
        ERR_ADDRESS_W=4, ERR_WRITE=5, ERR_ADDRESS_R=6, ERR_ARBITRATION=7,
        ERR_MACRO_FORMAT=8, ERR_MACRO_VERIFY=9;

    reg scl_low = 0, sda_low = 0;
    (* ASYNC_REG = "TRUE" *) reg [1:0] scl_sync = 2'b11;
    (* ASYNC_REG = "TRUE" *) reg [1:0] sda_sync = 2'b11;
    assign scl = scl_low ? 1'b0 : 1'bz;
    assign sda = sda_low ? 1'b0 : 1'bz;
    always @(posedge clk) begin
        scl_sync <= {scl_sync[0], scl};
        sda_sync <= {sda_sync[0], sda};
    end

    reg [4:0] state = IDLE, after_rise = IDLE;
    reg [31:0] phase_count = 0, wait_count = 0;
    reg [7:0] last_seen = 0, active_token = 0, completed_token = 0;
    reg busy = 0;
    reg [5:0] error_code = 0;
    reg [6:0] address = 0;
    reg [3:0] write_length = 0, read_length = 0;
    reg [63:0] write_data = 0, read_data = 0;
    reg [7:0] acked_write_count = 0;
    reg [7:0] tx_byte = 0;
    reg [2:0] bit_index = 7;
    reg [3:0] write_index = 0, read_index = 0;
    // 0=write address, 1=write payload, 2=read address.
    reg [1:0] tx_kind = 0;
    // Protected Si5351 clock-profile macro.  One VIO command performs the
    // complete mask/divider/reset/unmask transaction and reads back status,
    // output mask, CLK0 divider and CLK1 divider before reporting completion.
    // Status is polled locally after PLL reset until the crystal/PLLA lock
    // bits clear, with a hard finite bound; the host never sleeps or retries.
    // command[127:112]=16'hC535, [111]=10 MHz (0 selects 2 MHz),
    // command[110:8]=0 and command[7:0] remains the ordinary host token.
    reg clock_macro_active = 0, clock_macro_receipt_valid = 0;
    reg [3:0] clock_macro_step = 0;
    reg [7:0] clock_macro_divider = 0;
    reg [11:0] clock_macro_poll_count = 0;
    reg [31:0] clock_macro_readback = 0;
    wire [63:0] status_read_data = clock_macro_receipt_valid ?
                                     {32'h434C4B31,clock_macro_readback} : read_data;
    assign status = {38'b0, sda_sync[1], scl_sync[1],
                     acked_write_count, status_read_data, error_code,
                     (error_code != 0), busy, completed_token};

    task load_clock_macro_step;
        input [3:0] next_step;
        input [7:0] target_divider;
        reg [7:0] target_register;
        reg [7:0] target_value;
        reg target_read;
        begin
            target_register = 0;
            target_value = 0;
            target_read = 0;
            case (next_step)
                0: begin target_register=8'd3;   target_value=8'd255; end
                1: begin target_register=8'd45;  target_value=target_divider; end
                2: begin target_register=8'd53;  target_value=target_divider; end
                3: begin target_register=8'd177; target_value=8'd32; end
                4: begin target_register=8'd3;   target_value=8'd252; end
                5: begin target_register=8'd0;   target_read=1; end
                6: begin target_register=8'd3;   target_read=1; end
                7: begin target_register=8'd45;  target_read=1; end
                8: begin target_register=8'd53;  target_read=1; end
                default: begin target_register=0; target_value=0; end
            endcase
            address <= 7'h60;
            write_length <= target_read ? 1 : 2;
            read_length <= target_read ? 1 : 0;
            write_data <= {48'd0,target_value,target_register};
            read_data <= 0;
            acked_write_count <= 0;
            write_index <= 0;
            read_index <= 0;
            tx_byte <= {7'h60,1'b0};
            tx_kind <= 0;
            bit_index <= 7;
            phase_count <= 0;
            wait_count <= 0;
            busy <= 1;
            state <= CHECK_BUS;
        end
    endtask

    task finish;
        begin
            scl_low <= 0;
            sda_low <= 0;
            completed_token <= active_token;
            busy <= 0;
            state <= IDLE;
        end
    endtask

    // The only path into this task is after finishing an ACK clock low.
    task after_write;
        begin
            phase_count <= 0;
            if (read_length != 0) begin
                state <= RESTART_LOW;
                sda_low <= 0;
                tx_byte <= {address, 1'b1};
                tx_kind <= 2;
                bit_index <= 7;
            end else begin
                state <= STOP_LOW;
                sda_low <= 1;
            end
        end
    endtask

    always @(posedge clk) begin
        if (reset) begin
            state <= IDLE;
            after_rise <= IDLE;
            scl_low <= 0;
            sda_low <= 0;
            phase_count <= 0;
            wait_count <= 0;
            last_seen <= 0;
            active_token <= 0;
            completed_token <= 0;
            busy <= 0;
            error_code <= 0;
            address <= 0;
            write_length <= 0;
            read_length <= 0;
            write_data <= 0;
            read_data <= 0;
            acked_write_count <= 0;
            tx_byte <= 0;
            bit_index <= 7;
            write_index <= 0;
            read_index <= 0;
            tx_kind <= 0;
            clock_macro_active <= 0;
            clock_macro_receipt_valid <= 0;
            clock_macro_step <= 0;
            clock_macro_divider <= 0;
            clock_macro_poll_count <= 0;
            clock_macro_readback <= 0;
        end else begin
            case (state)
                IDLE: begin
                    scl_low <= 0;
                    sda_low <= 0;
                    if (command[7:0] != 0 && command[7:0] != last_seen) begin
                        active_token <= command[7:0];
                        last_seen <= command[7:0];
                        error_code <= 0;
                        if(command[127:112]==16'hC535) begin
                            if(command[110:8]!=0) begin
                                clock_macro_active <= 0;
                                clock_macro_receipt_valid <= 0;
                                error_code <= ERR_MACRO_FORMAT;
                                completed_token <= command[7:0];
                                busy <= 0;
                            end else begin
                                clock_macro_active <= 1;
                                clock_macro_receipt_valid <= 0;
                                clock_macro_step <= 0;
                                clock_macro_divider <= command[111] ? 8'd43 : 8'd223;
                                clock_macro_poll_count <= 0;
                                clock_macro_readback <= 0;
                                load_clock_macro_step(0,command[111] ? 8'd43 : 8'd223);
                            end
                        end else begin
                            clock_macro_active <= 0;
                            clock_macro_receipt_valid <= 0;
                            address <= command[14:8];
                            write_length <= command[18:15];
                            read_length <= command[22:19];
                            write_data <= command[86:23];
                            read_data <= 0;
                            acked_write_count <= 0;
                            write_index <= 0;
                            read_index <= 0;
                            // A zero-length write plus a nonzero read is a true
                            // read-only transaction (needed for PCA9548A readback).
                            if (command[18:15] == 0 && command[22:19] != 0) begin
                                tx_byte <= {command[14:8], 1'b1};
                                tx_kind <= 2;
                            end else begin
                                tx_byte <= {command[14:8], 1'b0};
                                tx_kind <= 0;
                            end
                            bit_index <= 7;
                            phase_count <= 0;
                            wait_count <= 0;
                            busy <= 1;
                            state <= CHECK_BUS;
                        end
                    end
                end
                CHECK_BUS: begin
                    // Remain entirely released until a full idle half-period.
                    if (write_length > 8 || read_length > 8) begin
                        error_code <= ERR_LENGTH;
                        finish;
                    end else if (wait_count >= TIMEOUT_CYCLES-1) begin
                        error_code <= ERR_BUSY;
                        finish;
                    end else if (scl_sync[1] && sda_sync[1]) begin
                        if (phase_count == HALF_CYCLES-1) begin
                            sda_low <= 1;
                            phase_count <= 0;
                            state <= START_HOLD;
                        end else phase_count <= phase_count + 1'b1;
                        wait_count <= wait_count + 1'b1;
                    end else begin
                        phase_count <= 0;
                        wait_count <= wait_count + 1'b1;
                    end
                end
                START_HOLD: begin
                    if (phase_count == HALF_CYCLES-1) begin
                        scl_low <= 1;
                        phase_count <= 0;
                        state <= TX_LOW;
                    end else phase_count <= phase_count + 1'b1;
                end
                TX_LOW: begin
                    sda_low <= !tx_byte[bit_index];
                    if (phase_count == HALF_CYCLES-1) begin
                        scl_low <= 0;
                        wait_count <= 0;
                        after_rise <= TX_HIGH;
                        state <= WAIT_RISE;
                    end else phase_count <= phase_count + 1'b1;
                end
                TX_HIGH: begin
                    if (phase_count == HALF_CYCLES-1) begin
                        phase_count <= 0;
                        if (tx_byte[bit_index] && !sda_sync[1]) begin
                            // Another device holds SDA low: relinquish the bus.
                            error_code <= ERR_ARBITRATION;
                            finish;
                        end else begin
                            scl_low <= 1;
                            if (bit_index == 0) state <= ACK_LOW;
                            else begin
                                bit_index <= bit_index - 1'b1;
                                state <= TX_LOW;
                            end
                        end
                    end else phase_count <= phase_count + 1'b1;
                end
                ACK_LOW: begin
                    sda_low <= 0;
                    if (phase_count == HALF_CYCLES-1) begin
                        scl_low <= 0;
                        wait_count <= 0;
                        after_rise <= ACK_HIGH;
                        state <= WAIT_RISE;
                    end else phase_count <= phase_count + 1'b1;
                end
                ACK_HIGH: begin
                    if (phase_count == HALF_CYCLES-1) begin
                        scl_low <= 1;
                        phase_count <= 0;
                        bit_index <= 7;
                        if (sda_sync[1]) begin
                            case (tx_kind)
                                0: error_code <= ERR_ADDRESS_W;
                                1: error_code <= ERR_WRITE;
                                2: error_code <= ERR_ADDRESS_R;
                            endcase
                            state <= STOP_LOW;
                        end else if (tx_kind == 0) begin
                            if (write_length != 0) begin
                                tx_byte <= write_data[7:0];
                                tx_kind <= 1;
                                state <= TX_LOW;
                            end else after_write;
                        end else if (tx_kind == 1) begin
                            acked_write_count <= acked_write_count + 1'b1;
                            if (write_index + 1 < write_length) begin
                                write_index <= write_index + 1'b1;
                                write_data <= write_data >> 8;
                                tx_byte <= write_data[15:8];
                                state <= TX_LOW;
                            end else after_write;
                        end else begin
                            state <= RX_LOW;
                            read_index <= 0;
                        end
                    end else phase_count <= phase_count + 1'b1;
                end
                RESTART_LOW: begin
                    sda_low <= 0;
                    if (phase_count == HALF_CYCLES-1) begin
                        scl_low <= 0;
                        wait_count <= 0;
                        after_rise <= RESTART_HIGH;
                        state <= WAIT_RISE;
                    end else phase_count <= phase_count + 1'b1;
                end
                RESTART_HIGH: begin
                    if (phase_count == HALF_CYCLES-1) begin
                        sda_low <= 1;
                        phase_count <= 0;
                        state <= START_HOLD;
                    end else phase_count <= phase_count + 1'b1;
                end
                RX_LOW: begin
                    sda_low <= 0;
                    if (phase_count == HALF_CYCLES-1) begin
                        scl_low <= 0;
                        wait_count <= 0;
                        after_rise <= RX_HIGH;
                        state <= WAIT_RISE;
                    end else phase_count <= phase_count + 1'b1;
                end
                RX_HIGH: begin
                    if (phase_count == HALF_CYCLES-1) begin
                        read_data[read_index*8 + bit_index] <= sda_sync[1];
                        scl_low <= 1;
                        phase_count <= 0;
                        if (bit_index == 0) state <= RX_ACK_LOW;
                        else begin
                            bit_index <= bit_index - 1'b1;
                            state <= RX_LOW;
                        end
                    end else phase_count <= phase_count + 1'b1;
                end
                RX_ACK_LOW: begin
                    sda_low <= (read_index + 1 < read_length);
                    if (phase_count == HALF_CYCLES-1) begin
                        scl_low <= 0;
                        wait_count <= 0;
                        after_rise <= RX_ACK_HIGH;
                        state <= WAIT_RISE;
                    end else phase_count <= phase_count + 1'b1;
                end
                RX_ACK_HIGH: begin
                    if (phase_count == HALF_CYCLES-1) begin
                        scl_low <= 1;
                        phase_count <= 0;
                        if (read_index + 1 < read_length) begin
                            read_index <= read_index + 1'b1;
                            bit_index <= 7;
                            state <= RX_LOW;
                        end else state <= STOP_LOW;
                    end else phase_count <= phase_count + 1'b1;
                end
                STOP_LOW: begin
                    scl_low <= 1;
                    sda_low <= 1;
                    if (phase_count == HALF_CYCLES-1) begin
                        scl_low <= 0;
                        wait_count <= 0;
                        after_rise <= STOP_HIGH;
                        state <= WAIT_RISE;
                    end else phase_count <= phase_count + 1'b1;
                end
                STOP_HIGH: begin
                    if (phase_count == HALF_CYCLES-1) begin
                        sda_low <= 0;
                        phase_count <= 0;
                        state <= STOP_RELEASE;
                    end else phase_count <= phase_count + 1'b1;
                end
                STOP_RELEASE: begin
                    if (phase_count == HALF_CYCLES-1) begin
                        if(clock_macro_active) begin
                            if(error_code!=0) begin
                                clock_macro_active <= 0;
                                clock_macro_receipt_valid <= 1;
                                finish;
                            end else if(clock_macro_step==5 &&
                                        (read_data[7:0] & 8'hA8)!=0) begin
                                if(clock_macro_poll_count==12'hFFF) begin
                                    error_code <= ERR_MACRO_VERIFY;
                                    clock_macro_active <= 0;
                                    clock_macro_receipt_valid <= 1;
                                    finish;
                                end else begin
                                    clock_macro_poll_count <= clock_macro_poll_count+1'b1;
                                    load_clock_macro_step(5,clock_macro_divider);
                                end
                            end else if(clock_macro_step<8) begin
                                if(clock_macro_step==5) clock_macro_readback[7:0] <= read_data[7:0];
                                else if(clock_macro_step==6) clock_macro_readback[15:8] <= read_data[7:0];
                                else if(clock_macro_step==7) clock_macro_readback[23:16] <= read_data[7:0];
                                clock_macro_step <= clock_macro_step+1'b1;
                                load_clock_macro_step(clock_macro_step+1'b1,
                                                      clock_macro_divider);
                            end else begin
                                clock_macro_readback[31:24] <= read_data[7:0];
                                if((clock_macro_readback[7:0] & 8'hA8)!=0 ||
                                   clock_macro_readback[15:8]!=8'd252 ||
                                   clock_macro_readback[23:16]!=clock_macro_divider ||
                                   read_data[7:0]!=clock_macro_divider)
                                    error_code <= ERR_MACRO_VERIFY;
                                clock_macro_active <= 0;
                                clock_macro_receipt_valid <= 1;
                                finish;
                            end
                        end else finish;
                    end
                    else phase_count <= phase_count + 1'b1;
                end
                WAIT_RISE: begin
                    if (scl_sync[1]) begin
                        phase_count <= 0;
                        state <= after_rise;
                    end else if (wait_count == TIMEOUT_CYCLES-1) begin
                        if (error_code == 0) error_code <= ERR_STRETCH;
                        if (after_rise == STOP_HIGH) finish;
                        else begin
                            // Attempt a STOP; a second timeout releases both pins.
                            scl_low <= 1;
                            sda_low <= 1;
                            phase_count <= 0;
                            state <= STOP_LOW;
                        end
                    end else wait_count <= wait_count + 1'b1;
                end
                default: begin
                    error_code <= ERR_BUSY;
                    finish;
                end
            endcase
        end
    end
endmodule

