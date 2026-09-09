`timescale 1ns / 1ps

module tb_dac81416_runtime_spi;
    reg clk = 1'b0;
    reg update = 1'b0;
    reg wb_profile = 1'b0;
    reg [15:0] vcc_set_code = 16'h1234;
    reg [15:0] vcc_wl_set_code = 16'hABCD;
    wire sclk;
    wire sdi;
    wire cs_n;
    wire ldac_n;
    wire initialized;
    wire ready;

    reg [23:0] captured [0:31];
    reg [23:0] shift = 24'd0;
    integer bit_count = 0;
    integer frame_count = 0;
    integer failures = 0;

    dac81416_runtime_spi #(
        .BOOT_INITIALIZE(1'b1),
        .VDDIO_STARTUP_DELAY_CYCLES(32'd4)
    ) dut (
        .clk_i(clk),
        .update_i(update),
        .wb_profile_i(wb_profile),
        .vcc_set_code_i(vcc_set_code),
        .vcc_wl_set_code_i(vcc_wl_set_code),
        .sclk_o(sclk),
        .sdi_o(sdi),
        .cs_n_o(cs_n),
        .ldac_n_o(ldac_n),
        .initialized_o(initialized),
        .ready_o(ready)
    );

    always #5 clk = ~clk;

    always @(negedge sclk) begin
        if (!cs_n) begin
            shift = {shift[22:0], sdi};
            bit_count = bit_count + 1;
            if (bit_count == 24) begin
                captured[frame_count] = shift;
                frame_count = frame_count + 1;
                bit_count = 0;
                shift = 24'd0;
            end
        end
    end

    task expect_frame;
        input integer index;
        input [23:0] expected;
        begin
            if (captured[index] !== expected) begin
                $display("FAIL frame %0d expected=%06h actual=%06h", index, expected, captured[index]);
                failures = failures + 1;
            end
        end
    endtask

    task start_update;
        input profile;
        begin
            @(negedge clk);
            wb_profile = profile;
            update = 1'b1;
            @(negedge clk);
            update = 1'b0;
            wait (!ready);
            wait (ready);
            repeat (2) @(posedge clk);
        end
    endtask

    initial begin
        wait (ready);
        repeat (2) @(posedge clk);
        if (frame_count != 25) begin
            $display("FAIL boot frame count expected=25 actual=%0d", frame_count);
            failures = failures + 1;
        end
        expect_frame(0, 24'h09FFFF);
        expect_frame(2, 24'h0B0101);
        expect_frame(7, 24'h17CCCC);
        expect_frame(8, 24'h09FF7F);
        expect_frame(9, 24'h10199A);
        expect_frame(10, 24'h118000);
        expect_frame(11, 24'h12199A);
        expect_frame(12, 24'h138000);
        expect_frame(14, 24'h1575C3);
        expect_frame(15, 24'h160CCD);
        expect_frame(17, 24'h19199A);
        expect_frame(18, 24'h1A170A);
        expect_frame(19, 24'h1B1EB8);
        expect_frame(20, 24'h1C51EB);
        expect_frame(21, 24'h1D3333);
        expect_frame(24, 24'h094110);

        frame_count = 0;
        start_update(1'b0);
        if (frame_count != 13) begin
            $display("FAIL scan frame count expected=13 actual=%0d", frame_count);
            failures = failures + 1;
        end
        expect_frame(0, 24'h0A0000);
        expect_frame(1, 24'h0B0101);
        expect_frame(2, 24'h0C0101);
        expect_frame(3, 24'h0D0000);
        expect_frame(4, 24'h122468);
        expect_frame(5, 24'h13ABCD);
        expect_frame(6, 24'h161234);
        expect_frame(7, 24'h19199A);
        expect_frame(8, 24'h1A170A);
        expect_frame(9, 24'h1B1EB8);
        expect_frame(10, 24'h1C51EB);
        expect_frame(11, 24'h1D3333);
        expect_frame(12, 24'h094110);

        frame_count = 0;
        start_update(1'b1);
        if (frame_count != 6) begin
            $display("FAIL WB frame count expected=6 actual=%0d", frame_count);
            failures = failures + 1;
        end
        expect_frame(0, 24'h19199A);
        expect_frame(1, 24'h1A170A);
        expect_frame(2, 24'h1B1EB8);
        expect_frame(3, 24'h1C51EB);
        expect_frame(4, 24'h1D3333);
        expect_frame(5, 24'h094158);

        if (failures == 0) begin
            $display("PASS: scan mirrors Vcc_set on DAC2/DAC6 and enables both after loading values");
            $finish;
        end
        $fatal(1, "runtime DAC sequence failed with %0d errors", failures);
    end
endmodule
