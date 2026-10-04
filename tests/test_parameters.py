"""--parameter（设计参数覆盖）的校验与编译集成测试。"""

import json
import shutil

import pytest

from rtl_lab.cli import main
from rtl_lab.errors import InputError
from rtl_lab.runner import RunConfig, run
from rtl_lab.verification import TestSpec, VerifyConfig, verify

pytestmark = pytest.mark.skipif(
    shutil.which("iverilog") is None or shutil.which("vvp") is None,
    reason="需要安装 Icarus Verilog",
)

DESIGN = "module d; endmodule\n"

# 带参数的测试台：WIDTH=8 且 DEPTH=3 时断言通过，否则失败。
TB_PARAM = """
`timescale 1ns/1ps
module tb;
  parameter WIDTH = 4;
  parameter DEPTH = 2;
  initial begin
    if (WIDTH == 8 && DEPTH == 3) $display("ASSERT PASS ok");
    else $display("ASSERT FAIL ok params not applied");
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


# ---------------- 参数覆盖透传 ----------------

def test_run_with_parameters(proj):
    argv = _run_argv(proj, extra=[
        "--parameter", "tb.WIDTH=8", "--parameter", "tb.DEPTH=3",
    ])
    assert main(argv) == 0
    report = _load_report(proj)
    assert report["status"] == "passed"
    compile_cmd = report["command"]["compile"]
    assert "-Ptb.WIDTH=8" in compile_cmd
    assert "-Ptb.DEPTH=3" in compile_cmd
    assert any("WIDTH 8 DEPTH 3" in d for d in report["diagnostics"])


def test_run_without_parameters_keeps_command_clean(proj):
    argv = _run_argv(proj, TB_PLAIN)
    assert main(argv) == 0
    compile_cmd = _load_report(proj)["command"]["compile"]
    assert not any(t.startswith("-P") for t in compile_cmd)


def test_parameter_order_preserved(proj):
    argv = _run_argv(proj, extra=[
        "--parameter", "tb.DEPTH=3", "--parameter", "tb.WIDTH=8",
    ])
    assert main(argv) == 0
    compile_cmd = _load_report(proj)["command"]["compile"]
    p_tokens = [t for t in compile_cmd if t.startswith("-P")]
    assert p_tokens == ["-Ptb.DEPTH=3", "-Ptb.WIDTH=8"]


def test_parameter_value_may_contain_equals(proj):
    # iverilog 不接受含等号的 defparam 值（打印错误但仍以 0 退出），
    # 参数不生效导致断言失败；命令记录须完整保留原始 VALUE。
    argv = _run_argv(proj, extra=["--parameter", "tb.WIDTH=a=b"])
    assert main(argv) == 6
    assert "-Ptb.WIDTH=a=b" in _load_report(proj)["command"]["compile"]


def test_parameter_empty_value_accepted(proj):
    # 空 VALUE 合法（不属于输入错误）；iverilog 拒绝该 defparam 值但
    # 仍以 0 退出，参数不生效导致断言失败。
    argv = _run_argv(proj, extra=["--parameter", "tb.WIDTH="])
    assert main(argv) == 6
    assert "-Ptb.WIDTH=" in _load_report(proj)["command"]["compile"]


def test_unknown_parameter_is_compile_failure(proj):
    argv = _run_argv(proj, extra=["--parameter", "tb.NOPE=1"])
    assert main(argv) == 3
    report = _load_report(proj)
    assert report["status"] == "compile_failed"
    assert "-Ptb.NOPE=1" in report["command"]["compile"]


def test_same_inputs_produce_identical_report(proj):
    argv = _run_argv(proj, extra=[
        "--parameter", "tb.WIDTH=8", "--parameter", "tb.DEPTH=3",
    ])
    assert main(argv) == 0
    first = _load_report(proj)
    assert main(argv) == 0
    second = _load_report(proj)
    # schema v1 无 generated_at：相同输入的报告内容完全一致。
    assert first == second


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
    assert "-Ptb.WIDTH=8" in report["command"]["compile"]
    assert "-Ptb.DEPTH=3" in report["command"]["compile"]
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
    entry_cmd = report["testbenches"][0]["command"]["compile"]
    assert "-Ptb.WIDTH=8" in entry_cmd
    assert "-Ptb.DEPTH=3" in entry_cmd
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
    ["--parameter", "=8"],
    ["--parameter", ".tb.WIDTH=8"],
    ["--parameter", "tb.WIDTH.=8"],
    ["--parameter", "tb..WIDTH=8"],
    ["--parameter", "1tb=8"],
    ["--parameter", "tb.1WIDTH=8"],
    ["--parameter", "tb.W-IDTH=8"],
    ["--parameter", "tb.W IDTH=8"],
    ["--parameter", "tb.WIDTH\n=8"],
    ["--parameter", "tb.WIDTH=8\n9"],
    ["--parameter", "tb.WIDTH=8\r9"],
    ["--parameter", "tb.WIDTH=8\x009"],
    ["--parameter", "tb.WIDTH=8", "--parameter", "tb.WIDTH=9"],
    ["--parameter", "tb.WIDTH", "--parameter", "tb.WIDTH=8"],
], ids=[
    "empty", "path-empty", "path-leading-dot", "path-trailing-dot",
    "path-consecutive-dots", "path-digit-start", "path-segment-digit-start",
    "path-dash", "path-space", "path-newline",
    "value-newline", "value-cr", "value-nul",
    "dup-valued", "dup-bare",
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
        "--parameter", "1BAD=1",
    ]
    assert main(argv) == 2
    assert not (tmp_path / "r.json").exists()


# ---------------- Python 入口 ----------------

def test_python_api_parameters(proj):
    tmp_path, src, tb = proj
    config = RunConfig(
        sources=[str(src)], testbench=str(tb), top="tb",
        duration="100ns", workdir=str(tmp_path / "w"),
        parameters=["tb.WIDTH=8", "tb.DEPTH=3"],
    )
    report = run(config)
    assert report["status"] == "passed"
    compile_cmd = report["command"]["compile"]
    p_tokens = [t for t in compile_cmd if t.startswith("-P")]
    assert p_tokens == ["-Ptb.WIDTH=8", "-Ptb.DEPTH=3"]


def test_python_api_config_preserves_input_order(proj):
    tmp_path, src, tb = proj
    config = RunConfig(
        sources=[str(src)], testbench=str(tb), top="tb",
        duration="100ns", workdir=str(tmp_path / "w"),
        parameters=["tb.DEPTH=3", "tb.WIDTH=8"],
    )
    assert config.parameters == ["tb.DEPTH=3", "tb.WIDTH=8"]


def test_python_api_duplicate_parameter_raises(proj):
    tmp_path, src, tb = proj
    config = RunConfig(
        sources=[str(src)], testbench=str(tb), top="tb",
        duration="100ns", workdir=str(tmp_path / "w"),
        parameters=["tb.WIDTH=8", "tb.WIDTH=9"],
    )
    with pytest.raises(InputError):
        run(config)


def test_python_api_invalid_parameter_raises(proj):
    tmp_path, src, tb = proj
    config = RunConfig(
        sources=[str(src)], testbench=str(tb), top="tb",
        duration="100ns", workdir=str(tmp_path / "w"),
        parameters=["tb..WIDTH=8"],
    )
    with pytest.raises(InputError):
        run(config)


def test_python_api_verify_parameters(proj):
    tmp_path, src, tb = proj
    config = VerifyConfig(
        sources=[str(src)],
        testbenches=[TestSpec(testbench=str(tb))],
        run_id="params", duration="100ns",
        workdir=str(tmp_path / "w"),
        parameters=["tb.WIDTH=8", "tb.DEPTH=3"],
    )
    assert config.parameters == ["tb.WIDTH=8", "tb.DEPTH=3"]
    report = verify(config)
    assert report["result"] == "passed"
    entry_cmd = report["testbenches"][0]["command"]["compile"]
    assert "-Ptb.WIDTH=8" in entry_cmd
