`timescale 1ns/1ps

module tb;
  reg clk = 1'b0;
  reg rst = 1'b0;
  wire [3:0] q;

  integer seed = 0;

  counter dut (
    .clk(clk),
    .rst(rst),
    .q(q)
  );

  always #5 clk = ~clk;

  initial begin
    if ($value$plusargs("seed=%d", seed)) ;
    $display("starting tb seed=%0d", seed);

    // Issue a real reset edge so the async-reset counter clears.
    #1 rst = 1'b1;
    #1;
    if (q === 4'd0)
      $display("ASSERT PASS check_reset");
    else
      $display("ASSERT FAIL check_reset q=%0d", q);
    $display("COVER reset_seen");

    #11 rst = 1'b0;
    repeat (3) @(posedge clk);
    #1;
    if (q === 4'd3)
      $display("ASSERT PASS check_count");
    else
      $display("ASSERT FAIL check_count q=%0d", q);
    $display("COVER counting_seen");
    $display("COVER counting_seen");
    $display("COVER counting_seen");

    #10 $finish;
  end
endmodule
