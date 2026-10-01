`timescale 1ns/1ps

module tb;
  initial begin
    // Flaky assertion: fails first, passes last -> final status pass,
    // but the earlier failure is counted.
    $display("ASSERT FAIL flaky first sample wrong");
    #2;
    $display("ASSERT PASS flaky");

    // Stable assertion: final result is FAIL.
    $display("ASSERT FAIL stable mismatch one");
    #2;
    $display("ASSERT FAIL stable mismatch two");

    #5 $finish;
  end
endmodule
