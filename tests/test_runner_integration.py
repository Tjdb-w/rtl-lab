"""端到端集成测试：需要 iverilog/vvp，缺失时自动跳过。"""

import json
import shutil

import pytest

from rtl_lab.errors import (
    InputError, CompilationError, SimulationError,
)
from rtl_lab.runner import RunConfig, run

pytestmark = pytest.mark.skipif(
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
    #10 $finish;
  end
endmodule
"""

TB_FAIL = """
`timescale 1ns/1ps
module tb;
  initial begin
    $display("ASSERT PASS a");
    #1 $display("ASSERT FAIL a later failure msg");
    #10 $finish;
  end
endmodule
"""

TB_FATAL = """
`timescale 1ns/1ps
module tb;
  initial #5 $fatal(1, "boom");
endmodule
"""

TB_COMPERR = """
`timescale 1ns/1ps
module tb; ghost_mod g(); initial #1 $finish; endmodule
"""

TB_INFINITE = """
`timescale 1ns/1ps
module tb; initial forever #1; endmodule
"""

TB_STDERR = """
`timescale 1ns/1ps
module tb;
  initial begin
    $fwrite(32'h8000_0002, "ERRSTREAM\\n");
    $display("OUTSTREAM");
    $display("ASSERT PASS stream");
    #1 $finish;
  end
endmodule
"""


@pytest.fixture
def project(tmp_path):
    src = tmp_path / "counter.v"
    src.write_text(COUNTER)
    return tmp_path, src


def _cfg(project, tb_text, *, duration="100ns", top="tb", seed=42, **kw):
    tmp_path, src = project
    tb = tmp_path / "tb.v"
    tb.write_text(tb_text)
    workdir = tmp_path / "work"
    report = tmp_path / "report.json"
    return RunConfig(
        sources=[str(src)], testbench=str(tb), top=top,
        duration=duration, workdir=str(workdir), seed=seed,
        report_path=str(report), **kw
    ), report


def _load(report_path):
    with open(report_path, encoding="utf-8") as f:
        return json.load(f)


def test_passed_flow(project):
    cfg, report_path = _cfg(project, TB_PASS)
    report = run(cfg)
    assert report["status"] == "passed"
    assert report["seed"] == 42
    assert report["duration"] == "100ns"
    assert report["top"] == "tb"
    names = [a["name"] for a in report["assertions"]]
    assert names == ["a1"]
    assert report["assertions"][0]["status"] == "passed"
    assert report["assertions"][0]["fail_count"] == 0
    assert report["coverage"] == [{"name": "c1", "hits": 1}]
    # 种子 plusarg 进入仿真命令，测试台读到了种子。
    assert any("GOTSEED 42" in d for d in report["diagnostics"])
    on_disk = _load(report_path)
    assert on_disk["status"] == "passed"
    # 报告不泄露工作目录绝对路径与工具安装位置。
    blob = json.dumps(on_disk)
    assert "/usr/bin" not in blob


def test_assertion_failed_flow(project):
    cfg, report_path = _cfg(project, TB_FAIL)
    with pytest.raises(SimulationError) as exc:
        run(cfg)
    assert exc.value.assertion_failed is True
    assert exc.value.exit_code == 6
    report = exc.value.report
    assert report["status"] == "assertion_failed"
    # 最后一次结果决定状态；失败次数仍被记录。
    a = report["assertions"][0]
    assert a["name"] == "a" and a["status"] == "failed"
    assert a["fail_count"] == 1
    assert _load(report_path)["status"] == "assertion_failed"


def test_simulation_nonzero_exit_flow(project):
    cfg, report_path = _cfg(project, TB_FATAL)
    with pytest.raises(SimulationError) as exc:
        run(cfg)
    assert exc.value.exit_code == 5
    assert exc.value.assertion_failed is False
    assert exc.value.report["status"] == "simulation_failed"
    assert any("boom" in d for d in exc.value.report["diagnostics"])
    assert _load(report_path)["status"] == "simulation_failed"


def test_compilation_failure_flow(project):
    cfg, report_path = _cfg(project, TB_COMPERR)
    with pytest.raises(CompilationError) as exc:
        run(cfg)
    assert exc.value.exit_code == 3
    assert exc.value.report["status"] == "compile_failed"
    assert exc.value.report["diagnostics"]  # 含编译诊断
    assert _load(report_path)["status"] == "compile_failed"


def test_watchdog_caps_runaway_simulation(project):
    cfg, report_path = _cfg(project, TB_INFINITE, duration="100ns")
    report = run(cfg)
    assert report["status"] == "passed"
    assert any("RTL_LAB_DURATION_REACHED" in d
               for d in report["diagnostics"])


def test_stdout_and_stderr_both_preserved(project):
    cfg, _ = _cfg(project, TB_STDERR)
    report = run(cfg)
    joined = "\n".join(report["diagnostics"])
    assert "OUTSTREAM" in joined and "ERRSTREAM" in joined
    # 两流分别保留（stderr 是独立条目）。
    assert any("ERRSTREAM" in d for d in report["diagnostics"])


@pytest.mark.parametrize("duration", ["1fs", "1ps", "1ns", "1us", "1ms", "1s"])
def test_all_time_units(project, duration):
    cfg, _ = _cfg(project, TB_PASS, duration=duration)
    assert run(cfg)["duration"] == duration


def test_sv_suffix_accepted(tmp_path):
    src = tmp_path / "d.sv"
    src.write_text("module d(input logic a, output logic y); assign y = ~a; endmodule\n")
    tb = tmp_path / "tb.v"
    tb.write_text(TB_PASS)
    cfg = RunConfig(
        sources=[str(src)], testbench=str(tb), top="tb",
        duration="100ns", workdir=str(tmp_path / "w"),
    )
    assert run(cfg)["status"] == "passed"


def test_missing_source_raises_and_no_report(project):
    tmp_path, src = project
    cfg = RunConfig(
        sources=[str(tmp_path / "ghost.v")], testbench=str(src),
        top="tb", duration="100ns", workdir=str(tmp_path / "w"),
        report_path=str(tmp_path / "r.json"),
    )
    with pytest.raises(InputError) as exc:
        run(cfg)
    assert exc.value.exit_code == 2
    assert not (tmp_path / "r.json").exists()


def test_bad_suffix(project):
    tmp_path, src = project
    bad = tmp_path / "x.txt"
    bad.write_text("x")
    cfg = RunConfig(
        sources=[str(bad)], testbench=str(src), top="tb", duration="100ns",
    )
    with pytest.raises(InputError):
        run(cfg)


def test_empty_top(project):
    cfg, _ = _cfg(project, TB_PASS, top="   ")
    with pytest.raises(InputError):
        run(cfg)


def test_nonpositive_or_bad_duration(project):
    for bad in ("0ns", "-1ns", "100mhz", "100", "abc"):
        cfg, _ = _cfg(project, TB_PASS, duration=bad)
        with pytest.raises(InputError):
            run(cfg)


def test_negative_seed(project):
    cfg, _ = _cfg(project, TB_PASS, seed=-3)
    with pytest.raises(InputError):
        run(cfg)


def test_multiple_sources_order_preserved(tmp_path):
    d1 = tmp_path / "a.v"
    d2 = tmp_path / "b.v"
    d1.write_text("module a_mod; endmodule\n")
    d2.write_text("module b_mod; endmodule\n")
    tb = tmp_path / "tb.v"
    tb.write_text(TB_PASS)
    cfg = RunConfig(
        sources=[str(d2), str(d1)], testbench=str(tb), top="tb",
        duration="100ns", workdir=str(tmp_path / "w"),
    )
    report = run(cfg)
    assert report["sources"] == ["b.v", "a.v"]
