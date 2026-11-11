`timescale 1ns/1ps

// Resource-bounded, read-only transport for the immutable bulk evidence.
// page_control[2:0] selects one of four pages; bit 3 is the fail-closed
// interlock and must remain zero. Selector bits never reach the scheduler or
// programming datapath. Invalid selectors return zero payload and fault only
// the VIO-visible header.
module bulk_vio_pager (
 input  wire          clk,
 input  wire [255:0]  header_raw,
 input  wire [9215:0] scan_frames_raw,
 input  wire [479:0]  tdc_words_raw,
 input  wire [3:0]    page_control,
 output wire [255:0]  header,
 output reg  [255:0]  page_descriptor,
 output reg  [2559:0] page_data
);
 // VIO is clocked by the same fabric clock. Two explicit stages make the
 // selector publication boundary unambiguous; page echo lets the host wait
 // for the selected payload rather than relying on JTAG timing.
 (* ASYNC_REG="TRUE" *) reg [3:0] page_control_meta=0;
 (* ASYNC_REG="TRUE" *) reg [3:0] page_control_sync=0;
 always @(posedge clk) begin
  page_control_meta<=page_control;
  page_control_sync<=page_control_meta;
 end
 wire [2:0] page_select=page_control_sync[2:0];
 wire keepalive=page_control_sync[3];
 wire selector_valid=page_select<=3'd3;
 wire page_valid=selector_valid && !keepalive;
 wire tdc_complete=header_raw[45:31]==15'h7FFF;

 assign header={header_raw[255:212],
                header_raw[211]|keepalive|!selector_valid,
                header_raw[210:0]};

 always @* begin
  page_descriptor=0;
  page_data=0;
  if(page_valid) begin
   page_descriptor[255:224]=32'h42504732; // "BPG2"
   page_descriptor[223:216]=8'd2;
   page_descriptor[215:208]={5'd0,page_select};
   page_descriptor[207:176]=header_raw[207:176]; // sequence32
   page_descriptor[175:160]=header_raw[175:160]; // job16
   page_descriptor[159:152]=header_raw[223:216]; // header protocol version
   page_descriptor[151]=header_raw[0];           // header valid snapshot
   page_descriptor[150]=1'b1;                    // selector/page valid
   page_descriptor[149:140]=10'h3FF;
   page_descriptor[139:135]=header_raw[10:6];    // row
   page_descriptor[134:130]=header_raw[5:1];     // column
   page_descriptor[129:121]=header_raw[63:55];   // pre valid
   page_descriptor[120:112]=header_raw[54:46];   // post valid
   page_descriptor[111:97]=header_raw[45:31];    // TDC valid
   page_descriptor[96]=header_raw[208];           // pre done
   page_descriptor[95]=header_raw[209];           // WB done
   page_descriptor[94]=header_raw[210];           // post done
   page_descriptor[93]=header[211];               // visible any fault
   page_descriptor[92]=header_raw[213];            // job mismatch
   page_descriptor[91]=header_raw[215];            // WB protocol error
   page_descriptor[90]=header_raw[214];            // WB exact echo
   page_descriptor[89:81]=header_raw[30:22];      // pre clip
   page_descriptor[80:72]=header_raw[21:13];      // post clip
   page_descriptor[71]=header_raw[12];             // pre physical order
   page_descriptor[70]=header_raw[11];             // post physical order
   case(page_select)
    // Pre half-lanes 0..9.
    3'd0: page_data=scan_frames_raw[0+:2560];
    // Pre half-lanes 10..17, followed by the packed TDC low/high halves.
    // Before WB, tdc_valid is zero and the host ignores these two lanes. The
    // same selected page is refreshed after WB for the immediate full-loop
    // TDC gate without another selector commit.
    3'd1: begin
     page_data[0+:2048]=scan_frames_raw[2560+:2048];
     page_data[2048+:256]=tdc_complete ? tdc_words_raw[255:0] : 256'd0;
     page_data[2304+:256]=tdc_complete ? {32'd0,tdc_words_raw[479:256]} : 256'd0;
    end
    // Post half-lanes 0..9.
    3'd2: page_data=scan_frames_raw[4608+:2560];
    // Post half-lanes 10..17, then packed TDC low/high halves.
    3'd3: begin
     page_data[0+:2048]=scan_frames_raw[7168+:2048];
     page_data[2048+:256]=tdc_complete ? tdc_words_raw[255:0] : 256'd0;
     page_data[2304+:256]=tdc_complete ? {32'd0,tdc_words_raw[479:256]} : 256'd0;
    end
    default: page_data=0;
   endcase
  end
 end
endmodule
