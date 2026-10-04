"""--parameter（设计参数覆盖）的校验与编译集成测试。"""

import json
import shutil

import pytest

from rtl_lab.cli import main
from rtl_lab.errors import InputError
from rtl_lab.runner import RunConfig, run, validate_compile_options

pytestmark = pytest.mark.skipif(
    shutil.which("iverilog") is None or shutil.which("vvp") is None,
    reason="需要安装 Icarus Verilog",
)

DESIGN = "module d; endmodule\n"

# 带参数的测试台：参数被覆盖为期望值时断言通过，否则失败。
TB_PARAM = """
`timescale 1ns/1ps
module tb;
  parameter WIDTH = 4;
  parameter DEPTH = 2;
  initial begin
    if (WIDTH == 8 && DEPTH == 3)
      $display("ASSERT PASS ok");
    else
      $display("ASSERT FAIL ok params not applied");
    $display("COVER c");
    $display("WIDTH %0d DEPTH %0d", WIDTH, DEPTH);
    #5 $finish;
  end
endmodule
"""

# 不使用参数的朴素通过测试台。
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
    tb.write_text(TB_PARAM)
    return tmp_path, src, tb


def _run_argv(proj, tb_text=None, extra=()):
    tmp_path, src, tb = proj
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

def test_run_with_parameters(proj):
    argv = _run_argv(proj, extra=[
        "--parameter", "tb.WIDTH=8", "--parameter", "tb.DEPTH=3",
    ])
    assert main(argv) == 0
    report = _load_report(proj)
    assert report["status"] == "passed"
    compile_cmd = report["command"]["compile"]
    # 参数按输入顺序出现在编译命令中。
    assert "-Ptb.WIDTH=8" in compile_cmd
    assert "-Ptb.DEPTH=3" in compile_cmd
    assert compile_cmd.index("-Ptb.WIDTH=8") < compile_cmd.index("-Ptb.DEPTH=3")
    assert any("WIDTH 8 DEPTH 3" in d for d in report["diagnostics"])


def test_run_without_parameters_keeps_command_clean(proj):
    argv = _run_argv(proj, TB_PLAIN)
    assert main(argv) == 0
    compile_cmd = _load_report(proj)["command"]["compile"]
    assert not any(t.startswith("-P") for t in compile_cmd)


def test_parameter_value_may_be_empty(proj):
    # 空值通过输入校验；是否被设计接受由 iverilog 裁决（不是退出 2）。
    argv = _run_argv(proj, TB_PLAIN, extra=["--parameter", "tb.WIDTH="])
    assert main(argv) != 2
    compile_cmd = _load_report(proj)["command"]["compile"]
    assert "-Ptb.WIDTH=" in compile_cmd


def test_parameter_value_may_contain_equals(proj):
    argv = _run_argv(proj, TB_PLAIN, extra=["--parameter", "tb.WIDTH=8=9"])
    assert main(argv) != 2
    compile_cmd = _load_report(proj)["command"]["compile"]
    assert "-Ptb.WIDTH=8=9" in compile_cmd


def test_unknown_parameter_is_compile_failure(proj):
    argv = _run_argv(proj, extra=["--parameter", "tb.NOPE=1"])
    assert main(argv) == 3
    report = _load_report(proj)
    assert report["status"] == "compile_failed"
    assert "-Ptb.NOPE=1" in report["command"]["compile"]


# ---------------- regress / verify 透传 ----------------

def test_regress_compiles_once_with_parameters(proj):
    tmp_path, src, tb = proj
    argv = [
        "regress", str(src), "--tb", str(tb), "--top", "tb",
        "--duration", "100ns", "--seeds", "1,2",
        "--workdir", str(tmp_path / "w"),
        "--report", str(tmp_path / "r.json"),
        "--parameter", "tb.WIDTH=8", "--parameter", "tb.DEPTH=3",
    ]
    assert main(argv) == 0
    report = _load_report(proj)
    assert report["status"] == "passed"
    compile_cmd = report["command"]["compile"]
    assert "-Ptb.WIDTH=8" in compile_cmd
    assert "-Ptb.DEPTH=3" in compile_cmd
    assert [r["status"] for r in report["runs"]] == ["passed", "passed"]


def test_verify_with_parameters(proj):
    tmp_path, src, tb = proj
    argv = [
        "verify", str(src), "--run-id", "params", "--duration", "100ns",
        "--tb", str(tb), "--workdir", str(tmp_path / "w"),
        "--report", str(tmp_path / "r.json"),
        "--parameter", "tb.WIDTH=8", "--parameter", "tb.DEPTH=3",
    ]
    assert main(argv) == 0
    report = _load_report(proj)
    assert report["result"] == "passed"
    compile_cmd = report["testbenches"][0]["command"]["compile"]
    assert "-Ptb.WIDTH=8" in compile_cmd
    assert "-Ptb.DEPTH=3" in compile_cmd
    assert "-Ptb.WIDTH=8" in report["config"]["compile"]


def test_verify_multiseed_with_parameters(proj):
    tmp_path, src, tb = proj
    argv = [
        "verify", str(src), "--run-id", "params", "--duration", "100ns",
        "--tb", str(tb), "--seeds", "1,2",
        "--workdir", str(tmp_path / "w"),
        "--report", str(tmp_path / "r.json"),
        "--parameter", "tb.WIDTH=8", "--parameter", "tb.DEPTH=3",
    ]
    assert main(argv) == 0
    report = _load_report(proj)
    assert report["schema_version"] == 5
    assert report["result"] == "passed"
    entry = report["testbenches"][0]
    assert "-Ptb.WIDTH=8" in entry["command"]["compile"]
    assert [r["status"] for r in entry["runs"]] == ["passed", "passed"]


# ---------------- 输入校验（退出码 2，不生成报告） ----------------

@pytest.mark.parametrize("extra", [
    ["--parameter", ""],
    ["--parameter", "tb.WIDTH"],
    ["--parameter", "=8"],
    ["--parameter", ".tb.WIDTH=8"],
    ["--parameter", "tb.=8"],
    ["--parameter", "tb..WIDTH=8"],
    ["--parameter", "1tb.WIDTH=8"],
    ["--parameter", "tb.1WIDTH=8"],
    ["--parameter", "tb.W-1=8"],
    ["--parameter", "tb.W I=8"],
    ["--parameter", "tb.WIDTH=1\n"],
    ["--parameter", "tb.WIDTH=1\r"],
    ["--parameter", "tb.WIDTH=1\x002"],
    ["--parameter", "tb.WIDTH=8", "--parameter", "tb.WIDTH=3"],
    ["--parameter", "tb.WIDTH=8", "--parameter", "tb.WIDTH=8"],
], ids=[
    "empty", "missing-equals", "empty-path", "leading-dot", "trailing-dot",
    "consecutive-dots", "digit-start-first", "digit-start-segment",
    "dash-in-path", "space-in-path", "value-newline", "value-cr",
    "value-nul", "dup-different-value", "dup-same-value",
])
def test_invalid_parameters_exit_2(proj, extra):
    argv = _run_argv(proj, TB_PLAIN, extra=extra)
    assert main(argv) == 2
    assert not (proj[0] / "r.json").exists()


def test_verify_invalid_parameter_exit_2(proj):
    tmp_path, src, tb = proj
    argv = [
        "verify", str(src), "--run-id", "params", "--duration", "100ns",
        "--tb", str(tb), "--workdir", str(tmp_path / "w"),
        "--report", str(tmp_path / "r.json"),
        "--parameter", "tb..WIDTH=8",
    ]
    assert main(argv) == 2
    assert not (tmp_path / "r.json").exists()


# ---------------- 校验单元（不启动 iverilog） ----------------

def _validate(parameters):
    config = RunConfig(
        sources=["d.v"], testbench="tb.v", top="tb",
        duration="100ns", parameters=parameters,
    )
    validate_compile_options(config)
    return config.parameters


def test_validate_accepts_empty_and_equals_and_space_values():
    assert _validate(["tb.WIDTH="]) == ["tb.WIDTH="]
    assert _validate(["tb.WIDTH=8=9"]) == ["tb.WIDTH=8=9"]
    assert _validate(["tb.WIDTH= 8"]) == ["tb.WIDTH= 8"]


def test_validate_preserves_input_order():
    params = ["tb.DEPTH=3", "tb.WIDTH=8", "tb.U_DUT.SIZE=1"]
    assert _validate(params) == params


@pytest.mark.parametrize("item", [
    "", "tb.WIDTH", "=8", ".tb.W=1", "tb.=1", "tb..W=1", "1tb.W=1",
    "tb.1W=1", "tb.W\n=1", "tb.W=1\n", "tb.W=1\r", "tb.W=1\x00",
])
def test_validate_rejects_invalid_items(item):
    with pytest.raises(InputError):
        _validate([item])


def test_validate_rejects_duplicate_path():
    with pytest.raises(InputError):
        _validate(["tb.W=1", "tb.W=2"])


# ---------------- Python 入口 ----------------

def test_python_api_parameters_preserved_in_order(proj):
    tmp_path, src, tb = proj
    config = RunConfig(
        sources=[str(src)], testbench=str(tb), top="tb",
        duration="100ns", workdir=str(tmp_path / "w"),
        parameters=["tb.DEPTH=3", "tb.WIDTH=8"],
    )
    assert config.parameters == ["tb.DEPTH=3", "tb.WIDTH=8"]
    report = run(config)
    assert report["status"] == "passed"
    compile_cmd = report["command"]["compile"]
    assert compile_cmd.index("-Ptb.DEPTH=3") < compile_cmd.index("-Ptb.WIDTH=8")


def test_python_api_duplicate_parameter_raises(proj):
    tmp_path, src, tb = proj
    config = RunConfig(
        sources=[str(src)], testbench=str(tb), top="tb",
        duration="100ns", workdir=str(tmp_path / "w"),
        parameters=["tb.WIDTH=8", "tb.WIDTH=3"],
    )
    with pytest.raises(InputError):
        run(config)
