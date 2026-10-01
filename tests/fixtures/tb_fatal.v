`timescale 1ns/1ps

module tb_fatal;
  initial begin
    #1 $display("ASSERT PASS before_crash");
    #2 $fatal(1, "simulation blew up");
  end
endmodule
