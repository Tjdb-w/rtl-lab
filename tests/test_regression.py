"""多种子回归测试：种子解析、报告结构、退出码与异常语义。"""

import json
import shutil

import pytest

from rtl_lab.cli import main
from rtl_lab.errors import (
    InputError, CompilationError, SimulationError, ToolError,
)
from rtl_lab.regression import RegressConfig, parse_seeds, regress

requires_icarus = pytest.mark.skipif(
    shutil.which("iverilog") is None or shutil.which("vvp") is None,
    reason="需要安装 Icarus Verilog",
)

COUNTER = """
module counter(input clk, input rst, output reg [3:0] q);
  always @(posedge clk or posedge rst)
    if (rst) q <= 4'd0; else q <= q + 4'd1;
endmodule
"""

TB_PASS = """
`timescale 1ns/1ps
module tb;
  integer seed;
  initial begin
    if ($value$plusargs("SEED=%d", seed)) $display("GOTSEED %0d", seed);
    $display("ASSERT PASS a1");
    $display("COVER c1");
    if (seed == 3) $display("COVER c2");
    #10 $finish;
  end
endmodule
"""

TB_SEED2_FAILS = """
`timescale 1ns/1ps
module tb;
  integer seed;
  initial begin
    if ($value$plusargs("SEED=%d", seed)) $display("GOTSEED %0d", seed);
    $display("ASSERT PASS a1");
    if (seed == 2) $display("ASSERT FAIL a2 seed2 broke");
    else $display("ASSERT PASS a2");
    $display("COVER c1");
    #10 $finish;
  end
endmodule
"""

TB_SEED2_FATAL = """
`timescale 1ns/1ps
module tb;
  integer seed;
  initial begin
    if ($value$plusargs("SEED=%d", seed)) $display("GOTSEED %0d", seed);
    $display("ASSERT PASS a1");
    if (seed == 2) #5 $fatal(1, "boom2");
    #10 $finish;
  end
endmodule
"""

TB_COMPERR = """
`timescale 1ns/1ps
module tb; ghost_mod g(); initial #1 $finish; endmodule
"""


@pytest.fixture
def project(tmp_path):
    src = tmp_path / "counter.v"
    src.write_text(COUNTER)
    return tmp_path, src


def _cfg(project, tb_text, *, seeds=(1, 2, 3), duration="100ns", top="tb",
         **kw):
    tmp_path, src = project
    tb = tmp_path / "tb.v"
    tb.write_text(tb_text)
    report = tmp_path / "report.json"
    return RegressConfig(
        sources=[str(src)], testbench=str(tb), top=top,
        duration=duration, seeds=list(seeds),
        workdir=str(tmp_path / "work"), report_path=str(report), **kw
    ), report


def _load(report_path):
    with open(report_path, encoding="utf-8") as f:
        return json.load(f)


# ---- 种子解析（无需 iverilog）----

def test_parse_seeds_valid():
    assert parse_seeds("1,2,3") == [1, 2, 3]
    assert parse_seeds("0") == [0]
    assert parse_seeds(" 7 , 8 ") == [7, 8]


@pytest.mark.parametrize("text", [
    "", "   ", "1,,2", "1,", ",1", "abc", "1.5", "-1", "2,-3", "1,1", "5,2,5",
])
def test_parse_seeds_invalid(text):
    with pytest.raises(InputError) as exc:
        parse_seeds(text)
    assert exc.value.exit_code == 2


@pytest.mark.parametrize("seeds", [
    [], [-1], [1, 1], ["1"], [True], [1.5], None,
])
def test_regress_invalid_seeds_no_report(project, seeds):
    cfg, report_path = _cfg(project, TB_PASS, seeds=seeds or [])
    if seeds is None:
        cfg.seeds = None
    with pytest.raises(InputError):
        regress(cfg)
    assert not report_path.exists()


def test_regress_kwargs_form(project):
    tmp_path, src = project
    tb = tmp_path / "tb.v"
    tb.write_text(TB_PASS)
    with pytest.raises(InputError):
        regress(sources=[str(src)], testbench=str(tb), top="tb",
                duration="100ns", seeds=[1, 1],
                workdir=str(tmp_path / "w"))


def test_cli_bad_seeds_exit_2_no_report(project):
    tmp_path, src = project
    tb = tmp_path / "tb.v"
    tb.write_text(TB_PASS)
    argv = ["regress", str(src), "--tb", str(tb), "--top", "tb",
            "--duration", "100ns", "--seeds", "1,,2",
            "--workdir", str(tmp_path / "w"),
            "--report", str(tmp_path / "r.json")]
    assert main(argv) == 2
    assert not (tmp_path / "r.json").exists()


def test_cli_missing_tool_exit_4_no_report(project, monkeypatch):
    monkeypatch.setattr("rtl_lab.tools.shutil.which", lambda name: None)
    tmp_path, src = project
    tb = tmp_path / "tb.v"
    tb.write_text(TB_PASS)
    argv = ["regress", str(src), "--tb", str(tb), "--top", "tb",
            "--duration", "100ns", "--seeds", "1,2",
            "--workdir", str(tmp_path / "w"),
            "--report", str(tmp_path / "r.json")]
    assert main(argv) == 4
    assert not (tmp_path / "r.json").exists()


def test_regress_missing_tool_raises(project, monkeypatch):
    monkeypatch.setattr("rtl_lab.tools.shutil.which", lambda name: None)
    cfg, report_path = _cfg(project, TB_PASS)
    with pytest.raises(ToolError) as exc:
        regress(cfg)
    assert exc.value.exit_code == 4
    assert not report_path.exists()


# ---- 端到端流程（需要 iverilog/vvp）----

@requires_icarus
def test_regress_passed_flow(project):
    cfg, report_path = _cfg(project, TB_PASS, seeds=(1, 2, 3))
    report = regress(cfg)
    assert report["schema_version"] == 2
    assert report["status"] == "passed"
    assert report["seeds"] == [1, 2, 3]
    assert report["failed_seeds"] == []
    assert report["duration"] == "100ns"
    assert report["top"] == "tb"

    # runs 依种子顺序，各含 seed/status/diagnostics/assertions/coverage。
    assert [r["seed"] for r in report["runs"]] == [1, 2, 3]
    for r in report["runs"]:
        assert r["status"] == "passed"
        assert [a["name"] for a in r["assertions"]] == ["a1"]
        assert any("GOTSEED" in d for d in r["diagnostics"])
    assert any("GOTSEED 2" in d for d in report["runs"][1]["diagnostics"])

    # 汇总：hits 为各种子之和，hit_runs 为命中种子数。
    assert report["assertions"] == [
        {"name": "a1", "status": "passed", "fail_count": 0}
    ]
    assert report["coverage"] == [
        {"name": "c1", "hits": 3, "hit_runs": 3},
        {"name": "c2", "hits": 1, "hit_runs": 1},
    ]

    # command 保存脱敏的编译、仿真 argv（仿真为首个种子的 argv）。
    assert report["command"]["compile"]
    assert "+SEED=1" in report["command"]["simulate"]

    on_disk = _load(report_path)
    assert on_disk["status"] == "passed"
    assert on_disk["schema_version"] == 2
    # 报告不泄露工作目录绝对路径与工具安装位置。
    blob = json.dumps(on_disk)
    assert str(project[0]) not in blob
    assert "/usr/bin" not in blob


@requires_icarus
def test_regress_assertion_failed_runs_all_seeds(project):
    cfg, report_path = _cfg(project, TB_SEED2_FAILS, seeds=(1, 2, 3))
    with pytest.raises(SimulationError) as exc:
        regress(cfg)
    assert exc.value.assertion_failed is True
    assert exc.value.exit_code == 6
    report = exc.value.report
    assert report["status"] == "assertion_failed"
    # 断言失败仍执行剩余种子。
    assert [r["seed"] for r in report["runs"]] == [1, 2, 3]
    assert [r["status"] for r in report["runs"]] == [
        "passed", "assertion_failed", "passed",
    ]
    assert report["failed_seeds"] == [2]
    # 汇总按首次出现排序；fail_count 跨种子求和。
    assert report["assertions"] == [
        {"name": "a1", "status": "passed", "fail_count": 0},
        {"name": "a2", "status": "failed", "fail_count": 1},
    ]
    assert report["coverage"] == [{"name": "c1", "hits": 3, "hit_runs": 3}]
    assert _load(report_path)["status"] == "assertion_failed"


@requires_icarus
def test_regress_simulation_failure_stops_remaining_seeds(project):
    cfg, report_path = _cfg(project, TB_SEED2_FATAL, seeds=(1, 2, 3))
    with pytest.raises(SimulationError) as exc:
        regress(cfg)
    assert exc.value.assertion_failed is False
    assert exc.value.exit_code == 5
    report = exc.value.report
    assert report["status"] == "simulation_failed"
    # 保留已有结果与诊断，后续种子不再执行。
    assert [r["seed"] for r in report["runs"]] == [1, 2]
    assert report["runs"][0]["status"] == "passed"
    assert report["runs"][1]["status"] == "simulation_failed"
    assert any("boom2" in d for d in report["runs"][1]["diagnostics"])
    assert report["failed_seeds"] == [2]
    assert _load(report_path)["status"] == "simulation_failed"


@requires_icarus
def test_regress_compile_failure_no_runs(project):
    cfg, report_path = _cfg(project, TB_COMPERR)
    with pytest.raises(CompilationError) as exc:
        regress(cfg)
    assert exc.value.exit_code == 3
    report = exc.value.report
    assert report["status"] == "compile_failed"
    assert report["runs"] == []
    assert report["diagnostics"]  # 含编译诊断
    assert _load(report_path)["runs"] == []


@requires_icarus
def test_cli_regress_passed_exit_zero(project, capsys):
    tmp_path, src = project
    tb = tmp_path / "tb.v"
    tb.write_text(TB_PASS)
    argv = ["regress", str(src), "--tb", str(tb), "--top", "tb",
            "--duration", "100ns", "--seeds", "1,2,3",
            "--workdir", str(tmp_path / "w"),
            "--report", str(tmp_path / "r.json")]
    assert main(argv) == 0
    report = _load(tmp_path / "r.json")
    assert report["status"] == "passed"
    assert report["seeds"] == [1, 2, 3]
    err = capsys.readouterr().err
    assert "passed" in err and "3 个种子" in err


@requires_icarus
def test_cli_regress_assertion_exit_6(project):
    tmp_path, src = project
    tb = tmp_path / "tb.v"
    tb.write_text(TB_SEED2_FAILS)
    argv = ["regress", str(src), "--tb", str(tb), "--top", "tb",
            "--duration", "100ns", "--seeds", "1,2,3",
            "--workdir", str(tmp_path / "w"),
            "--report", str(tmp_path / "r.json")]
    assert main(argv) == 6
    assert _load(tmp_path / "r.json")["status"] == "assertion_failed"


@requires_icarus
def test_cli_regress_compile_failure_exit_3(project):
    tmp_path, src = project
    tb = tmp_path / "tb.v"
    tb.write_text(TB_COMPERR)
    argv = ["regress", str(src), "--tb", str(tb), "--top", "tb",
            "--duration", "100ns", "--seeds", "1",
            "--workdir", str(tmp_path / "w"),
            "--report", str(tmp_path / "r.json")]
    assert main(argv) == 3
    assert _load(tmp_path / "r.json")["status"] == "compile_failed"
