// A second design unit used to verify multi-source compile ordering.
module extra (
  input  wire a,
  input  wire b,
  output wire y
);
  assign y = a & b;
endmodule
