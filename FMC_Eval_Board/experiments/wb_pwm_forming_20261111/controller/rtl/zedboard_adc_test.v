`timescale 1ns/1ps

// ZedBoard FMC-LPC diagnostic image for the ADS5282 on
// CaravelReRAM-SenseEval. The control logic runs at 50 MHz; all
// cycle-counted timing parameters are scaled from the 200 MHz KC705 design.
//
// Safety invariant: both fixed-purpose adc_run_vio outputs power up as zero.
// While they are zero, the ADC and DAC clocks/data are idle and both devices
// are held in hardware reset.  No VIO bit provides arbitrary FMC pin control.
module zedboard_adc_test (
    input  wire sysclk_100mhz,
    output wire led0,
    inout wire iic_scl_main,
    inout wire iic_sda_main,

    input wire out4_p, input wire out4_n,
    input wire fpga_rx,
    inout wire scan_si,
    output wire fpga_mclkin_p,
    output wire fpga_mclkin_n,
    input wire out5_p, input wire out5_n,
    input wire ready_i,
    inout wire reset_b,
    input wire lclk_p, input wire lclk_n,
    input wire out2_p, input wire out2_n,
    input wire adclk_p, input wire adclk_n,
    input wire out1_p, input wire out1_n,
    input wire out3_p, input wire out3_n,
    input wire si5351_clk1_1v8,

    output wire fpga_tx,
    output wire scan_cc,
    output wire scan_se,
    output wire scan_tm,
    output wire aq1_sck,
    output wire aq1_mosi,
    output wire fpga_sck,
    output wire fpga_sdata,
    output wire adc_pd,
    output wire fpga_loe,
    output wire aq1_ncs,
    output wire aq1_nldac,
    output wire fpga_ncs,
    output wire fpga_nadcrst,
    output wire aq1_nrst
);
    wire sysclk_raw;
    wire sysclk;
    wire adc_clk_10m;
    wire pll_locked;
    reg [27:0] blink_count = 0;
    IBUF sysclk_input (.I(sysclk_100mhz), .O(sysclk_raw));
    always @(posedge sysclk) blink_count <= blink_count + 1'b1;

    // Added management-I2C transport; existing FMC ports and controllers unchanged.
    wire [127:0] i2c_command, i2c_status;
    reg [4:0] i2c_por_count = 0;
    always @(posedge sysclk)
        if (!i2c_por_count[4]) i2c_por_count <= i2c_por_count + 1'b1;
    vadj_vio u_i2c_vio (.clk(sysclk), .probe_out0(i2c_command), .probe_in0(i2c_status));
    i2c_transaction_bridge #(
        .CLK_HZ(50000000), .TIMEOUT_CYCLES(500000)
    ) i2c_bridge (
        .clk(sysclk), .reset(!i2c_por_count[4]),
        .command(i2c_command), .status(i2c_status),
        .scl(iic_scl_main), .sda(iic_sda_main)
    );
    // One PLL makes the 50 MHz control clock, 10 MHz ADC clock, and a separate
    // 200 MHz clock used only by the passive return-clock monitor.
    // The PLL may run while disabled; the ODDR output stays reset low.
    wire pll_feedback_unbuffered;
    wire pll_feedback;
    wire adc_clk_10m_unbuffered;
    wire sysclk_50m_unbuffered;
    wire monitor_clk_200m_unbuffered;
    wire monitor_clk_200m;
    PLLE2_BASE #(
        .BANDWIDTH("OPTIMIZED"),
        .CLKFBOUT_MULT(10),
        .CLKIN1_PERIOD(10.000),
        .CLKOUT0_DIVIDE(20),
        .CLKOUT1_DIVIDE(100),
        .CLKOUT2_DIVIDE(5),
        .DIVCLK_DIVIDE(1),
        .STARTUP_WAIT("FALSE")
    ) adc_clock_pll (
        .CLKIN1(sysclk_raw),
        .CLKFBIN(pll_feedback),
        .RST(1'b0),
        .PWRDWN(1'b0),
        .CLKFBOUT(pll_feedback_unbuffered),
        .CLKOUT0(sysclk_50m_unbuffered),
        .CLKOUT1(adc_clk_10m_unbuffered),
        .CLKOUT2(monitor_clk_200m_unbuffered),
        .LOCKED(pll_locked)
    );
    BUFG sysclk_global (.I(sysclk_50m_unbuffered), .O(sysclk));
    BUFG monitor_clock_buffer (.I(monitor_clk_200m_unbuffered), .O(monitor_clk_200m));
    BUFG pll_feedback_buffer (.I(pll_feedback_unbuffered), .O(pll_feedback));
    BUFG adc_clock_buffer (.I(adc_clk_10m_unbuffered), .O(adc_clk_10m));

    wire run_enable;
    wire dac_enable;
    wire [31:0] control_status;
    adc_run_vio u_run_vio (
        .clk(sysclk),
        .probe_in0(control_status),
        .probe_out0(run_enable),
        .probe_out1(dac_enable)
    );

    wire adc_clock_enable;
    wire adc_reset_n;
    wire spi_sclk;
    wire spi_sdata;
    wire spi_ncs;
    wire config_done;
    wire [2:0] sequence_index;
    wire [3:0] control_state;

    ads5282_control controller (
        .clk(sysclk),
        .run_enable(run_enable),
        .adc_clock_enable(adc_clock_enable),
        .adc_reset_n(adc_reset_n),
        .spi_sclk(spi_sclk),
        .spi_sdata(spi_sdata),
        .spi_ncs(spi_ncs),
        .config_done(config_done),
        .sequence_index(sequence_index),
        .state_debug(control_state)
    );

    // Evaluation-project scan profile adapted to the v1.2F PCB.
    // DAC2 is VCC_SET. NT12 removed; NT22 routes DAC14=3.0 V to VDDA1.
    // DAC15 remains 1.8 V. All user analog rails follow supply startup.
    // dac_enable is an independent VIO interlock and initializes to zero.
    wire dac_sclk;
    wire dac_sdata;
    wire dac_ncs;
    wire dac_nldac;
    wire dac_reset_n;
    wire dac_ready;
    wire [4:0] dac_frame_index;
    wire [2:0] dac_state;
    dac81416_pcb_scan_control dac_controller (
        .clk(sysclk),
        .enable(dac_enable),
        .sclk(dac_sclk),
        .sdata(dac_sdata),
        .ncs(dac_ncs),
        .nldac(dac_nldac),
        .reset_n(dac_reset_n),
        .ready(dac_ready),
        .frame_index(dac_frame_index),
        .state_debug(dac_state)
    );

    // Cleanly release the ODDR only on the 10 MHz clock domain.  Assertion
    // of the disabled state is asynchronous so a VIO stop immediately forces
    // the forwarded clock low.
    reg [1:0] adc_clock_enable_sync = 2'b00;
    always @(posedge adc_clk_10m or negedge adc_clock_enable) begin
        if (!adc_clock_enable)
            adc_clock_enable_sync <= 2'b00;
        else
            adc_clock_enable_sync <= {adc_clock_enable_sync[0], pll_locked};
    end
    wire adc_clock_running = adc_clock_enable_sync[1];
    wire adc_clock_forwarded;
`ifdef ZCU106
    ODDRE1 #(
        .SIM_DEVICE("ULTRASCALE_PLUS"),
        .SRVAL(1'b0)
    ) adc_clock_oddr (
        .Q(adc_clock_forwarded),
        .C(adc_clk_10m),
        .D1(1'b1),
        .D2(1'b0),
        .SR(~adc_clock_running)
    );
`else
    ODDR #(
        .DDR_CLK_EDGE("SAME_EDGE"),
        .INIT(1'b0),
        .SRTYPE("ASYNC")
    ) adc_clock_oddr (
        .Q(adc_clock_forwarded),
        .C(adc_clk_10m),
        .CE(1'b1),
        .D1(1'b1),
        .D2(1'b0),
        .R(~adc_clock_running),
        .S(1'b0)
    );
`endif
    OBUF adc_clock_p_output (.I(adc_clock_forwarded), .O(fpga_mclkin_p));
    OBUF adc_clock_n_reference (.I(1'b0), .O(fpga_mclkin_n));

    // ADS5282 return interface. LCLK is the DDR bit clock. For this bench test
    // with no external resistors, the ADC sequencer enables nominal 94-ohm
    // source termination before starting conversions. Receiver-end termination
    // remains preferable for a characterized LVDS link. FPGA DIFF_TERM stays
    // disabled in the unchanged XDC for the 1.8 V HR bank.
    wire out1, out2, out3, out4, out5, lclk, adclk;
    IBUFDS out1_input  (.I(out1_p),  .IB(out1_n),  .O(out1));
    IBUFDS out2_input  (.I(out2_p),  .IB(out2_n),  .O(out2));
    IBUFDS out3_input  (.I(out3_p),  .IB(out3_n),  .O(out3));
    IBUFDS out4_input  (.I(out4_p),  .IB(out4_n),  .O(out4));
    IBUFDS out5_input  (.I(out5_p),  .IB(out5_n),  .O(out5));
    IBUFDS lclk_input  (.I(lclk_p),  .IB(lclk_n),  .O(lclk));
    IBUFDS adclk_input (.I(adclk_p), .IB(adclk_n), .O(adclk));
    // Keep the source-synchronous capture path in the Bank-12 I/O region:
    // BUFIO drives every IDDR C pin, while the matching BYPASS BUFR drives the
    // shift registers and ILA.  This avoids the data/clock skew of a BUFG-only
    // receiver and leaves global clocks for non-I/O logic.
    wire lclk_io;
    wire lclk_fabric;
`ifdef ZCU106
    // UltraScale+ uses the native IDDRE1.  A global clock is sufficient for
    // this diagnostic capture and also clocks the downstream shift registers.
    BUFG lclk_global_buffer (.I(lclk), .O(lclk_fabric));
    assign lclk_io = lclk_fabric;
`else
    BUFIO lclk_io_buffer (.I(lclk), .O(lclk_io));
    BUFR #(.BUFR_DIVIDE("BYPASS"), .SIM_DEVICE("7SERIES")) lclk_fabric_buffer (
        .I(lclk), .CE(1'b1), .CLR(1'b0), .O(lclk_fabric)
    );
`endif

    wire [4:0] ddr_rise;
    wire [4:0] ddr_fall;
    wire adclk_rise;
    wire adclk_fall;
`ifdef ZCU106
    IDDRE1 #(.DDR_CLK_EDGE("SAME_EDGE_PIPELINED"), .IS_CB_INVERTED(1'b1))
        iddr_out1 (.Q1(ddr_rise[0]), .Q2(ddr_fall[0]), .C(lclk_io), .CB(lclk_io), .D(out1), .R(1'b0));
    IDDRE1 #(.DDR_CLK_EDGE("SAME_EDGE_PIPELINED"), .IS_CB_INVERTED(1'b1))
        iddr_out2 (.Q1(ddr_rise[1]), .Q2(ddr_fall[1]), .C(lclk_io), .CB(lclk_io), .D(out2), .R(1'b0));
    IDDRE1 #(.DDR_CLK_EDGE("SAME_EDGE_PIPELINED"), .IS_CB_INVERTED(1'b1))
        iddr_out3 (.Q1(ddr_rise[2]), .Q2(ddr_fall[2]), .C(lclk_io), .CB(lclk_io), .D(out3), .R(1'b0));
    IDDRE1 #(.DDR_CLK_EDGE("SAME_EDGE_PIPELINED"), .IS_CB_INVERTED(1'b1))
        iddr_out4 (.Q1(ddr_rise[3]), .Q2(ddr_fall[3]), .C(lclk_io), .CB(lclk_io), .D(out4), .R(1'b0));
    IDDRE1 #(.DDR_CLK_EDGE("SAME_EDGE_PIPELINED"), .IS_CB_INVERTED(1'b1))
        iddr_out5 (.Q1(ddr_rise[4]), .Q2(ddr_fall[4]), .C(lclk_io), .CB(lclk_io), .D(out5), .R(1'b0));
    IDDRE1 #(.DDR_CLK_EDGE("SAME_EDGE_PIPELINED"), .IS_CB_INVERTED(1'b1))
        iddr_adclk (.Q1(adclk_rise), .Q2(adclk_fall), .C(lclk_io), .CB(lclk_io), .D(adclk), .R(1'b0));
`else
    IDDR #(.DDR_CLK_EDGE("SAME_EDGE_PIPELINED"), .INIT_Q1(1'b0), .INIT_Q2(1'b0), .SRTYPE("SYNC"))
        iddr_out1 (.Q1(ddr_rise[0]), .Q2(ddr_fall[0]), .C(lclk_io), .CE(1'b1), .D(out1), .R(1'b0), .S(1'b0));
    IDDR #(.DDR_CLK_EDGE("SAME_EDGE_PIPELINED"), .INIT_Q1(1'b0), .INIT_Q2(1'b0), .SRTYPE("SYNC"))
        iddr_out2 (.Q1(ddr_rise[1]), .Q2(ddr_fall[1]), .C(lclk_io), .CE(1'b1), .D(out2), .R(1'b0), .S(1'b0));
    IDDR #(.DDR_CLK_EDGE("SAME_EDGE_PIPELINED"), .INIT_Q1(1'b0), .INIT_Q2(1'b0), .SRTYPE("SYNC"))
        iddr_out3 (.Q1(ddr_rise[2]), .Q2(ddr_fall[2]), .C(lclk_io), .CE(1'b1), .D(out3), .R(1'b0), .S(1'b0));
    IDDR #(.DDR_CLK_EDGE("SAME_EDGE_PIPELINED"), .INIT_Q1(1'b0), .INIT_Q2(1'b0), .SRTYPE("SYNC"))
        iddr_out4 (.Q1(ddr_rise[3]), .Q2(ddr_fall[3]), .C(lclk_io), .CE(1'b1), .D(out4), .R(1'b0), .S(1'b0));
    IDDR #(.DDR_CLK_EDGE("SAME_EDGE_PIPELINED"), .INIT_Q1(1'b0), .INIT_Q2(1'b0), .SRTYPE("SYNC"))
        iddr_out5 (.Q1(ddr_rise[4]), .Q2(ddr_fall[4]), .C(lclk_io), .CE(1'b1), .D(out5), .R(1'b0), .S(1'b0));
    IDDR #(.DDR_CLK_EDGE("SAME_EDGE_PIPELINED"), .INIT_Q1(1'b0), .INIT_Q2(1'b0), .SRTYPE("SYNC"))
        iddr_adclk (.Q1(adclk_rise), .Q2(adclk_fall), .C(lclk_io), .CE(1'b1), .D(adclk), .R(1'b0), .S(1'b0));
`endif

    // Six LCLK cycles collect twelve serialized bits on each lane.  A detected
    // ADCLK rising transition resets the pair phase so completed groups follow
    // the returned ADC frame clock rather than an arbitrary power-up phase.
    reg [11:0] shift1 = 0, shift2 = 0, shift3 = 0, shift4 = 0, shift5 = 0;
    reg [11:0] sample1 = 0, sample2 = 0, sample3 = 0, sample4 = 0, sample5 = 0;
    reg [2:0] pair_count = 0;
    reg previous_adclk_fall = 0;
    reg frame_locked = 0;
    wire adclk_rising_marker = (!previous_adclk_fall && adclk_rise) ||
                               (!adclk_rise && adclk_fall);
    always @(posedge lclk_fabric) begin
        previous_adclk_fall <= adclk_fall;
        if (adclk_rising_marker) begin
            shift1 <= {10'b0, ddr_rise[0], ddr_fall[0]};
            shift2 <= {10'b0, ddr_rise[1], ddr_fall[1]};
            shift3 <= {10'b0, ddr_rise[2], ddr_fall[2]};
            shift4 <= {10'b0, ddr_rise[3], ddr_fall[3]};
            shift5 <= {10'b0, ddr_rise[4], ddr_fall[4]};
            pair_count <= 1;
            frame_locked <= 1;
        end else if (frame_locked) begin
            shift1 <= {shift1[9:0], ddr_rise[0], ddr_fall[0]};
            shift2 <= {shift2[9:0], ddr_rise[1], ddr_fall[1]};
            shift3 <= {shift3[9:0], ddr_rise[2], ddr_fall[2]};
            shift4 <= {shift4[9:0], ddr_rise[3], ddr_fall[3]};
            shift5 <= {shift5[9:0], ddr_rise[4], ddr_fall[4]};
            if (pair_count == 3'd5) begin
                sample1 <= {shift1[9:0], ddr_rise[0], ddr_fall[0]};
                sample2 <= {shift2[9:0], ddr_rise[1], ddr_fall[1]};
                sample3 <= {shift3[9:0], ddr_rise[2], ddr_fall[2]};
                sample4 <= {shift4[9:0], ddr_rise[3], ddr_fall[3]};
                sample5 <= {shift5[9:0], ddr_rise[4], ddr_fall[4]};
                pair_count <= 0;
            end else begin
                pair_count <= pair_count + 1'b1;
            end
        end
    end

    wire [59:0] completed_words = {sample5, sample4, sample3, sample2, sample1};
    wire [59:0] live_shifts = {shift5, shift4, shift3, shift2, shift1};
    wire [11:0] raw_ddr = {adclk_rise, adclk_fall, ddr_rise, ddr_fall};
    wire [59:0] snapshot_words;
    wire snapshot_valid;
    // completed_words changes on the same edge as this strobe. The snapshot
    // intentionally carries the previous completed packet (one-frame latency).
    wire completed_packet_strobe = frame_locked && !adclk_rising_marker && pair_count == 3'd5;
    adc_word_snapshot word_snapshot (
        .source_clk(lclk_fabric), .source_valid(completed_packet_strobe),
        .source_words(completed_words), .destination_clk(sysclk),
        .destination_words(snapshot_words), .destination_valid(snapshot_valid)
    );
    (* ASYNC_REG = "TRUE" *) reg [1:0] frame_locked_sync = 0;
    always @(posedge sysclk) frame_locked_sync <= {frame_locked_sync[0], frame_locked};
    wire ret_window_valid_raw, ret_lclk_seen_raw, ret_adclk_seen_raw;
    wire ret_lclk_60m_raw, ret_adclk_10m_raw;
    adc_return_clock_monitor return_clock_monitor (
        .clk(monitor_clk_200m), .enable(run_enable), .lclk_async(lclk), .adclk_async(adclk),
        .window_valid(ret_window_valid_raw), .lclk_seen(ret_lclk_seen_raw),
        .adclk_seen(ret_adclk_seen_raw), .lclk_60m(ret_lclk_60m_raw),
        .adclk_10m(ret_adclk_10m_raw)
    );
    (* ASYNC_REG = "TRUE" *) reg [4:0] ret_status_sync1 = 0;
    (* ASYNC_REG = "TRUE" *) reg [4:0] ret_status_sync2 = 0;
    always @(posedge sysclk) begin
        ret_status_sync1 <= {ret_window_valid_raw, ret_lclk_60m_raw,
            ret_adclk_10m_raw, ret_lclk_seen_raw, ret_adclk_seen_raw};
        ret_status_sync2 <= ret_status_sync1;
    end
    wire ret_window_valid = ret_status_sync2[4];
    wire ret_lclk_60m = ret_status_sync2[3];
    wire ret_adclk_10m = ret_status_sync2[2];
    wire ret_lclk_seen = ret_status_sync2[1];
    wire ret_adclk_seen = ret_status_sync2[0];
    adc_capture_ila u_capture_ila (
        .clk(sysclk),
        .probe0(snapshot_words),
        .probe1(live_shifts),
        .probe2(raw_ddr),
        .probe3(control_status),
        .probe4({snapshot_valid, frame_locked_sync[1], ret_lclk_60m, ret_adclk_10m})
    );
    assign control_status = {
        ret_window_valid, ret_lclk_60m, ret_adclk_10m, ret_lclk_seen, ret_adclk_seen,
        dac_sclk, dac_ncs, dac_reset_n, dac_ready, dac_state,
        dac_frame_index, dac_enable,
        spi_sclk, spi_ncs, control_state, sequence_index,
        config_done, adc_reset_n, adc_clock_running, pll_locked, run_enable
    };
    assign led0 = (run_enable || dac_enable) ? blink_count[25] : blink_count[27];


    wire [63:0] scan_command;
    (* DONT_TOUCH="TRUE" *) wire [255:0] scan_result0, scan_result1;
    (* DONT_TOUCH="TRUE" *) wire [9215:0] bulk_scan_frames;
    (* DONT_TOUCH="TRUE" *) wire [479:0] bulk_tdc_words;
    (* DONT_TOUCH="TRUE" *) wire [255:0] bulk_header_raw;
    (* DONT_TOUCH="TRUE" *) wire [255:0] bulk_header;
    wire scan_interface_enable, scan_reset_low, scan_tm_drive, scan_se_drive, scan_si_drive;
    wire scan_si_read, scan_reset_read;
    wire scan_z, si_z, runtime_start, runtime_busy, runtime_done, runtime_sclk, runtime_sdata, runtime_ncs;
    wire [23:0] runtime_frame;
    runtime_dac_spi #(.HALF(13), .GAP(250)) runtime_dac (.clk(sysclk), .enable(dac_enable && dac_ready), .start(runtime_start), .data(runtime_frame),
        .busy(runtime_busy), .done(runtime_done), .sclk(runtime_sclk), .sdata(runtime_sdata), .ncs(runtime_ncs));
    scan_read_vio u_scan_vio (.clk(sysclk), .probe_out0(scan_command),
        .probe_in0(scan_result0), .probe_in1(scan_result1));
    (* DONT_TOUCH="TRUE" *) wire [3:0] bulk_page_control;
    (* DONT_TOUCH="TRUE" *) wire [255:0] bulk_page_descriptor;
    (* DONT_TOUCH="TRUE" *) wire [2559:0] bulk_page_data;
    (* DONT_TOUCH="TRUE" *) wire [255:0] bulk_page_lane0=bulk_page_data[0+:256];
    (* DONT_TOUCH="TRUE" *) wire [255:0] bulk_page_lane1=bulk_page_data[256+:256];
    (* DONT_TOUCH="TRUE" *) wire [255:0] bulk_page_lane2=bulk_page_data[512+:256];
    (* DONT_TOUCH="TRUE" *) wire [255:0] bulk_page_lane3=bulk_page_data[768+:256];
    (* DONT_TOUCH="TRUE" *) wire [255:0] bulk_page_lane4=bulk_page_data[1024+:256];
    (* DONT_TOUCH="TRUE" *) wire [255:0] bulk_page_lane5=bulk_page_data[1280+:256];
    (* DONT_TOUCH="TRUE" *) wire [255:0] bulk_page_lane6=bulk_page_data[1536+:256];
    (* DONT_TOUCH="TRUE" *) wire [255:0] bulk_page_lane7=bulk_page_data[1792+:256];
    (* DONT_TOUCH="TRUE" *) wire [255:0] bulk_page_lane8=bulk_page_data[2048+:256];
    (* DONT_TOUCH="TRUE" *) wire [255:0] bulk_page_lane9=bulk_page_data[2304+:256];
    bulk_vio_pager u_bulk_vio_pager (
        .clk(sysclk), .header_raw(bulk_header_raw),
        .scan_frames_raw(bulk_scan_frames), .tdc_words_raw(bulk_tdc_words),
        .page_control(bulk_page_control), .header(bulk_header),
        .page_descriptor(bulk_page_descriptor), .page_data(bulk_page_data)
    );
    // Bulk evidence is isolated from u_scan_vio. Two selector cycles expose
    // every pre frame before a pulse; two expose every post frame plus TDC.
    (* DONT_TOUCH="TRUE" *) bulk_result_vio u_bulk_vio (
        .clk(sysclk),
        .probe_out0(bulk_page_control),
        .probe_in0(bulk_header),
        .probe_in1(bulk_page_descriptor),
        .probe_in2(bulk_page_lane0), .probe_in3(bulk_page_lane1),
        .probe_in4(bulk_page_lane2), .probe_in5(bulk_page_lane3),
        .probe_in6(bulk_page_lane4), .probe_in7(bulk_page_lane5),
        .probe_in8(bulk_page_lane6), .probe_in9(bulk_page_lane7),
        .probe_in10(bulk_page_lane8), .probe_in11(bulk_page_lane9)
    );
    cell_control_engine #(
        .RESET_SYS(600000), .POST_CYCLES(32),
        .READY_FALLBACK_CYCLES(500),
        .WATCHDOG_CYCLES(64'd3000000000),
        .WB_ZERO(50000), .WB_ONE(200000), .WB_HIGH(125000), .WB_END(125000),
        .CLOCK_MISSING(500), .DAC_SETTLE_CYCLES(50000)
    ) scan_reader (
        .clk(sysclk), .feedback_clk(si5351_clk1_1v8), .ready_i(ready_i), .reset_i(scan_reset_read), .uart_rx(fpga_rx),
        .dac_ready(dac_enable && dac_ready),
        .adc_healthy(run_enable && config_done && ret_lclk_60m && ret_adclk_10m && frame_locked_sync[1]),
        .command(scan_command), .sample_valid(snapshot_valid), .samples(snapshot_words),
        .interface_enable(scan_interface_enable), .reset_low(scan_reset_low),
        .tm(scan_tm_drive), .se(scan_se_drive), .si(scan_si_drive),
        .scan_tristate(scan_z), .si_tristate(si_z), .dac_start(runtime_start), .dac_frame(runtime_frame), .dac_done(runtime_done),
        .result0(scan_result0), .result1(scan_result1),
        .bulk_scan_frames(bulk_scan_frames), .bulk_tdc_words(bulk_tdc_words),
        .bulk_header(bulk_header_raw));
    IOBUF scan_si_buffer (.I(scan_si_drive), .T(si_z), .O(scan_si_read), .IO(scan_si));
    // Open-drain reset: never actively drive high against FT232H or SW1.
    IOBUF scan_reset_buffer (.I(1'b0), .T(!(scan_interface_enable && scan_reset_low)),
        .O(scan_reset_read), .IO(reset_b));

    // Existing ADC/DAC pin routing and voltage standards are preserved.
    OBUF fpga_tx_output      (.I(1'b1), .O(fpga_tx));
    OBUF scan_cc_output      (.I(1'b0), .O(scan_cc));
    OBUFT scan_se_output     (.I(scan_se_drive), .T(scan_z), .O(scan_se));
    // TM must stay low in WB mode on this PCB. U17's TXS0108E pull-ups
    // otherwise raise a floating TM into scan mode. Caravel GPIO36 remains
    // an input; the engine drives tm=0 throughout WB. Electrical constraints
    // (including LVCMOS18, drive and slew) are unchanged.
    OBUFT scan_tm_output     (.I(scan_tm_drive), .T(!scan_interface_enable), .O(scan_tm));
    OBUF aq1_sck_output      (.I(dac_enable ? (runtime_busy ? runtime_sclk : dac_sclk) : 1'b0), .O(aq1_sck));
    OBUF aq1_mosi_output     (.I(dac_enable ? (runtime_busy ? runtime_sdata : dac_sdata) : 1'b0), .O(aq1_mosi));
    OBUF fpga_sck_output     (.I(spi_sclk), .O(fpga_sck));
    OBUF fpga_sdata_output   (.I(spi_sdata), .O(fpga_sdata));
    OBUF adc_pd_output       (.I(1'b0), .O(adc_pd));
    OBUF fpga_loe_output     (.I(scan_interface_enable), .O(fpga_loe));
    OBUF aq1_ncs_output      (.I(dac_enable ? (runtime_busy ? runtime_ncs : dac_ncs) : 1'b1), .O(aq1_ncs));
    OBUF aq1_nldac_output    (.I(dac_enable ? dac_nldac : 1'b1), .O(aq1_nldac));
    OBUF fpga_ncs_output     (.I(spi_ncs), .O(fpga_ncs));
    OBUF fpga_nadcrst_output (.I(adc_reset_n), .O(fpga_nadcrst));
    OBUF aq1_nrst_output     (.I(dac_enable ? dac_reset_n : 1'b0), .O(aq1_nrst));

    // Keep the remaining input-only PCB signals present for the I/O audit.
    (* ASYNC_REG = "TRUE", DONT_TOUCH = "TRUE" *) reg [6:0] input_sync_1 = 0;
    (* ASYNC_REG = "TRUE", DONT_TOUCH = "TRUE" *) reg [6:0] input_sync_2 = 0;
    always @(posedge sysclk) begin
        input_sync_1 <= {si5351_clk1_1v8, scan_si_read, scan_reset_read, ready_i, fpga_rx, adclk, lclk};
        input_sync_2 <= input_sync_1;
    end
endmodule

// Fixed DAC81416 scan-startup profile mapped to the v1.2F PCB.
// The DAC samples SDI on each falling SCLK edge. At 50 MHz and a 13-cycle
// half-period, SCLK is 1.923 MHz and every inter-frame gap is 5 us.
module dac81416_pcb_scan_control #(
    parameter integer RESET_LOW_CYCLES = 50,
    parameter integer RESET_RELEASE_CYCLES = 50000,
    parameter integer REFERENCE_SETTLE_CYCLES = 50000,
    parameter integer SUPPLY_SETTLE_CYCLES = 500000,
    parameter integer SPI_HALF_CYCLES = 13,
    parameter integer INTERFRAME_CYCLES = 250
) (
    input  wire clk,
    input  wire enable,
    output reg  sclk = 1'b0,
    output reg  sdata = 1'b0,
    output reg  ncs = 1'b1,
    output wire nldac,
    output reg  reset_n = 1'b0,
    output reg  ready = 1'b0,
    output reg [4:0] frame_index = 5'd0,
    output wire [2:0] state_debug
);
    localparam [2:0] ST_RESET_LOW = 3'd0;
    localparam [2:0] ST_RESET_WAIT = 3'd1;
    localparam [2:0] ST_FRAME_LOAD = 3'd2;
    localparam [2:0] ST_SPI_RISE   = 3'd3;
    localparam [2:0] ST_SPI_FALL   = 3'd4;
    localparam [2:0] ST_FRAME_END  = 3'd5;
    localparam [2:0] ST_GAP        = 3'd6;
    localparam [2:0] ST_READY      = 3'd7;

    localparam [15:0] CODE_0V5 = 16'h199A;
    localparam [15:0] CODE_2V5 = 16'h8000;

    reg [2:0] state = ST_RESET_LOW;
    reg [31:0] wait_count = 32'd0;
    reg [4:0] bit_index = 5'd23;
    reg [23:0] frame = 24'd0;
    assign nldac = 1'b1; // asynchronous update mode
    assign state_debug = state;

    function [23:0] frame_for_index;
        input [4:0] index;
        begin
            case (index)
                5'd0: frame_for_index = 24'h09FFFF;       // all channels off
                5'd1: frame_for_index = 24'h0A0000;       // all channels 0..5 V
                5'd2: frame_for_index = 24'h0B0000;
                5'd3: frame_for_index = 24'h0C0000;
                5'd4: frame_for_index = 24'h0D0000;
                5'd5: frame_for_index = 24'h030004;       // active, CRC/stream off
                5'd6: frame_for_index = 24'h040000;       // internal 2.5 V ref on
                5'd7: frame_for_index = 24'h1F5C29;       // DAC15 VCCD2 = 1.8 V
                5'd8: frame_for_index = 24'h1575C3;       // DAC5 VDDA2/reset = 2.3 V
                5'd9: frame_for_index = 24'h1E999A;       // DAC14 VDDA1 = 3.0 V via NT22
                5'd10: frame_for_index = 24'h093FDF;       // supply channels 15,14,5 first
                // Wait 10 ms before applying user analog-pad voltages.
                5'd11: frame_for_index = {8'h10, CODE_0V5}; // DAC0 VCC_READ
                5'd12: frame_for_index = {8'h11, CODE_2V5}; // DAC1 WL_READ
                5'd13: frame_for_index = {8'h12, CODE_0V5}; // DAC2 VCC_SET (not DAC6)
                5'd14: frame_for_index = {8'h13, CODE_2V5}; // DAC3 WL_SET
                5'd15: frame_for_index = 24'h140000;       // DAC4 WL_RESET off in scan
                5'd16: frame_for_index = 24'h192E14;       // DAC9 IREF = 0.9 V
                5'd17: frame_for_index = 24'h1A2E14;       // DAC10 VCOMP = 0.9 V on 5 V span
                5'd18: frame_for_index = 24'h1B1EB8;       // DAC11 BIAS_COMP2 = 0.6 V
                5'd19: frame_for_index = 24'h1C51EB;       // DAC12 VBIAS = 1.6 V
                5'd20: frame_for_index = 24'h1D3333;       // DAC13 DC_BIAS = 1.0 V
                5'd21: frame_for_index = 24'h0901D0;       // off: 4,6,7,8
                default: frame_for_index = 24'h09FFFF;
            endcase
        end
    endfunction

    always @(posedge clk) begin
        if (!enable) begin
            state       <= ST_RESET_LOW;
            wait_count  <= 32'd0;
            bit_index   <= 5'd23;
            frame_index <= 5'd0;
            frame       <= 24'd0;
            sclk        <= 1'b0;
            sdata       <= 1'b0;
            ncs         <= 1'b1;
            reset_n     <= 1'b0;
            ready       <= 1'b0;
        end else begin
            case (state)
                ST_RESET_LOW: begin
                    sclk    <= 1'b0;
                    sdata   <= 1'b0;
                    ncs     <= 1'b1;
                    reset_n <= 1'b0;
                    ready   <= 1'b0;
                    if (wait_count + 1 >= RESET_LOW_CYCLES) begin
                        wait_count <= 32'd0;
                        reset_n <= 1'b1;
                        state <= ST_RESET_WAIT;
                    end else begin
                        wait_count <= wait_count + 1'b1;
                    end
                end

                ST_RESET_WAIT: begin
                    reset_n <= 1'b1;
                    if (wait_count + 1 >= RESET_RELEASE_CYCLES) begin
                        wait_count <= 32'd0;
                        state <= ST_FRAME_LOAD;
                    end else begin
                        wait_count <= wait_count + 1'b1;
                    end
                end

                ST_FRAME_LOAD: begin
                    frame     <= frame_for_index(frame_index);
                    bit_index <= 5'd23;
                    wait_count <= 32'd0;
                    sclk      <= 1'b0;
                    sdata     <= 1'b0;
                    ncs       <= 1'b0;
                    state     <= ST_SPI_RISE;
                end

                ST_SPI_RISE: begin
                    if (wait_count + 1 >= SPI_HALF_CYCLES) begin
                        wait_count <= 32'd0;
                        sclk <= 1'b1;
                        sdata <= frame[bit_index];
                        state <= ST_SPI_FALL;
                    end else begin
                        wait_count <= wait_count + 1'b1;
                    end
                end

                ST_SPI_FALL: begin
                    if (wait_count + 1 >= SPI_HALF_CYCLES) begin
                        wait_count <= 32'd0;
                        sclk <= 1'b0;
                        if (bit_index == 0) begin
                            state <= ST_FRAME_END;
                        end else begin
                            bit_index <= bit_index - 1'b1;
                            state <= ST_SPI_RISE;
                        end
                    end else begin
                        wait_count <= wait_count + 1'b1;
                    end
                end

                ST_FRAME_END: begin
                    if (wait_count + 1 >= SPI_HALF_CYCLES) begin
                        wait_count <= 32'd0;
                        ncs <= 1'b1;
                        sdata <= 1'b0;
                        state <= ST_GAP;
                    end else begin
                        wait_count <= wait_count + 1'b1;
                    end
                end

                ST_GAP: begin
                    // REF is not externally driven on this PCB.  Give the
                    // internal 2.5 V reference a conservative 1 ms after the
                    // GENCONFIG write before loading or enabling any output.
                    if (wait_count + 1 >=
                        ((frame_index == 5'd6) ? REFERENCE_SETTLE_CYCLES :
                         (frame_index == 5'd10) ? SUPPLY_SETTLE_CYCLES :
                                                INTERFRAME_CYCLES)) begin
                        wait_count <= 32'd0;
                        if (frame_index == 5'd21) begin
                            ready <= 1'b1;
                            state <= ST_READY;
                        end else begin
                            frame_index <= frame_index + 1'b1;
                            state <= ST_FRAME_LOAD;
                        end
                    end else begin
                        wait_count <= wait_count + 1'b1;
                    end
                end

                default: begin
                    sclk    <= 1'b0;
                    sdata   <= 1'b0;
                    ncs     <= 1'b1;
                    reset_n <= 1'b1;
                    ready   <= 1'b1;
                    state   <= ST_READY;
                end
            endcase
        end
    end
endmodule

// Fixed ADS5282 initialization sequencer.  Serial frames are A7..A0 followed
// by D15..D0, MSB first.  SDATA changes only while SCLK is low and the ADC
// latches each bit on a rising edge. At 50 MHz and SPI_HALF_CYCLES=13 the
// SCLK frequency is 1.923 MHz, safely below the requested 5 MHz ceiling.
module ads5282_control #(
    parameter integer RESET_HIGH_CYCLES = 50,
    parameter integer RESET_LOW_CYCLES = 50,
    parameter integer RESET_RELEASE_CYCLES = 50000,
    parameter integer PRE_CLOCK_WAIT_CYCLES = 500000,
    parameter integer POST_CLOCK_WAIT_CYCLES = 5000,
    parameter integer SPI_HALF_CYCLES = 13
) (
    input wire clk,
    input wire run_enable,
    output reg adc_clock_enable = 0,
    output reg adc_reset_n = 0,
    output reg spi_sclk = 0,
    output reg spi_sdata = 0,
    output reg spi_ncs = 1,
    output reg config_done = 0,
    output reg [2:0] sequence_index = 0,
    output wire [3:0] state_debug
);
    localparam [3:0] ST_SAFE=0, ST_RESET_HIGH=1, ST_RESET_LOW=2,
                     ST_RESET_WAIT=3, ST_LOAD=4, ST_SETUP=5,
                     ST_HIGH=6, ST_FINISH=7, ST_GAP=8,
                     ST_PRE_CLOCK_WAIT=9, ST_POST_CLOCK_WAIT=10,
                     ST_DONE=11;
    reg [3:0] state = ST_SAFE;
    reg [31:0] delay_count = 0;
    reg [15:0] phase_count = 0;
    reg [4:0] bit_index = 23;
    reg [23:0] shift_word = 0;
    assign state_debug = state;

    function [23:0] command_word;
        input [2:0] index;
        begin
            case (index)
                // D14 enables internal termination; code 011 selects nominal
                // 94 ohm for data, frame clock and bit clock. Datasheet p20.
                3'd0: command_word = 24'h12_4333;
                3'd1: command_word = 24'h03_0002;
                3'd2: command_word = 24'h01_0010;
                3'd3: command_word = 24'hC7_8001;
                3'd4: command_word = 24'hDE_01C0;
                3'd5: command_word = 24'hE3_0060; // low-rate PLL for 10 MSPS
                3'd6: command_word = 24'h42_8001;
                3'd7: command_word = 24'h25_0000; // normal ADC data, test patterns off
                default: command_word = 24'h00_0000;
            endcase
        end
    endfunction

    always @(posedge clk) begin
        if (!run_enable) begin
            state <= ST_SAFE;
            delay_count <= 0;
            phase_count <= 0;
            bit_index <= 23;
            shift_word <= 0;
            sequence_index <= 0;
            adc_clock_enable <= 0;
            adc_reset_n <= 0;
            spi_sclk <= 0;
            spi_sdata <= 0;
            spi_ncs <= 1;
            config_done <= 0;
        end else begin
            case (state)
                ST_SAFE: begin
                    adc_clock_enable <= 0;
                    // Begin the required explicit high-low-high reset pulse.
                    adc_reset_n <= 1;
                    spi_sclk <= 0;
                    spi_sdata <= 0;
                    spi_ncs <= 1;
                    config_done <= 0;
                    delay_count <= 0;
                    state <= ST_RESET_HIGH;
                end
                ST_RESET_HIGH: begin
                    if (delay_count == RESET_HIGH_CYCLES-1) begin
                        delay_count <= 0;
                        adc_reset_n <= 0;
                        state <= ST_RESET_LOW;
                    end else delay_count <= delay_count + 1'b1;
                end
                ST_RESET_LOW: begin
                    if (delay_count == RESET_LOW_CYCLES-1) begin
                        delay_count <= 0;
                        adc_reset_n <= 1;
                        state <= ST_RESET_WAIT;
                    end else delay_count <= delay_count + 1'b1;
                end
                ST_RESET_WAIT: begin
                    if (delay_count == RESET_RELEASE_CYCLES-1) begin
                        delay_count <= 0;
                        sequence_index <= 0;
                        state <= ST_LOAD;
                    end else delay_count <= delay_count + 1'b1;
                end
                ST_LOAD: begin
                    shift_word <= command_word(sequence_index);
                    bit_index <= 23;
                    spi_ncs <= 0;
                    spi_sclk <= 0;
                    spi_sdata <= command_word(sequence_index) >> 23;
                    phase_count <= 0;
                    state <= ST_SETUP;
                end
                ST_SETUP: begin
                    if (phase_count == SPI_HALF_CYCLES-1) begin
                        phase_count <= 0;
                        spi_sclk <= 1;
                        state <= ST_HIGH;
                    end else phase_count <= phase_count + 1'b1;
                end
                ST_HIGH: begin
                    if (phase_count == SPI_HALF_CYCLES-1) begin
                        phase_count <= 0;
                        spi_sclk <= 0;
                        if (bit_index == 0) begin
                            state <= ST_FINISH;
                        end else begin
                            bit_index <= bit_index - 1'b1;
                            spi_sdata <= shift_word[bit_index-1'b1];
                            state <= ST_SETUP;
                        end
                    end else phase_count <= phase_count + 1'b1;
                end
                ST_FINISH: begin
                    if (phase_count == SPI_HALF_CYCLES-1) begin
                        phase_count <= 0;
                        spi_ncs <= 1;
                        spi_sdata <= 0;
                        state <= ST_GAP;
                    end else phase_count <= phase_count + 1'b1;
                end
                ST_GAP: begin
                    if (phase_count == SPI_HALF_CYCLES-1) begin
                        phase_count <= 0;
                        if (sequence_index == 3'd7) begin
                            delay_count <= 0;
                            state <= ST_PRE_CLOCK_WAIT;
                        end else begin
                            sequence_index <= sequence_index + 1'b1;
                            state <= ST_LOAD;
                        end
                    end else phase_count <= phase_count + 1'b1;
                end
                ST_PRE_CLOCK_WAIT: begin
                    // SPI is complete while the conversion clock remains off.
                    // Wait at least 10 ms before applying the sample clock.
                    if (delay_count == PRE_CLOCK_WAIT_CYCLES-1) begin
                        delay_count <= 0;
                        adc_clock_enable <= 1;
                        state <= ST_POST_CLOCK_WAIT;
                    end else delay_count <= delay_count + 1'b1;
                end
                ST_POST_CLOCK_WAIT: begin
                    // Allow at least 100 us of sample clock before ready.
                    if (delay_count == POST_CLOCK_WAIT_CYCLES-1) begin
                        delay_count <= 0;
                        config_done <= 1;
                        state <= ST_DONE;
                    end else delay_count <= delay_count + 1'b1;
                end
                ST_DONE: begin
                    adc_clock_enable <= 1;
                    adc_reset_n <= 1;
                    spi_sclk <= 0;
                    spi_sdata <= 0;
                    spi_ncs <= 1;
                    config_done <= 1;
                end
                default: state <= ST_SAFE;
            endcase
        end
    end
endmodule

