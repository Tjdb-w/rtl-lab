"""--incdir / --define 编译参数：校验、命令构造与各子命令行为。"""

import json
import shutil

import pytest

from rtl_lab.cli import main
from rtl_lab.errors import InputError
from rtl_lab.runner import (
    RegressConfig,
    RunConfig,
    parse_compile_flags,
    run,
)
from rtl_lab.tools import compile_sources
from rtl_lab.verification import VerifyConfig

needs_icarus = pytest.mark.skipif(
    shutil.which("iverilog") is None or shutil.which("vvp") is None,
    reason="需要安装 Icarus Verilog",
)

DESIGN = """
`include "defs.vh"
module d;
  initial $display("WIDTH=%0d", `WIDTH);
endmodule
"""

TB = """
`timescale 1ns/1ps
`include "defs.vh"
module tb;
  initial begin
`ifdef STRICT
    $display("ASSERT PASS strict");
`else
    $display("ASSERT PASS ok");
`endif
    $display("COVER c");
    #5 $finish;
  end
endmodule
"""

HEADER = "`define WIDTH 8\n"


@pytest.fixture
def files(tmp_path):
    src = tmp_path / "d.v"
    src.write_text(DESIGN)
    tb = tmp_path / "tb.v"
    tb.write_text(TB)
    inc = tmp_path / "inc"
    inc.mkdir()
    (inc / "defs.vh").write_text(HEADER)
    return tmp_path, src, tb, inc


# ---------------- parse_compile_flags 单元校验 ----------------

def test_flags_defaults_empty():
    assert parse_compile_flags(None, None) == ([], [])


def test_flags_valid(tmp_path):
    dirs, defs = parse_compile_flags(
        [str(tmp_path)], ["A", "B=1", "C=", "D=x=y", "_E9=0"]
    )
    assert dirs == [str(tmp_path)]
    assert defs == ["A", "B=1", "C=", "D=x=y", "_E9=0"]


def test_incdir_empty_rejected():
    with pytest.raises(InputError):
        parse_compile_flags([""], [])
    with pytest.raises(InputError):
        parse_compile_flags(["   "], [])


def test_incdir_missing_rejected(tmp_path):
    with pytest.raises(InputError):
        parse_compile_flags([str(tmp_path / "nope")], [])


def test_incdir_not_a_directory_rejected(tmp_path):
    f = tmp_path / "f.v"
    f.write_text("module f; endmodule\n")
    with pytest.raises(InputError):
        parse_compile_flags([str(f)], [])


@pytest.mark.parametrize("bad", ["a\nb", "a\rb", "a\0b"])
def test_incdir_forbidden_chars_rejected(bad):
    with pytest.raises(InputError):
        parse_compile_flags([bad], [])


@pytest.mark.parametrize("bad", ["", "=1", "1A", "A-B", "A B", "中文", "A.B"])
def test_define_name_invalid_rejected(bad):
    with pytest.raises(InputError):
        parse_compile_flags([], [bad])


def test_define_duplicate_name_rejected():
    with pytest.raises(InputError):
        parse_compile_flags([], ["A=1", "A=2"])
    with pytest.raises(InputError):
        parse_compile_flags([], ["A", "A="])


@pytest.mark.parametrize("bad", ["A\nB", "A\rB", "A\0B", "A=1\n2"])
def test_define_forbidden_chars_rejected(bad):
    with pytest.raises(InputError):
        parse_compile_flags([], [bad])


def test_define_empty_value_still_defined():
    # 空值宏按已定义处理：原样保留 "NAME="。
    _, defs = parse_compile_flags([], ["FLAG="])
    assert defs == ["FLAG="]


# ---------------- compile_sources 命令构造 ----------------

@needs_icarus
def test_compile_command_includes_flags_in_order(tmp_path):
    src = tmp_path / "d.v"
    src.write_text("module d; endmodule\n")
    out = tmp_path / "a.vvp"
    rc, _o, _e, cmd = compile_sources(
        source_files=[str(src)], top="d", output_path=str(out),
        include_dirs=["inc1", "inc2"], defines=["A", "B=2"],
    )
    assert rc == 0
    assert cmd[cmd.index("-I") + 1] == "inc1"
    i1 = cmd.index("inc1")
    i2 = cmd.index("inc2")
    d1 = cmd.index("A")
    d2 = cmd.index("B=2")
    # 顺序与给出顺序一致，且都在源文件之前。
    assert i1 < i2 < d1 < d2 < cmd.index(str(src))
    assert cmd[i1 - 1] == "-I" and cmd[d1 - 1] == "-D"


@needs_icarus
def test_compile_command_unchanged_without_flags(tmp_path):
    src = tmp_path / "d.v"
    src.write_text("module d; endmodule\n")
    out = tmp_path / "a.vvp"
    _rc, _o, _e, cmd = compile_sources(
        source_files=[str(src)], top="d", output_path=str(out),
    )
    assert "-I" not in cmd and "-D" not in cmd


# ---------------- run / regress / verify 集成 ----------------

@needs_icarus
def test_run_with_incdir_and_define(files):
    tmp_path, src, tb, inc = files
    config = RunConfig(
        sources=[str(src)], testbench=str(tb), top="tb", duration="100ns",
        workdir=str(tmp_path / "w"), report_path=str(tmp_path / "r.json"),
        include_dirs=[str(inc)], defines=["STRICT"],
    )
    report = run(config)
    assert report["status"] == "passed"
    compile_cmd = report["command"]["compile"]
    assert "-I" in compile_cmd and "-D" in compile_cmd
    assert "STRICT" in compile_cmd
    assert "strict" in [a["name"] for a in report["assertions"]]


@needs_icarus
def test_run_without_flags_compile_fails_exit_3(files):
    tmp_path, src, tb, _inc = files
    argv = [
        "run", str(src), "--tb", str(tb), "--top", "tb",
        "--duration", "100ns", "--workdir", str(tmp_path / "w"),
        "--report", str(tmp_path / "r.json"),
    ]
    # 缺 include 目录，`include 找不到 => 既有编译失败路径。
    assert main(argv) == 3
    assert json.load(open(tmp_path / "r.json"))["status"] == "compile_failed"


@needs_icarus
def test_run_cli_with_flags_exit_0(files):
    tmp_path, src, tb, inc = files
    argv = [
        "run", str(src), "--tb", str(tb), "--top", "tb",
        "--duration", "100ns", "--workdir", str(tmp_path / "w"),
        "--report", str(tmp_path / "r.json"),
        "--incdir", str(inc), "--define", "STRICT",
    ]
    assert main(argv) == 0
    report = json.load(open(tmp_path / "r.json"))
    assert report["status"] == "passed"
    assert "STRICT" in report["command"]["compile"]


@needs_icarus
def test_run_bad_incdir_exit_2_no_report(files):
    tmp_path, src, tb, _inc = files
    argv = [
        "run", str(src), "--tb", str(tb), "--top", "tb",
        "--duration", "100ns", "--workdir", str(tmp_path / "w"),
        "--report", str(tmp_path / "r.json"),
        "--incdir", str(tmp_path / "missing"),
    ]
    assert main(argv) == 2
    assert not (tmp_path / "r.json").exists()


@needs_icarus
def test_run_duplicate_define_exit_2_no_report(files):
    tmp_path, src, tb, inc = files
    argv = [
        "run", str(src), "--tb", str(tb), "--top", "tb",
        "--duration", "100ns", "--workdir", str(tmp_path / "w"),
        "--report", str(tmp_path / "r.json"),
        "--incdir", str(inc), "--define", "A=1", "--define", "A=2",
    ]
    assert main(argv) == 2
    assert not (tmp_path / "r.json").exists()


@needs_icarus
def test_regress_with_flags_compile_once(files):
    tmp_path, src, tb, inc = files
    config = RegressConfig(
        sources=[str(src)], testbench=str(tb), top="tb", duration="100ns",
        seeds=[1, 2], workdir=str(tmp_path / "w"),
        report_path=str(tmp_path / "r.json"),
        include_dirs=[str(inc)], defines=["STRICT=1"],
    )
    from rtl_lab.runner import regress
    report = regress(config)
    assert report["status"] == "passed"
    assert len(report["runs"]) == 2
    compile_cmd = report["command"]["compile"]
    assert "-I" in compile_cmd and "STRICT=1" in compile_cmd


@needs_icarus
def test_regress_bad_define_exit_2_no_report(files):
    tmp_path, src, tb, _inc = files
    argv = [
        "regress", str(src), "--tb", str(tb), "--top", "tb",
        "--duration", "100ns", "--seeds", "1,2",
        "--workdir", str(tmp_path / "w"),
        "--report", str(tmp_path / "r.json"),
        "--define", "1BAD",
    ]
    assert main(argv) == 2
    assert not (tmp_path / "r.json").exists()


@needs_icarus
def test_verify_with_flags(files):
    tmp_path, src, tb, inc = files
    argv = [
        "verify", str(src), "--run-id", "flags", "--duration", "100ns",
        "--tb", str(tb), "--workdir", str(tmp_path / "w"),
        "--report", str(tmp_path / "r.json"),
        "--incdir", str(inc), "--define", "STRICT",
    ]
    assert main(argv) == 0
    report = json.load(open(tmp_path / "r.json"))
    assert report["result"] == "passed"
    compile_cmd = report["testbenches"][0]["command"]["compile"]
    assert "-I" in compile_cmd and "STRICT" in compile_cmd


@needs_icarus
def test_verify_multiseed_compile_failure_runs_empty(files):
    tmp_path, src, tb, _inc = files
    # 不给 --incdir：`include 找不到 => 该台 compilation_failed 且 runs 为空。
    argv = [
        "verify", str(src), "--run-id", "flags", "--duration", "100ns",
        "--tb", str(tb), "--seeds", "1,2",
        "--workdir", str(tmp_path / "w"),
        "--report", str(tmp_path / "r.json"),
    ]
    assert main(argv) == 7
    report = json.load(open(tmp_path / "r.json"))
    entry = report["testbenches"][0]
    assert entry["reason"] == "compilation_failed"
    assert entry["runs"] == []


@needs_icarus
def test_verify_bad_incdir_exit_2_no_report(files):
    tmp_path, src, tb, _inc = files
    argv = [
        "verify", str(src), "--run-id", "flags", "--duration", "100ns",
        "--tb", str(tb), "--workdir", str(tmp_path / "w"),
        "--report", str(tmp_path / "r.json"),
        "--incdir", str(tmp_path / "missing"),
    ]
    assert main(argv) == 2
    assert not (tmp_path / "r.json").exists()


def test_verify_config_defaults_no_flags(files):
    tmp_path, src, tb, _inc = files
    config = VerifyConfig(
        sources=[str(src)], testbenches=[{"testbench": str(tb)}],
        run_id="x", duration="100ns",
    )
    assert config.include_dirs == [] and config.defines == []
