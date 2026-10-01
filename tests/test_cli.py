"""CLI 入口测试：退出码与报告文件行为。"""

import json
import shutil

import pytest

from rtl_lab.cli import main

pytestmark = pytest.mark.skipif(
    shutil.which("iverilog") is None or shutil.which("vvp") is None,
    reason="需要安装 Icarus Verilog",
)

DESIGN = "module d; endmodule\n"
TB_PASS = """
`timescale 1ns/1ps
module tb;
  initial begin
    $display("ASSERT PASS ok");
    $display("COVER c");
    #5 $finish;
  end
endmodule
"""
TB_FAIL = """
`timescale 1ns/1ps
module tb;
  initial begin
    $display("ASSERT FAIL ok broke");
    #5 $finish;
  end
endmodule
"""


@pytest.fixture
def files(tmp_path):
    src = tmp_path / "d.v"
    src.write_text(DESIGN)
    tb = tmp_path / "tb.v"
    tb.write_text(TB_PASS)
    return tmp_path, src, tb


def _common(files, tb_text=None):
    tmp_path, src, tb = files
    if tb_text is not None:
        tb.write_text(tb_text)
    return [
        str(src), "--tb", str(tb), "--top", "tb",
        "--duration", "100ns", "--workdir", str(tmp_path / "w"),
        "--report", str(tmp_path / "r.json"),
    ]


def test_cli_passed_exit_zero(files):
    argv = ["run"] + _common(files)
    assert main(argv) == 0
    assert json.load(open(files[0] / "r.json"))["status"] == "passed"


def test_cli_assertion_exit_6(files):
    argv = ["run"] + _common(files, TB_FAIL)
    assert main(argv) == 6
    assert json.load(open(files[0] / "r.json"))["status"] == "assertion_failed"


def test_cli_input_error_exit_2_no_report(files):
    tmp_path = files[0]
    argv = ["run", str(tmp_path / "missing.v"), "--tb", str(files[2]),
            "--top", "tb", "--duration", "100ns",
            "--workdir", str(tmp_path / "w"),
            "--report", str(tmp_path / "r.json")]
    assert main(argv) == 2
    assert not (tmp_path / "r.json").exists()


def test_cli_bad_duration_exit_2(files):
    argv = ["run"] + _common(files)
    argv[argv.index("100ns")] = "0ns"
    assert main(argv) == 2


def test_cli_compile_failure_exit_3(files):
    tmp_path, src, _tb = files
    bad_tb = tmp_path / "bad.v"
    bad_tb.write_text("module tb; nope_mod x(); endmodule\n")
    argv = ["run", str(src), "--tb", str(bad_tb), "--top", "tb",
            "--duration", "100ns", "--workdir", str(tmp_path / "w"),
            "--report", str(tmp_path / "r.json")]
    assert main(argv) == 3
    assert json.load(open(tmp_path / "r.json"))["status"] == "compile_failed"
