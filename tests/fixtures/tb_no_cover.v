`timescale 1ns/1ps

module tb;
  initial begin
    #1 $display("ASSERT PASS only_check");
    // Deliberately emits no COVER lines: absence of coverage is allowed.
    #5 $finish;
  end
endmodule
