"""--incdir/--define（include 目录与宏定义）的校验与编译集成测试。"""

import json
import shutil

import pytest

from rtl_lab.cli import main
from rtl_lab.errors import InputError
from rtl_lab.runner import RunConfig, run

pytestmark = pytest.mark.skipif(
    shutil.which("iverilog") is None or shutil.which("vvp") is None,
    reason="需要安装 Icarus Verilog",
)

DESIGN = "module d; endmodule\n"

# 含 include 与条件宏的测试台：FLAG 定义时通过，否则断言失败。
TB_INCLUDE = """
`timescale 1ns/1ps
`include "params.vh"
module tb;
  initial begin
`ifdef FLAG
    $display("ASSERT PASS ok");
`else
    $display("ASSERT FAIL ok flag missing");
`endif
    $display("COVER c");
    $display("WIDTH %0d", `WIDTH);
    #5 $finish;
  end
endmodule
"""

# 仅用条件宏、不含 include 的测试台。
TB_MACRO = """
`timescale 1ns/1ps
module tb;
  initial begin
`ifdef FLAG
    $display("ASSERT PASS ok");
`else
    $display("ASSERT FAIL ok flag missing");
`endif
    $display("COVER c");
    #5 $finish;
  end
endmodule
"""

# 不使用 include/宏的朴素通过测试台。
TB_PLAIN = """
`timescale 1ns/1ps
module tb;
  initial begin
    $display("ASSERT PASS ok");
    $display("COVER c");
    #5 $finish;
  end
endmodule
"""


@pytest.fixture
def proj(tmp_path):
    src = tmp_path / "d.v"
    src.write_text(DESIGN)
    tb = tmp_path / "tb.v"
    tb.write_text(TB_INCLUDE)
    inc = tmp_path / "incdir"
    inc.mkdir()
    (inc / "params.vh").write_text("`define WIDTH 8\n")
    return tmp_path, src, tb, inc


def _run_argv(proj, tb_text=None, extra=()):
    tmp_path, src, tb, _inc = proj
    if tb_text is not None:
        tb.write_text(tb_text)
    return [
        "run", str(src), "--tb", str(tb), "--top", "tb",
        "--duration", "100ns", "--workdir", str(tmp_path / "w"),
        "--report", str(tmp_path / "r.json"),
    ] + list(extra)


def _load_report(proj):
    return json.load(open(proj[0] / "r.json"))


# ---------------- 编译参数透传 ----------------

def test_run_with_incdir_and_define(proj):
    tmp_path, _src, _tb, inc = proj
    argv = _run_argv(proj, extra=[
        "--incdir", str(inc), "--define", "FLAG=1",
    ])
    assert main(argv) == 0
    report = _load_report(proj)
    assert report["status"] == "passed"
    compile_cmd = report["command"]["compile"]
    assert "-I" in compile_cmd
    # 绝对路径在报告中脱敏为 basename。
    assert "incdir" in compile_cmd
    assert "-DFLAG=1" in compile_cmd
    assert any("WIDTH 8" in d for d in report["diagnostics"])


def test_run_without_flags_keeps_command_clean(proj):
    argv = _run_argv(proj, TB_PLAIN)
    assert main(argv) == 0
    compile_cmd = _load_report(proj)["command"]["compile"]
    assert "-I" not in compile_cmd
    assert not any(t.startswith("-D") for t in compile_cmd)


def test_relative_incdir_resolved_from_cwd(proj, monkeypatch):
    tmp_path, src, tb, _inc = proj
    monkeypatch.chdir(tmp_path)
    argv = [
        "run", str(src), "--tb", str(tb), "--top", "tb",
        "--duration", "100ns", "--workdir", str(tmp_path / "w"),
        "--report", str(tmp_path / "r.json"),
        "--incdir", "incdir", "--define", "FLAG",
    ]
    assert main(argv) == 0
    compile_cmd = _load_report(proj)["command"]["compile"]
    # 相对路径不泄露绝对位置，原样保留。
    assert "incdir" in compile_cmd
    assert "-DFLAG" in compile_cmd


def test_define_empty_value_still_defined(proj):
    argv = _run_argv(proj, TB_MACRO, extra=["--define", "FLAG="])
    assert main(argv) == 0
    report = _load_report(proj)
    assert report["status"] == "passed"
    assert "-DFLAG=" in report["command"]["compile"]


def test_define_value_may_contain_equals(proj):
    argv = _run_argv(proj, TB_PLAIN, extra=["--define", "EXPR=a=b"])
    assert main(argv) == 0
    assert "-DEXPR=a=b" in _load_report(proj)["command"]["compile"]


def test_missing_include_file_is_compile_failure(proj):
    tmp_path, src, tb, _inc = proj
    tb.write_text(
        "`timescale 1ns/1ps\n"
        "`include \"no_such_header.vh\"\n"
        "module tb; initial #5 $finish; endmodule\n"
    )
    argv = _run_argv(proj)
    assert main(argv) == 3
    assert _load_report(proj)["status"] == "compile_failed"


# ---------------- regress / verify 透传 ----------------

def test_regress_compiles_once_with_flags(proj):
    tmp_path, src, tb, inc = proj
    argv = [
        "regress", str(src), "--tb", str(tb), "--top", "tb",
        "--duration", "100ns", "--seeds", "1,2",
        "--workdir", str(tmp_path / "w"),
        "--report", str(tmp_path / "r.json"),
        "--incdir", str(inc), "--define", "FLAG=1",
    ]
    assert main(argv) == 0
    report = _load_report(proj)
    assert report["status"] == "passed"
    assert "-DFLAG=1" in report["command"]["compile"]
    assert [r["status"] for r in report["runs"]] == ["passed", "passed"]


def test_verify_with_incdir_and_define(proj):
    tmp_path, src, tb, inc = proj
    argv = [
        "verify", str(src), "--run-id", "flags", "--duration", "100ns",
        "--tb", str(tb), "--workdir", str(tmp_path / "w"),
        "--report", str(tmp_path / "r.json"),
        "--incdir", str(inc), "--define", "FLAG=1",
    ]
    assert main(argv) == 0
    report = _load_report(proj)
    assert report["result"] == "passed"
    compile_cmd = report["testbenches"][0]["command"]["compile"]
    assert "-I" in compile_cmd
    assert "-DFLAG=1" in compile_cmd


def test_verify_multiseed_with_incdir_and_define(proj):
    tmp_path, src, tb, inc = proj
    argv = [
        "verify", str(src), "--run-id", "flags", "--duration", "100ns",
        "--tb", str(tb), "--seeds", "1,2",
        "--workdir", str(tmp_path / "w"),
        "--report", str(tmp_path / "r.json"),
        "--incdir", str(inc), "--define", "FLAG=1",
    ]
    assert main(argv) == 0
    report = _load_report(proj)
    assert report["schema_version"] == 5
    assert report["result"] == "passed"
    entry = report["testbenches"][0]
    assert "-DFLAG=1" in entry["command"]["compile"]
    assert [r["status"] for r in entry["runs"]] == ["passed", "passed"]


# ---------------- 输入校验（退出码 2，不生成报告） ----------------

@pytest.mark.parametrize("extra", [
    ["--incdir", ""],
    ["--incdir", "a\nb"],
    ["--incdir", "a\rb"],
    ["--incdir", "a\x00b"],
    ["--define", ""],
    ["--define", "1ABC"],
    ["--define", "A-B"],
    ["--define", "A B"],
    ["--define", "FOO\n"],
    ["--define", "FOO=1", "--define", "FOO=2"],
    ["--define", "FOO", "--define", "FOO"],
], ids=[
    "incdir-empty", "incdir-newline", "incdir-cr", "incdir-nul",
    "define-empty", "define-digit-start", "define-dash", "define-space",
    "define-newline", "define-dup-valued", "define-dup-bare",
])
def test_invalid_compile_options_exit_2(proj, extra):
    argv = _run_argv(proj, TB_PLAIN, extra=extra)
    assert main(argv) == 2
    assert not (proj[0] / "r.json").exists()


def test_incdir_missing_exit_2(proj):
    argv = _run_argv(proj, TB_PLAIN,
                     extra=["--incdir", str(proj[0] / "nope")])
    assert main(argv) == 2
    assert not (proj[0] / "r.json").exists()


def test_incdir_not_a_directory_exit_2(proj):
    argv = _run_argv(proj, TB_PLAIN, extra=["--incdir", str(proj[1])])
    assert main(argv) == 2
    assert not (proj[0] / "r.json").exists()


def test_verify_invalid_define_exit_2(proj):
    tmp_path, src, tb, _inc = proj
    argv = [
        "verify", str(src), "--run-id", "flags", "--duration", "100ns",
        "--tb", str(tb), "--workdir", str(tmp_path / "w"),
        "--report", str(tmp_path / "r.json"),
        "--define", "1BAD",
    ]
    assert main(argv) == 2
    assert not (tmp_path / "r.json").exists()


# ---------------- Python 入口 ----------------

def test_python_api_include_dirs_and_defines(proj):
    tmp_path, src, tb, inc = proj
    config = RunConfig(
        sources=[str(src)], testbench=str(tb), top="tb",
        duration="100ns", workdir=str(tmp_path / "w"),
        include_dirs=[str(inc)], defines=["FLAG=1"],
    )
    report = run(config)
    assert report["status"] == "passed"


def test_python_api_duplicate_define_raises(proj):
    tmp_path, src, tb, _inc = proj
    config = RunConfig(
        sources=[str(src)], testbench=str(tb), top="tb",
        duration="100ns", workdir=str(tmp_path / "w"),
        defines=["A=1", "A=2"],
    )
    with pytest.raises(InputError):
        run(config)
