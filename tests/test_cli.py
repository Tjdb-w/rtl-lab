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

# 仅 seed 1 断言失败；所有种子打印自身 GOTSEED。
TB_SEEDED = """
`timescale 1ns/1ps
module tb;
  integer seed;
  initial begin
    if ($value$plusargs("SEED=%d", seed)) $display("GOTSEED %0d", seed);
    if (seed == 1) $display("ASSERT FAIL ok broke");
    else $display("ASSERT PASS ok");
    $display("COVER c");
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


# ---------------- regress 子命令 ----------------

def _regress_common(files, seeds, tb_text=None):
    tmp_path, src, tb = files
    if tb_text is not None:
        tb.write_text(tb_text)
    return [
        "regress", str(src), "--tb", str(tb), "--top", "tb",
        "--duration", "100ns", "--seeds", seeds,
        "--workdir", str(tmp_path / "w"),
        "--report", str(tmp_path / "r.json"),
    ]


def test_cli_regress_passed_exit_zero(files):
    argv = _regress_common(files, "0,2,4")
    assert main(argv) == 0
    data = json.load(open(files[0] / "r.json"))
    assert data["status"] == "passed"
    assert data["schema_version"] == 2
    assert data["seeds"] == [0, 2, 4]
    assert len(data["runs"]) == 3


def test_cli_regress_assertion_exit_6_and_runs_all_seeds(files):
    argv = _regress_common(files, "0,1,2", TB_SEEDED)
    assert main(argv) == 6
    data = json.load(open(files[0] / "r.json"))
    assert data["status"] == "assertion_failed"
    assert data["failed_seeds"] == [1]
    assert [r["seed"] for r in data["runs"]] == [0, 1, 2]


@pytest.mark.parametrize("bad", [
    "", "1,,2", "1,-1", "1.5", "x", "1,1", " 3 ,3", "+",
])
def test_cli_regress_bad_seeds_exit_2_no_report(files, bad):
    tmp_path = files[0]
    argv = _regress_common(files, bad)
    assert main(argv) == 2
    assert not (tmp_path / "r.json").exists()


def test_cli_regress_missing_seeds_flag_exit_2(files):
    tmp_path, src, tb = files
    argv = ["regress", str(src), "--tb", str(tb), "--top", "tb",
            "--duration", "100ns", "--workdir", str(tmp_path / "w")]
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2


def test_cli_regress_compile_failure_exit_3(files):
    tmp_path, src, _tb = files
    bad_tb = tmp_path / "bad.v"
    bad_tb.write_text("module tb; nope_mod x(); endmodule\n")
    argv = ["regress", str(src), "--tb", str(bad_tb), "--top", "tb",
            "--duration", "100ns", "--seeds", "1,2",
            "--workdir", str(tmp_path / "w"),
            "--report", str(tmp_path / "r.json")]
    assert main(argv) == 3
    data = json.load(open(tmp_path / "r.json"))
    assert data["status"] == "compile_failed"
    assert data["runs"] == []


def test_cli_regress_streams_split_and_summary(files, capsys):
    argv = _regress_common(files, "0,2", TB_SEEDED)
    assert main(argv) == 0
    captured = capsys.readouterr()
    # 仿真 stdout 回放到 stdout（GOTSEED 来自 $display）。
    assert "GOTSEED 0" in captured.out and "GOTSEED 2" in captured.out
    # 汇总行写 stderr，含种子数与覆盖率数量。
    assert "2 个种子" in captured.err
    assert "1 个覆盖率点" in captured.err
