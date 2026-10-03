"""verify --seeds 多种子矩阵（schema v5）测试。

配置校验与报告构建不调用仿真器；端到端用例需要 iverilog/vvp，
缺失时自动跳过。
"""

import json
import shutil

import pytest

from rtl_lab.cli import main
from rtl_lab.errors import InputError
from rtl_lab.report import (
    VERIFICATION_SEEDS_SCHEMA_VERSION,
    build_verification_report,
)
from rtl_lab.verification import (
    CoverageConfig,
    TestSpec,
    VerifyConfig,
    verify,
)

needs_icarus = pytest.mark.skipif(
    shutil.which("iverilog") is None or shutil.which("vvp") is None,
    reason="需要安装 Icarus Verilog",
)

DESIGN = "module d; endmodule\n"

TB_PASS = """
`timescale 1ns/1ps
module tb_pass;
  initial begin
    $display("ASSERT PASS a1");
    $display("COVER c1");
    #5 $finish;
  end
endmodule
"""

# 仅在 SEED=2 时断言失败：断言失败应继续后续种子。
TB_SEED_ASSERT = """
`timescale 1ns/1ps
module tb_seedassert;
  integer seed;
  initial begin
    if (!$value$plusargs("SEED=%d", seed)) seed = 0;
    if (seed == 2) $display("ASSERT FAIL ax boom");
    else $display("ASSERT PASS ax");
    $display("COVER cx");
    #5 $finish;
  end
endmodule
"""

# 仅在 SEED=2 时仿真异常退出：应停止该台后续种子。
TB_SEED_FATAL = """
`timescale 1ns/1ps
module tb_seedfatal;
  integer seed;
  initial begin
    if (!$value$plusargs("SEED=%d", seed)) seed = 0;
    $display("ASSERT PASS ay");
    $display("COVER cy");
    if (seed == 2) #1 $fatal(1, "boom");
    #5 $finish;
  end
endmodule
"""

TB_COMP_FAIL = """
`timescale 1ns/1ps
module tb_cfail; ghost_mod g(); initial #1 $finish; endmodule
"""

# 只有断言、没有覆盖率。
TB_NO_COVER = """
`timescale 1ns/1ps
module tb_nocov;
  initial begin
    $display("ASSERT PASS only");
    #5 $finish;
  end
endmodule
"""

# 只有覆盖率、没有断言。
TB_NO_ASSERT = """
`timescale 1ns/1ps
module tb_noassert;
  initial begin
    $display("COVER c5");
    #5 $finish;
  end
endmodule
"""


@pytest.fixture
def project(tmp_path):
    src = tmp_path / "d.v"
    src.write_text(DESIGN)

    def write(name, text):
        p = tmp_path / name
        p.write_text(text)
        return str(p)

    tbs = {
        "pass": write("tb_pass.v", TB_PASS),
        "seedassert": write("tb_seedassert.v", TB_SEED_ASSERT),
        "seedfatal": write("tb_seedfatal.v", TB_SEED_FATAL),
        "cfail": write("tb_cfail.v", TB_COMP_FAIL),
        "nocov": write("tb_nocov.v", TB_NO_COVER),
        "noassert": write("tb_noassert.v", TB_NO_ASSERT),
    }
    return tmp_path, str(src), tbs


def _cfg(project, tb_names, *, seeds=(1, 2), run_id="rid", skip=(),
         optional=(), jobs=1, report=True, baseline=None):
    tmp_path, src, tbs = project
    specs = [
        TestSpec(
            testbench=tbs[n],
            required=n not in optional,
            skip=n in skip,
        )
        for n in tb_names
    ]
    report_path = str(tmp_path / "report.json") if report else None
    return VerifyConfig(
        sources=[src],
        testbenches=specs,
        run_id=run_id,
        duration="100ns",
        coverage=CoverageConfig(),
        workdir=str(tmp_path / "work"),
        report_path=report_path,
        jobs=jobs,
        baseline_path=baseline,
        seeds=list(seeds),
    )


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _by_name(entries):
    return {e["name"]: e for e in entries}


# ---------------- 配置校验（不调用仿真器） ----------------


def _plain_cfg(tmp_path, **kwargs):
    src = tmp_path / "d.v"
    src.write_text(DESIGN)
    tb = tmp_path / "tb.v"
    tb.write_text("module tb; endmodule\n")
    params = dict(
        sources=[str(src)],
        testbenches=[TestSpec(testbench=str(tb))],
        run_id="rid",
        duration="100ns",
        coverage=CoverageConfig(),
        workdir=str(tmp_path / "w"),
    )
    params.update(kwargs)
    return VerifyConfig(**params)


@pytest.mark.parametrize("bad", [
    "",          # 完全为空
    "1,,3",      # 中间空值
    " 1, ,2",    # 空白项
    "-1",        # 负数
    "1.5",       # 非整数
    "+1",        # 带符号
    "0x1",       # 非十进制
    "abc",
    "1,1",       # 重复
    " 3 ,3",     # 带空白的重复
])
def test_seeds_invalid_string_rejected(tmp_path, bad):
    with pytest.raises(InputError):
        verify(_plain_cfg(tmp_path, seeds=bad))


@pytest.mark.parametrize("bad", [[-1], [1, -2], [True], [1, 2.0], [1, 1], []])
def test_seeds_invalid_list_rejected(tmp_path, bad):
    with pytest.raises(InputError):
        verify(_plain_cfg(tmp_path, seeds=bad))


def test_seeds_conflict_with_per_tb_seed_rejected(tmp_path):
    src = tmp_path / "d.v"
    src.write_text(DESIGN)
    tb = tmp_path / "tb.v"
    tb.write_text("module tb; endmodule\n")
    cfg = VerifyConfig(
        sources=[str(src)],
        testbenches=[TestSpec(testbench=str(tb), seed=3)],
        run_id="rid",
        duration="100ns",
        coverage=CoverageConfig(),
        workdir=str(tmp_path / "w"),
        seeds=[1, 2],
    )
    with pytest.raises(InputError):
        verify(cfg)


def test_seeds_invalid_does_not_touch_existing_report(tmp_path):
    cfg = _plain_cfg(tmp_path, seeds="1,1",
                     report_path=str(tmp_path / "r.json"))
    marker = tmp_path / "r.json"
    marker.write_text('{"keep": true}', encoding="utf-8")
    with pytest.raises(InputError):
        verify(cfg)
    assert _load(marker) == {"keep": True}


# ---------------- 报告构建（不调用仿真器） ----------------


def _seeded_outcome(name, seeds=(1, 2)):
    runs = [
        {
            "seed": seed,
            "status": "passed",
            "reason": "passed",
            "diagnostics": [],
            "assertions": {"a1": {"status": "passed", "fail_count": 0}},
            "coverage": {"c1": 1},
            "command": {"simulate": ["/usr/bin/vvp", "sim.vvp",
                                     f"+SEED={seed}"]},
        }
        for seed in seeds
    ]
    return {
        "name": name,
        "top": name,
        "testbench_file": f"/w/tb/{name}.v",
        "status": "passed",
        "reason": "passed",
        "required": True,
        "start_time": "2026-01-01T00:00:00+00:00",
        "end_time": "2026-01-01T00:00:01+00:00",
        "diagnostics": [],
        "assertions": {"a1": {"status": "passed", "fail_count": 0}},
        "coverage": {"c1": 2},
        "commands": {
            "compile": ["/usr/bin/iverilog", "-o", "/w/sim.vvp", "/w/d.v"],
            "simulate": ["/usr/bin/vvp", "sim.vvp", "+SEED=1"],
        },
        "seeds": list(seeds),
        "runs": runs,
    }


def test_build_v5_shape_and_schema_version():
    data, result = build_verification_report(
        run_id="rid",
        sources=["/w/d.v"],
        testbench_files=["/w/tb/t1.v"],
        coverage_config={"threshold": 1.0, "points": []},
        duration="100ns",
        compile_command=["/usr/bin/iverilog"],
        simulate_command=["/usr/bin/vvp"],
        outcomes=[_seeded_outcome("t1")],
        generated_at="2026-01-01T00:00:02+00:00",
        workdir="/w",
        multi_seed=True,
    )
    assert result == "passed"
    assert data["schema_version"] == VERIFICATION_SEEDS_SCHEMA_VERSION == 5
    assert data["format"] == "rtl-lab-verification"
    # 顶层字段与 v3 一致。
    assert list(data.keys()) == [
        "schema_version", "format", "run_id", "result", "generated_at",
        "tool", "config", "compilation", "testbenches", "testbench_summary",
        "assertion_summary", "assertions", "coverage_summary", "coverage",
        "skipped_required",
    ]
    tb = data["testbenches"][0]
    assert list(tb.keys()) == [
        "name", "top", "testbench", "status", "reason", "required",
        "start_time", "end_time", "command", "diagnostics", "assertions",
        "coverage", "seeds", "runs",
    ]
    assert tb["seeds"] == [1, 2]
    assert [r["seed"] for r in tb["runs"]] == [1, 2]
    run = tb["runs"][0]
    assert list(run.keys()) == [
        "seed", "status", "reason", "diagnostics", "assertions",
        "coverage", "command",
    ]
    assert run["command"]["simulate"] == ["vvp", "sim.vvp", "+SEED=1"]
    assert run["assertions"] == [
        {"name": "a1", "status": "passed", "fail_count": 0}
    ]
    assert run["coverage"] == [{"name": "c1", "hits": 1}]


def test_build_v3_unchanged_without_multi_seed():
    outcome = _seeded_outcome("t1")
    data, _ = build_verification_report(
        run_id="rid",
        sources=["/w/d.v"],
        testbench_files=["/w/tb/t1.v"],
        coverage_config={"threshold": 1.0, "points": []},
        duration="100ns",
        compile_command=["/usr/bin/iverilog"],
        simulate_command=["/usr/bin/vvp"],
        outcomes=[outcome],
        generated_at="2026-01-01T00:00:02+00:00",
        workdir="/w",
    )
    assert data["schema_version"] == 3
    assert "seeds" not in data["testbenches"][0]
    assert "runs" not in data["testbenches"][0]


# ---------------- 端到端（需要 iverilog/vvp） ----------------


@needs_icarus
def test_seeds_all_pass_schema_v5(project):
    cfg = _cfg(project, ["pass"], seeds=(1, 2, 3))
    report = verify(cfg)
    assert report["schema_version"] == 5
    assert report["result"] == "passed"
    tb = report["testbenches"][0]
    assert tb["status"] == "passed"
    assert tb["reason"] == "passed"
    assert tb["seeds"] == [1, 2, 3]
    # 编译一次：条目级一条编译命令；逐种子仿真命令按种子顺序排列。
    assert tb["command"]["compile"]
    assert [r["seed"] for r in tb["runs"]] == [1, 2, 3]
    for r in tb["runs"]:
        assert r["status"] == "passed"
        assert f"+SEED={r['seed']}" in r["command"]["simulate"]
        assert r["assertions"] == [
            {"name": "a1", "status": "passed", "fail_count": 0}
        ]
        assert r["coverage"] == [{"name": "c1", "hits": 1}]
    # 跨种子汇总：覆盖率 hits 累加，hit_testbenches 按台去重。
    assert tb["coverage"] == [{"name": "c1", "hits": 3}]
    cov = _by_name(report["coverage"])
    assert cov["c1"]["hits"] == 3
    assert cov["c1"]["hit_testbenches"] == 1
    assert report["coverage_summary"]["ratio"] == 1.0
    # 报告落盘。
    assert _load(cfg.report_path)["schema_version"] == 5


@needs_icarus
def test_seeds_assertion_failure_continues_and_aggregates(project):
    cfg = _cfg(project, ["seedassert"], seeds=(1, 2, 3))
    report = verify(cfg)
    assert report["result"] == "failed"
    tb = report["testbenches"][0]
    assert tb["status"] == "failed"
    assert tb["reason"] == "assertion_failed"
    # 断言失败不停止后续种子。
    assert [r["seed"] for r in tb["runs"]] == [1, 2, 3]
    assert [r["status"] for r in tb["runs"]] == ["passed", "failed", "passed"]
    assert tb["runs"][1]["reason"] == "assertion_failed"
    # 断言按名聚合终态并累加 fail_count。
    assert tb["assertions"] == [
        {"name": "ax", "status": "failed", "fail_count": 1}
    ]
    assert report["assertions"] == [
        {"name": "ax", "status": "failed", "fail_count": 1}
    ]
    assert report["assertion_summary"]["failed"] == 1


@needs_icarus
def test_seeds_simulation_failure_stops_remaining_seeds(project):
    cfg = _cfg(project, ["seedfatal"], seeds=(1, 2, 3))
    report = verify(cfg)
    assert report["result"] == "failed"
    tb = report["testbenches"][0]
    assert tb["status"] == "failed"
    assert tb["reason"] == "simulation_failed"
    # 种子 2 仿真异常退出：保留已有结果并停止后续种子。
    assert [r["seed"] for r in tb["runs"]] == [1, 2]
    assert tb["runs"][-1]["reason"] == "simulation_failed"


@needs_icarus
def test_seeds_compilation_failure_empty_runs(project):
    cfg = _cfg(project, ["pass", "cfail"], seeds=(1, 2))
    report = verify(cfg)
    tbs = _by_name(report["testbenches"])
    assert tbs["tb_cfail"]["status"] == "failed"
    assert tbs["tb_cfail"]["reason"] == "compilation_failed"
    assert tbs["tb_cfail"]["runs"] == []
    assert tbs["tb_cfail"]["seeds"] == [1, 2]
    assert tbs["tb_pass"]["status"] == "passed"
    assert report["result"] == "failed"


@needs_icarus
def test_seeds_incomplete_statistics_missing_assertions(project):
    cfg = _cfg(project, ["noassert"], seeds=(1, 2))
    report = verify(cfg)
    tb = report["testbenches"][0]
    assert tb["status"] == "failed"
    assert tb["reason"] == "incomplete_statistics"
    assert [r["reason"] for r in tb["runs"]] == [
        "incomplete_statistics", "incomplete_statistics",
    ]


@needs_icarus
def test_seeds_incomplete_statistics_missing_coverage(project):
    # nocov 只有断言；pass 提供全局覆盖率 => nocov 各种子记为统计缺失。
    cfg = _cfg(project, ["pass", "nocov"], seeds=(1, 2))
    report = verify(cfg)
    tbs = _by_name(report["testbenches"])
    assert tbs["tb_nocov"]["status"] == "failed"
    assert tbs["tb_nocov"]["reason"] == "incomplete_statistics"
    assert [r["reason"] for r in tbs["tb_nocov"]["runs"]] == [
        "incomplete_statistics", "incomplete_statistics",
    ]
    assert tbs["tb_pass"]["status"] == "passed"


@needs_icarus
def test_seeds_no_coverage_results_raises_and_preserves_report(project):
    tmp_path, src, tbs = project
    marker = tmp_path / "report.json"
    marker.write_text('{"keep": true}', encoding="utf-8")
    cfg = VerifyConfig(
        sources=[src],
        testbenches=[TestSpec(testbench=tbs["nocov"])],
        run_id="rid", duration="100ns", coverage=CoverageConfig(),
        workdir=str(tmp_path / "w"), report_path=str(marker),
        seeds=[1, 2],
    )
    with pytest.raises(RuntimeError):
        verify(cfg)
    assert _load(marker) == {"keep": True}


@needs_icarus
def test_seeds_skipped_tb_has_empty_runs(project):
    cfg = _cfg(project, ["pass", "seedassert"], seeds=(1, 2),
               skip=["seedassert"], optional=["seedassert"])
    report = verify(cfg)
    tbs = _by_name(report["testbenches"])
    skipped = tbs["tb_seedassert"]
    assert skipped["status"] == "skipped"
    assert skipped["seeds"] == [1, 2]
    assert skipped["runs"] == []
    assert report["result"] == "passed"


@needs_icarus
def test_seeds_jobs_parallel_preserves_order(project):
    cfg = _cfg(project, ["seedassert", "pass"], seeds=(1, 2), jobs=2)
    report = verify(cfg)
    assert [t["name"] for t in report["testbenches"]] == [
        "tb_seedassert", "tb_pass",
    ]
    for tb in report["testbenches"]:
        assert [r["seed"] for r in tb["runs"]] == [1, 2]


@needs_icarus
def test_seeds_reproducible_structured_results(project):
    cfg1 = _cfg(project, ["seedassert", "pass"], seeds=(1, 2), run_id="R")
    cfg2 = _cfg(project, ["seedassert", "pass"], seeds=(1, 2), run_id="R")
    r1 = verify(cfg1)
    r2 = verify(cfg2)

    def strip(report):
        d = dict(report)
        d.pop("generated_at")
        for t in d["testbenches"]:
            t.pop("start_time")
            t.pop("end_time")
        return json.dumps(d, sort_keys=True, ensure_ascii=False)

    assert strip(r1) == strip(r2)


@needs_icarus
def test_seeds_with_baseline_stays_v5_and_appends_comparison(project):
    base_cfg = _cfg(project, ["pass"], seeds=(1, 2), report=False)
    tmp_path = project[0]
    baseline_path = str(tmp_path / "base.json")
    base_cfg.report_path = baseline_path
    verify(base_cfg)
    assert _load(baseline_path)["schema_version"] == 5

    cfg = _cfg(project, ["pass"], seeds=(1, 2), baseline=baseline_path)
    report = verify(cfg)
    # 多种子 + 基线：schema 保持 v5，comparison 追加在最后。
    assert report["schema_version"] == 5
    assert list(report.keys())[-1] == "comparison"
    assert report["comparison"]["passed"] is True
    assert report["comparison"]["mismatches"] == []
    assert report["result"] == "passed"


@needs_icarus
def test_seeds_baseline_difference_fails(project):
    tmp_path = project[0]
    base_cfg = _cfg(project, ["pass"], seeds=(1, 2), report=False)
    baseline_path = str(tmp_path / "base.json")
    base_cfg.report_path = baseline_path
    verify(base_cfg)

    # 当前运行换成种子敏感的测试台：测试台与断言均有差异。
    cfg = _cfg(project, ["seedassert"], seeds=(1, 2), baseline=baseline_path)
    report = verify(cfg)
    assert report["schema_version"] == 5
    assert report["result"] == "failed"
    assert report["comparison"]["passed"] is False
    kinds = {m["kind"] for m in report["comparison"]["mismatches"]}
    assert "testbench_added" in kinds
    assert "testbench_missing" in kinds


# ---------------- CLI ----------------


def _cli_project(tmp_path):
    src = tmp_path / "d.v"
    src.write_text(DESIGN)
    tb = tmp_path / "tb_pass.v"
    tb.write_text(TB_PASS)
    tbf = tmp_path / "tb_seedassert.v"
    tbf.write_text(TB_SEED_ASSERT)
    return tmp_path, str(src), str(tb), str(tbf)


@needs_icarus
def test_cli_seeds_exit_0_and_v5(tmp_path):
    tmp, src, tb, _ = _cli_project(tmp_path)
    report = tmp / "r.json"
    argv = [
        "verify", src, "--run-id", "r", "--duration", "100ns",
        "--tb", tb, "--seeds", "1,2,3",
        "--workdir", str(tmp / "w"), "--report", str(report),
    ]
    assert main(argv) == 0
    data = _load(report)
    assert data["schema_version"] == 5
    assert data["result"] == "passed"
    tb0 = data["testbenches"][0]
    assert tb0["seeds"] == [1, 2, 3]
    assert [r["seed"] for r in tb0["runs"]] == [1, 2, 3]


@needs_icarus
def test_cli_seeds_leading_zeros_accepted(tmp_path):
    tmp, src, tb, _ = _cli_project(tmp_path)
    report = tmp / "r.json"
    argv = [
        "verify", src, "--run-id", "r", "--duration", "100ns",
        "--tb", tb, "--seeds", "01,2",
        "--workdir", str(tmp / "w"), "--report", str(report),
    ]
    assert main(argv) == 0
    assert _load(report)["testbenches"][0]["seeds"] == [1, 2]


@needs_icarus
def test_cli_seeds_failure_exit_7(tmp_path):
    tmp, src, _tb, tbf = _cli_project(tmp_path)
    report = tmp / "r.json"
    argv = [
        "verify", src, "--run-id", "r", "--duration", "100ns",
        "--tb", tbf, "--seeds", "1,2,3",
        "--workdir", str(tmp / "w"), "--report", str(report),
    ]
    assert main(argv) == 7
    data = _load(report)
    assert data["schema_version"] == 5
    assert data["result"] == "failed"
    assert data["testbenches"][0]["reason"] == "assertion_failed"


@needs_icarus
@pytest.mark.parametrize("bad", ["", "1,,2", "-1", "x", "1,1"])
def test_cli_seeds_invalid_exit_2_no_report(tmp_path, bad):
    tmp, src, tb, _ = _cli_project(tmp_path)
    report = tmp / "r.json"
    argv = [
        "verify", src, "--run-id", "r", "--duration", "100ns",
        "--tb", tb, "--seeds", bad,
        "--workdir", str(tmp / "w"), "--report", str(report),
    ]
    assert main(argv) == 2
    assert not report.exists()


@needs_icarus
def test_cli_seeds_with_tb_seed_exit_2_no_report(tmp_path):
    tmp, src, tb, _ = _cli_project(tmp_path)
    report = tmp / "r.json"
    argv = [
        "verify", src, "--run-id", "r", "--duration", "100ns",
        "--tb", tb, "--seeds", "1,2", "--tb-seed", "tb_pass=3",
        "--workdir", str(tmp / "w"), "--report", str(report),
    ]
    assert main(argv) == 2
    assert not report.exists()


@needs_icarus
def test_cli_seeds_baseline_exit_0_schema_v5(tmp_path):
    tmp, src, tb, _ = _cli_project(tmp_path)
    baseline = tmp / "base.json"
    argv = [
        "verify", src, "--run-id", "r", "--duration", "100ns",
        "--tb", tb, "--seeds", "1,2",
        "--workdir", str(tmp / "w"), "--report", str(baseline),
    ]
    assert main(argv) == 0
    assert _load(baseline)["schema_version"] == 5

    report = tmp / "r.json"
    argv = [
        "verify", src, "--run-id", "r", "--duration", "100ns",
        "--tb", tb, "--seeds", "1,2", "--baseline", str(baseline),
        "--workdir", str(tmp / "w2"), "--report", str(report),
    ]
    assert main(argv) == 0
    data = _load(report)
    assert data["schema_version"] == 5
    assert data["comparison"]["passed"] is True


@needs_icarus
def test_cli_without_seeds_stays_v3(tmp_path):
    tmp, src, tb, _ = _cli_project(tmp_path)
    report = tmp / "r.json"
    argv = [
        "verify", src, "--run-id", "r", "--duration", "100ns",
        "--tb", tb, "--workdir", str(tmp / "w"), "--report", str(report),
    ]
    assert main(argv) == 0
    data = _load(report)
    assert data["schema_version"] == 3
    assert "seeds" not in data["testbenches"][0]
    assert "runs" not in data["testbenches"][0]
