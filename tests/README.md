# RTL Lab tests

End-to-end tests drive the real ``iverilog``/``vvp`` toolchain; they are
skipped automatically when Icarus Verilog is not installed.

Run with:

    python -m unittest discover -s tests -v
