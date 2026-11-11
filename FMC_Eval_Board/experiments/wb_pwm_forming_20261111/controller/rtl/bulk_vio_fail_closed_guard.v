`timescale 1ns/1ps

// Retention-only VIO keepalive guard.  The VIO output never controls the
// scan/WB datapath.  Its required zero value is observable in two independent
// places; a nonzero value makes the metadata fail closed before programming.
module bulk_vio_fail_closed_guard (
 input wire [255:0] header_raw,
 input wire [479:0] tdc_words_raw,
 input wire keepalive,
 output wire [255:0] header,
 output wire [255:0] tdc_0_7,
 output wire [255:0] tdc_8_14
);
 assign header={header_raw[255:212],header_raw[211]|keepalive,
                header_raw[210:0]};
 assign tdc_0_7=tdc_words_raw[255:0];
 assign tdc_8_14={31'd0,keepalive,tdc_words_raw[479:256]};
endmodule
