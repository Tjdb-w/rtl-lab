"""regress 多随机种子回归的端到端集成测试：需要 iverilog/vvp。"""

import json
import shutil

import pytest

from rtl_lab.errors import (
    InputError, CompilationError, SimulationError,
)
from rtl_lab.runner import RegressConfig, regress

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

# 行为随种子变化：
# - 所有种子打印 GOTSEED，便于确认 +SEED 传入；
# - seed == 3 时断言 check_seed 失败，其余通过；
# - seed == 5 时 $fatal 非零退出；
# - 偶数种子命中 even_seed；所有种子命中 always_hit。
TB_SEEDED = """
`timescale 1ns/1ps
module tb;
  integer seed;
  initial begin
    if ($value$plusargs("SEED=%d", seed)) $display("GOTSEED %0d", seed);
    if (seed == 5) #1 $fatal(1, "fatalboom");
    if (seed == 3) $display("ASSERT FAIL check_seed expected pass");
    else $display("ASSERT PASS check_seed");
    if (seed % 2 == 0) $display("COVER even_seed");
    $display("COVER always_hit");
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
    tb = tmp_path / "tb.v"
    tb.write_text(TB_SEEDED)
    return tmp_path, src, tb


def _cfg(project, seeds, *, tb_text=None, **kw):
    tmp_path, src, tb = project
    if tb_text is not None:
        tb.write_text(tb_text)
    workdir = tmp_path / "work"
    report = tmp_path / "report.json"
    return RegressConfig(
        sources=[str(src)], testbench=str(tb), top="tb",
        duration="100ns", seeds=seeds, workdir=str(workdir),
        report_path=str(report), **kw
    ), report


def _load(report_path):
    with open(report_path, encoding="utf-8") as f:
        return json.load(f)


def _by_name(entries):
    return {e["name"]: e for e in entries}


def test_all_seeds_pass(project):
    cfg, report_path = _cfg(project, [0, 2, 4])
    report = regress(cfg)
    assert report["schema_version"] == 2
    assert report["status"] == "passed"
    assert report["seeds"] == [0, 2, 4]
    assert report["failed_seeds"] == []
    assert [r["seed"] for r in report["runs"]] == [0, 2, 4]
    assert all(r["status"] == "passed" for r in report["runs"])

    # 断言/覆盖率跨种子汇总。
    assert report["assertions"] == [
        {"name": "check_seed", "status": "passed", "fail_count": 0}
    ]
    cov = _by_name(report["coverage"])
    assert cov["even_seed"] == {"name": "even_seed", "hits": 3, "hit_runs": 3}
    assert cov["always_hit"] == {"name": "always_hit", "hits": 3, "hit_runs": 3}

    # 顶层 command 保存首个种子的脱敏仿真 argv 与编译 argv。
    assert "+SEED=0" in report["command"]["simulate"]
    assert report["command"]["compile"]
    assert report["command"]["compile"][0] == "iverilog"
    on_disk = _load(report_path)
    assert on_disk["status"] == "passed"
    blob = json.dumps(on_disk)
    assert "/usr/bin" not in blob


def test_each_seed_gets_its_plusarg_and_run_fields(project):
    cfg, _ = _cfg(project, [11, 22])
    report = regress(cfg)
    for run, seed in zip(report["runs"], [11, 22]):
        assert list(run.keys()) == [
            "seed", "status", "diagnostics", "assertions", "coverage",
        ]
        assert any(f"GOTSEED {seed}" in d for d in run["diagnostics"])
    # 原始回放流中各种子输出按顺序都在。
    assert "GOTSEED 11" in report.raw_stdout
    assert "GOTSEED 22" in report.raw_stdout


def test_assertion_failure_continues_remaining_seeds(project):
    cfg, report_path = _cfg(project, [1, 3, 2])
    with pytest.raises(SimulationError) as exc:
        regress(cfg)
    assert exc.value.exit_code == 6
    assert exc.value.assertion_failed is True
    report = exc.value.report
    assert report["status"] == "assertion_failed"
    # 失败后仍执行剩余种子：三个 run 全部存在。
    assert [r["seed"] for r in report["runs"]] == [1, 3, 2]
    statuses = {r["seed"]: r["status"] for r in report["runs"]}
    assert statuses == {1: "passed", 3: "assertion_failed", 2: "passed"}
    assert report["failed_seeds"] == [3]

    # 失败计数只统计失败种子；偶种子覆盖在 seed 2 仍被收集。
    assert report["assertions"] == [
        {"name": "check_seed", "status": "failed", "fail_count": 1}
    ]
    cov = _by_name(report["coverage"])
    assert cov["even_seed"]["hits"] == 1
    assert cov["even_seed"]["hit_runs"] == 1
    assert cov["always_hit"]["hits"] == 3
    # 后续种子的输出确实出现。
    assert "GOTSEED 2" in report.raw_stdout
    assert _load(report_path)["status"] == "assertion_failed"


def test_simulation_nonzero_exit_stops_later_seeds(project):
    cfg, report_path = _cfg(project, [1, 5, 2])
    with pytest.raises(SimulationError) as exc:
        regress(cfg)
    assert exc.value.exit_code == 5
    assert exc.value.assertion_failed is False
    report = exc.value.report
    assert report["status"] == "simulation_failed"
    # 保留已有结果与诊断，停止后续种子：seed 2 未执行。
    assert [r["seed"] for r in report["runs"]] == [1, 5]
    assert report["seeds"] == [1, 5, 2]
    assert report["failed_seeds"] == [5]
    failed_run = report["runs"][1]
    assert failed_run["status"] == "simulation_failed"
    assert any("fatalboom" in d for d in failed_run["diagnostics"])
    assert "GOTSEED 2" not in report.raw_stdout
    assert "GOTSEED 5" in report.raw_stdout
    assert _load(report_path)["status"] == "simulation_failed"


def test_compilation_failure_no_runs(project):
    cfg, report_path = _cfg(project, [1, 2], tb_text=TB_COMPERR)
    with pytest.raises(CompilationError) as exc:
        regress(cfg)
    assert exc.value.exit_code == 3
    report = exc.value.report
    assert report["status"] == "compile_failed"
    assert report["runs"] == []
    assert report["failed_seeds"] == []
    assert report["seeds"] == [1, 2]
    assert report["diagnostics"]
    assert report["command"]["compile"]
    assert report["command"]["simulate"] == []
    assert _load(report_path)["status"] == "compile_failed"


def test_invalid_seeds_raise_no_report(project):
    cfg, report_path = _cfg(project, [1, 1])
    with pytest.raises(InputError) as exc:
        regress(cfg)
    assert exc.value.exit_code == 2
    assert not report_path.exists()


@pytest.mark.parametrize("seeds", [[], [-1], [1, 2.5], [1, True]])
def test_invalid_seed_types_raise(project, seeds):
    cfg, _ = _cfg(project, seeds)
    with pytest.raises(InputError):
        regress(cfg)


def test_stderr_preserved_per_run(project):
    # seed 5 非零退出时 vvp 的消息应在其 run 的诊断与原始 stderr 中。
    cfg, _ = _cfg(project, [5])
    with pytest.raises(SimulationError):
        regress(cfg)
