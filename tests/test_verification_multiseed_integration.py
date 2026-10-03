"""verify 多种子矩阵（schema v5）端到端集成测试：需要 iverilog/vvp。"""

import json
import shutil

import pytest

from rtl_lab.errors import InputError
from rtl_lab.verification import CoverageConfig, TestSpec, VerifyConfig, verify

pytestmark = pytest.mark.skipif(
    shutil.which("iverilog") is None or shutil.which("vvp") is None,
    reason="需要安装 Icarus Verilog",
)

DESIGN = "module d; endmodule\n"

# 读取 +SEED：种子 2 断言失败（可恢复，继续后续种子）；
# 种子 5 仿真进程非零退出（停止该台后续种子）。
TB_MATRIX = """
`timescale 1ns/1ps
module tb_matrix;
  integer seed;
  initial begin
    if (!$value$plusargs("SEED=%d", seed)) seed = 0;
    $display("ASSERT PASS a1");
    if (seed == 2) $display("ASSERT FAIL a1 boom-on-2");
    if (seed == 5) $fatal(1, "simboom");
    $display("COVER c1");
    #5 $finish;
  end
endmodule
"""

TB_PASS = """
`timescale 1ns/1ps
module tb_pass;
  initial begin
    $display("ASSERT PASS p1");
    $display("COVER cp");
    #5 $finish;
  end
endmodule
"""

# 每命中一次 c1（与种子无关），用于跨种子 hits 累加。
TB_COVER_TWICE = """
`timescale 1ns/1ps
module tb_cover2;
  initial begin
    $display("ASSERT PASS k1");
    $display("COVER c1");
    $display("COVER c1");
    #5 $finish;
  end
endmodule
"""

TB_COMP_FAIL = """
`timescale 1ns/1ps
module tb_mcfail; ghost_mod g(); initial #1 $finish; endmodule
"""

TB_NO_ASSERT = """
`timescale 1ns/1ps
module tb_mnoassert;
  initial begin $display("COVER cX"); #5 $finish; end
endmodule
"""

TB_NO_COVER = """
`timescale 1ns/1ps
module tb_mnocov;
  initial begin $display("ASSERT PASS only"); #5 $finish; end
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
        "matrix": write("tb_matrix.v", TB_MATRIX),
        "pass": write("tb_pass.v", TB_PASS),
        "cover2": write("tb_cover2.v", TB_COVER_TWICE),
        "cfail": write("tb_mcfail.v", TB_COMP_FAIL),
        "noassert": write("tb_mnoassert.v", TB_NO_ASSERT),
        "nocov": write("tb_mnocov.v", TB_NO_COVER),
    }
    return tmp_path, str(src), tbs


def _cfg(project, tb_names, seeds, *, run_id="rid", threshold=1.0,
         points=(), skip=(), optional=(), jobs=1, tb_seeds=None,
         baseline=None, report=True):
    tmp_path, src, tbs = project
    specs = [
        TestSpec(
            testbench=tbs[n],
            required=n not in optional,
            skip=n in skip,
            seed=(tb_seeds or {}).get(n, 0),
        )
        for n in tb_names
    ]
    report_path = str(tmp_path / "report.json") if report else None
    return VerifyConfig(
        sources=[src],
        testbenches=specs,
        run_id=run_id,
        duration="100ns",
        coverage=CoverageConfig(threshold=threshold, points=list(points)),
        workdir=str(tmp_path / "work"),
        report_path=report_path,
        jobs=jobs,
        baseline_path=baseline,
        seeds=seeds,
    )


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _by_name(entries):
    return {e["name"]: e for e in entries}


def test_matrix_all_pass_is_schema_v5(project):
    cfg = _cfg(project, ["pass"], [1, 2, 3])
    report = verify(cfg)
    assert report["schema_version"] == 5
    assert report["result"] == "passed"
    tb = report["testbenches"][0]
    assert tb["seeds"] == [1, 2, 3]
    assert [r["seed"] for r in tb["runs"]] == [1, 2, 3]
    for run in tb["runs"]:
        assert list(run.keys()) == [
            "seed", "status", "reason", "diagnostics", "assertions",
            "coverage", "simulate",
        ]
        assert run["status"] == "passed" and run["reason"] == "passed"
        assert f"+SEED={run['seed']}" in run["simulate"]


def test_matrix_compiles_once_per_testbench(project):
    # 同一测试台的三个种子共用一次编译：各 run 的 simulate 指向同一 vvp，
    # 条目级 compile 命令只有一条。
    cfg = _cfg(project, ["pass"], [1, 2, 3])
    report = verify(cfg)
    tb = report["testbenches"][0]
    compiles = tb["command"]["compile"]
    assert sum(1 for tok in compiles if tok.endswith("rtl_lab_sim.vvp")) == 1
    assert len({tuple(r["simulate"][:-1]) for r in tb["runs"]}) == 1


def test_matrix_assertion_failure_continues_later_seeds(project):
    cfg = _cfg(project, ["matrix"], [1, 2, 3])
    report = verify(cfg)
    tb = report["testbenches"][0]
    assert tb["status"] == "failed"
    assert tb["reason"] == "assertion_failed"
    # 断言失败不停止：三个种子全部执行。
    assert [(r["seed"], r["reason"]) for r in tb["runs"]] == [
        (1, "passed"), (2, "assertion_failed"), (3, "passed"),
    ]
    # fail_count 跨种子累加；终态 failed。
    assert tb["assertions"] == [
        {"name": "a1", "status": "failed", "fail_count": 1}
    ]
    assert report["result"] == "failed"
    assert report["assertion_summary"]["fail_count"] == 1


def test_matrix_simulation_failure_stops_later_seeds(project):
    cfg = _cfg(project, ["matrix"], [1, 2, 3, 5, 9])
    report = verify(cfg)
    tb = report["testbenches"][0]
    # 种子 5 进程非零退出：停止后续（9 不执行），runs 为列表前缀。
    assert [r["seed"] for r in tb["runs"]] == [1, 2, 3, 5]
    assert tb["seeds"] == [1, 2, 3, 5, 9]
    assert tb["runs"][-1]["reason"] == "simulation_failed"
    assert tb["reason"] == "simulation_failed"
    assert report["result"] == "failed"


def test_matrix_compilation_failed_empty_runs(project):
    cfg = _cfg(project, ["cfail"], [1, 2, 3])
    report = verify(cfg)
    tb = report["testbenches"][0]
    assert tb["reason"] == "compilation_failed"
    assert tb["seeds"] == [1, 2, 3]
    assert tb["runs"] == []
    assert tb["command"]["simulate"] == []
    assert report["schema_version"] == 5
    assert report["result"] == "failed"
    # 编译失败仍落盘完整报告。
    assert _load(cfg.report_path)["result"] == "failed"


def test_matrix_missing_assertions_is_incomplete_statistics(project):
    cfg = _cfg(project, ["noassert"], [1, 2])
    report = verify(cfg)
    tb = report["testbenches"][0]
    assert tb["reason"] == "incomplete_statistics"
    assert [r["reason"] for r in tb["runs"]] == [
        "incomplete_statistics", "incomplete_statistics"
    ]
    assert report["result"] == "failed"


def test_matrix_missing_coverage_on_one_tb_run_level(project):
    # pass 台有覆盖率；nocov 台各种子正常结束但无覆盖率：
    # 该台 runs 记 incomplete_statistics（运行级两级裁决下沉到 run）。
    cfg = _cfg(project, ["pass", "nocov"], [1, 2])
    report = verify(cfg)
    tbs = _by_name(report["testbenches"])
    assert tbs["tb_pass"]["status"] == "passed"
    nocov = tbs["tb_mnocov"]
    assert nocov["reason"] == "incomplete_statistics"
    assert {r["reason"] for r in nocov["runs"]} == {"incomplete_statistics"}
    assert report["result"] == "failed"


def test_matrix_global_no_coverage_raises_and_preserves_report(project):
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


def test_matrix_jobs_parallel_preserves_selection_and_seed_order(project):
    cfg = _cfg(project, ["matrix", "pass"], [3, 1, 2], jobs=4)
    report = verify(cfg)
    assert [t["name"] for t in report["testbenches"]] == [
        "tb_matrix", "tb_pass"
    ]
    matrix, passed = report["testbenches"]
    # 同台种子严格按列表顺序串行，与 jobs 无关。
    assert [r["seed"] for r in matrix["runs"]] == [3, 1, 2]
    assert [r["seed"] for r in passed["runs"]] == [3, 1, 2]


def test_matrix_coverage_hits_sum_hit_testbenches_deduped(project):
    # cover2 台每个种子命中 c1 两次，共 3 个种子 => hits=6，hit_testbenches=1。
    cfg = _cfg(project, ["cover2"], [1, 2, 3])
    report = verify(cfg)
    by = _by_name(report["coverage"])
    assert by["c1"]["hits"] == 6
    assert by["c1"]["hit_testbenches"] == 1
    # 再加一个也命中 c1 的 matrix 台（种子 1 通过、有覆盖）=> 命中台数 2。
    cfg2 = _cfg(project, ["cover2", "matrix"], [1])
    report2 = verify(cfg2)
    by2 = _by_name(report2["coverage"])
    # cover2 hits=2，matrix seed1 命中 c1 一次 => 总 hits=3，2 台命中。
    assert by2["c1"]["hits"] == 3
    assert by2["c1"]["hit_testbenches"] == 2


def test_matrix_skipped_required_fails_optional_passes(project):
    cfg = _cfg(project, ["pass"], [1, 2], skip=["pass"])
    with pytest.raises(RuntimeError):
        verify(cfg)  # 唯一台被跳过 => 没有可执行测试台（退出 8）

    cfg2 = _cfg(
        project, ["pass", "nocov"], [1, 2],
        skip=["nocov"], optional=["nocov"],
    )
    report = verify(cfg2)
    assert report["result"] == "passed"
    skipped = _by_name(report["testbenches"])["tb_mnocov"]
    assert skipped["status"] == "skipped"
    assert skipped["seeds"] == [1, 2]
    assert skipped["runs"] == []
    assert report["skipped_required"] == []


def test_matrix_string_seeds_accepted_with_leading_zeros(project):
    cfg = _cfg(project, ["pass"], "01,002, 3 ")
    report = verify(cfg)
    assert report["testbenches"][0]["seeds"] == [1, 2, 3]
    assert report["result"] == "passed"


@pytest.mark.parametrize("bad", [
    "1,1", "", "1,,2", "-1", "1.0", "abc", "+1", "0x1",
])
def test_matrix_bad_seeds_input_error(project, bad):
    with pytest.raises(InputError):
        _cfg(project, ["pass"], bad)


@pytest.mark.parametrize("bad", [[1, 1], [-1], [True], []])
def test_matrix_bad_seed_list_input_error(project, bad):
    with pytest.raises(InputError):
        _cfg(project, ["pass"], bad)


def test_matrix_seeds_conflict_with_per_tb_seed(project):
    cfg = _cfg(project, ["pass"], [1, 2], tb_seeds={"pass": 7})
    with pytest.raises(InputError):
        verify(cfg)


def test_matrix_reproducible_body(project, tmp_path):
    r1 = verify(_cfg(project, ["matrix", "pass"], [2, 1]))

    other = tmp_path.parent / ("v5_other_" + tmp_path.name)
    other.mkdir(exist_ok=True)
    try:
        (other / "d.v").write_text(DESIGN)
        (other / "tb_matrix.v").write_text(TB_MATRIX)
        (other / "tb_pass.v").write_text(TB_PASS)
        cfg2 = VerifyConfig(
            sources=[str(other / "d.v")],
            testbenches=[
                TestSpec(testbench=str(other / "tb_matrix.v")),
                TestSpec(testbench=str(other / "tb_pass.v")),
            ],
            run_id="rid", duration="100ns", coverage=CoverageConfig(),
            workdir=str(other / "w"), report_path=str(other / "r.json"),
            seeds=[2, 1],
        )
        r2 = verify(cfg2)
    finally:
        shutil.rmtree(other, ignore_errors=True)

    def strip(report):
        d = dict(report)
        d.pop("generated_at")
        for t in d["testbenches"]:
            t.pop("start_time")
            t.pop("end_time")
        return json.dumps(d, sort_keys=True, ensure_ascii=False)

    assert strip(r1) == strip(r2)


def test_no_seeds_stays_schema_v3(project):
    cfg = _cfg(project, ["pass"], None)
    report = verify(cfg)
    assert report["schema_version"] == 3
    assert "seeds" not in report["testbenches"][0]
    assert "runs" not in report["testbenches"][0]
