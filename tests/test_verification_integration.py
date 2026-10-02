"""verify 统一验证的端到端集成测试：需要 iverilog/vvp，缺失时自动跳过。"""

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

TB_PASS2 = """
`timescale 1ns/1ps
module tb_pass2;
  initial begin
    $display("ASSERT PASS a2");
    $display("COVER c2");
    #5 $finish;
  end
endmodule
"""

TB_ASSERT_FAIL = """
`timescale 1ns/1ps
module tb_afail;
  initial begin
    $display("ASSERT FAIL ax boom");
    $display("COVER c3");
    #5 $finish;
  end
endmodule
"""

TB_SIM_FAIL = """
`timescale 1ns/1ps
module tb_sfail;
  initial begin
    $display("COVER c4");
    #1 $fatal(1, "simboom");
  end
endmodule
"""

TB_COMP_FAIL = """
`timescale 1ns/1ps
module tb_cfail; ghost_mod g(); initial #1 $finish; endmodule
"""

# 只有断言、没有覆盖率：正常结束但覆盖率统计缺失。
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
        "pass2": write("tb_pass2.v", TB_PASS2),
        "afail": write("tb_afail.v", TB_ASSERT_FAIL),
        "sfail": write("tb_sfail.v", TB_SIM_FAIL),
        "cfail": write("tb_cfail.v", TB_COMP_FAIL),
        "nocov": write("tb_nocov.v", TB_NO_COVER),
        "noassert": write("tb_noassert.v", TB_NO_ASSERT),
    }
    return tmp_path, str(src), tbs


def _cfg(project, tb_names, *, run_id="rid", threshold=1.0, points=None,
         skip=(), optional=(), jobs=1, seeds=None, report=True):
    tmp_path, src, tbs = project
    specs = [
        TestSpec(
            testbench=tbs[n],
            required=n not in optional,
            skip=n in skip,
            seed=(seeds or {}).get(n, 0),
        )
        for n in tb_names
    ]
    report_path = str(tmp_path / "report.json") if report else None
    return VerifyConfig(
        sources=[src],
        testbenches=specs,
        run_id=run_id,
        duration="100ns",
        coverage=CoverageConfig(threshold=threshold, points=list(points or [])),
        workdir=str(tmp_path / "work"),
        report_path=report_path,
        jobs=jobs,
    )


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _by_name(entries):
    return {e["name"]: e for e in entries}


def test_all_pass_report_passed(project):
    cfg = _cfg(project, ["pass", "pass2"])
    report = verify(cfg)
    assert report["schema_version"] == 3
    assert report["format"] == "rtl-lab-verification"
    assert report["run_id"] == "rid"
    assert report["result"] == "passed"
    assert report["compilation"]["sources"] == ["d.v"]
    assert [t["name"] for t in report["testbenches"]] == ["tb_pass", "tb_pass2"]
    assert report["testbench_summary"] == {
        "passed": 2, "failed": 0, "skipped": 0
    }
    assert report["assertion_summary"] == {
        "total": 2, "passed": 2, "failed": 0, "fail_count": 0
    }
    cov = _by_name(report["coverage"])
    assert cov["c1"]["status"] == "hit" and cov["c2"]["status"] == "hit"
    assert report["coverage_summary"]["ratio"] == 1.0
    # 报告落盘且为合法 UTF-8 JSON。
    on_disk = _load(cfg.report_path)
    assert on_disk["result"] == "passed"
    assert on_disk["run_id"] == "rid"


def test_assertion_failure_still_returns_full_report_failed(project):
    cfg = _cfg(project, ["pass", "afail"])
    report = verify(cfg)  # 不抛异常
    assert report["result"] == "failed"
    assert report["testbench_summary"] == {
        "passed": 1, "failed": 1, "skipped": 0
    }
    tbs = _by_name(report["testbenches"])
    assert tbs["tb_afail"]["status"] == "failed"
    assert tbs["tb_afail"]["reason"] == "assertion_failed"
    assert report["assertion_summary"]["failed"] == 1


def test_simulation_failure_reason(project):
    cfg = _cfg(project, ["pass", "sfail"])
    report = verify(cfg)
    tbs = _by_name(report["testbenches"])
    assert tbs["tb_sfail"]["reason"] == "simulation_failed"
    assert report["result"] == "failed"


def test_compilation_failure_reason_and_continues_others(project):
    cfg = _cfg(project, ["pass", "cfail", "pass2"])
    report = verify(cfg)
    tbs = _by_name(report["testbenches"])
    assert tbs["tb_cfail"]["reason"] == "compilation_failed"
    assert tbs["tb_cfail"]["command"]["simulate"] == []
    assert tbs["tb_pass"]["status"] == "passed"
    assert tbs["tb_pass2"]["status"] == "passed"
    assert report["result"] == "failed"


def test_incomplete_statistics_missing_coverage_fails_tb(project):
    # nocov 无覆盖率（但有断言）；与有覆盖率的 pass 组合，避免触发全局
    # “没有任何覆盖率结果”，从而可单独观察该台被记为 failed。
    cfg = _cfg(project, ["pass", "nocov"])
    report = verify(cfg)
    tbs = _by_name(report["testbenches"])
    assert tbs["tb_nocov"]["status"] == "failed"
    assert tbs["tb_nocov"]["reason"] == "incomplete_statistics"
    assert tbs["tb_pass"]["status"] == "passed"
    assert report["result"] == "failed"


def test_compilation_failure_without_coverage_still_generates_report(project):
    # 唯一测试台编译硬失败（此时必然无覆盖率）：仍生成完整 failed 报告，
    # 而不是被运行级“没有任何覆盖率结果”拦截。
    tmp_path, src, tbs = project
    report_path = tmp_path / "report.json"
    cfg = VerifyConfig(
        sources=[src],
        testbenches=[TestSpec(testbench=tbs["cfail"])],
        run_id="rid", duration="100ns", coverage=CoverageConfig(),
        workdir=str(tmp_path / "w"), report_path=str(report_path),
    )
    report = verify(cfg)
    assert report["result"] == "failed"
    assert report["testbenches"][0]["reason"] == "compilation_failed"
    assert _load(report_path)["result"] == "failed"


def test_incomplete_statistics_missing_assertions_fails_tb(project):
    cfg = _cfg(project, ["noassert"])
    report = verify(cfg)
    tbs = _by_name(report["testbenches"])
    assert tbs["tb_noassert"]["reason"] == "incomplete_statistics"
    assert report["result"] == "failed"


def test_skipped_required_fails_optional_passes(project):
    cfg = _cfg(project, ["pass", "pass2"], skip=["pass2"])
    report = verify(cfg)
    assert report["result"] == "failed"
    assert report["skipped_required"] == ["tb_pass2"]

    cfg2 = _cfg(project, ["pass", "pass2"], skip=["pass2"], optional=["pass2"])
    report2 = verify(cfg2)
    assert report2["result"] == "passed"
    assert report2["skipped_required"] == []


def test_coverage_threshold_below_fails(project):
    # c1 命中、c2 未出现（点名）-> ratio 0.5 < 1.0。
    cfg = _cfg(project, ["pass"], points=["c1", "c2"])
    report = verify(cfg)
    assert report["coverage_summary"]["ratio"] == 0.5
    assert report["coverage_summary"]["unavailable_points"] == ["c2"]
    assert report["result"] == "failed"


def test_coverage_threshold_partial_pass(project):
    # 两个点都命中，阈值 0.5 即通过。
    cfg = _cfg(project, ["pass", "pass2"], threshold=0.5)
    report = verify(cfg)
    assert report["coverage_summary"]["met"] is True
    assert report["result"] == "passed"


def test_empty_run_id_raises_value_error(project):
    for bad in ("", "   "):
        cfg = _cfg(project, ["pass"], run_id=bad)
        with pytest.raises(ValueError):
            verify(cfg)


def test_unwritable_report_is_directory_raises_oserror(project):
    tmp_path, src, tbs = project
    cfg = VerifyConfig(
        sources=[src], testbenches=[TestSpec(testbench=tbs["pass"])],
        run_id="rid", duration="100ns",
        coverage=CoverageConfig(), workdir=str(tmp_path / "w"),
        report_path=str(tmp_path),  # 已存在的目录
    )
    with pytest.raises(OSError):
        verify(cfg)


def test_all_skipped_raises_runtime_error(project):
    cfg = _cfg(project, ["pass", "pass2"], skip=["pass", "pass2"])
    with pytest.raises(RuntimeError):
        verify(cfg)


def test_no_coverage_results_raises_and_preserves_existing_report(project):
    tmp_path, src, tbs = project
    marker = tmp_path / "report.json"
    marker.write_text('{"keep": true}', encoding="utf-8")
    # 两个测试台都只有断言、没有任何覆盖率 => 全局无覆盖率结果。
    other = tmp_path / "tb_nocov2.v"
    other.write_text(TB_NO_COVER)
    cfg = VerifyConfig(
        sources=[src],
        testbenches=[
            TestSpec(testbench=tbs["nocov"], top="tb_nocov"),
            TestSpec(testbench=str(other), name="nocov2", top="tb_nocov"),
        ],
        run_id="rid", duration="100ns", coverage=CoverageConfig(),
        workdir=str(tmp_path / "w"), report_path=str(marker),
    )
    with pytest.raises(RuntimeError):
        verify(cfg)
    # 已有报告原样保留。
    assert _load(marker) == {"keep": True}


def test_duplicate_tb_name_raises_runtime_error(project):
    _, src, tbs = project
    cfg = VerifyConfig(
        sources=[src],
        testbenches=[
            TestSpec(testbench=tbs["pass"], name="dup"),
            TestSpec(testbench=tbs["pass2"], name="dup"),
        ],
        run_id="rid", duration="100ns", coverage=CoverageConfig(),
        workdir=str(project[0] / "w"),
    )
    with pytest.raises(RuntimeError):
        verify(cfg)


def test_reproducible_body_across_directories(project, tmp_path):
    cfg1 = _cfg(project, ["pass", "afail"], run_id="R")
    r1 = verify(cfg1)

    # 在不同工作目录重新生成同一 run_id 与相同输入。
    other = tmp_path.parent / ("verify_other_" + tmp_path.name)
    other.mkdir(exist_ok=True)
    try:
        src2 = other / "d.v"
        src2.write_text(DESIGN)
        p1 = other / "tb_pass.v"
        p1.write_text(TB_PASS)
        p2 = other / "tb_afail.v"
        p2.write_text(TB_ASSERT_FAIL)
        cfg2 = VerifyConfig(
            sources=[str(src2)],
            testbenches=[
                TestSpec(testbench=str(p1)),
                TestSpec(testbench=str(p2)),
            ],
            run_id="R", duration="100ns", coverage=CoverageConfig(),
            workdir=str(other / "w"), report_path=str(other / "r.json"),
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


def test_jobs_concurrent_preserves_selection_order(project):
    cfg_seq = _cfg(project, ["pass2", "pass", "afail"], jobs=1)
    cfg_par = _cfg(project, ["pass2", "pass", "afail"], jobs=3)
    r_seq = verify(cfg_seq)
    r_par = verify(cfg_par)
    order_seq = [t["name"] for t in r_seq["testbenches"]]
    order_par = [t["name"] for t in r_par["testbenches"]]
    assert order_seq == order_par == ["tb_pass2", "tb_pass", "tb_afail"]
    # 断言/覆盖率顺序也稳定（首次出现顺序）。
    assert [a["name"] for a in r_seq["assertions"]] == \
        [a["name"] for a in r_par["assertions"]]


def test_seed_per_testbench_is_applied(project):
    # 通过 +SEED 不影响结果结构；这里验证各台命令携带其各自种子。
    cfg = _cfg(project, ["pass", "pass2"], seeds={"pass": 7, "pass2": 9})
    report = verify(cfg)
    tbs = _by_name(report["testbenches"])
    assert "+SEED=7" in tbs["tb_pass"]["command"]["simulate"]
    assert "+SEED=9" in tbs["tb_pass2"]["command"]["simulate"]


def test_report_is_report_dict_with_verification_schema(project):
    from rtl_lab.report import Report
    cfg = _cfg(project, ["pass"])
    assert isinstance(verify(cfg), Report)


# ---------------- 不调用仿真器的配置校验 ----------------

def test_coverage_config_threshold_validation():
    with pytest.raises(ValueError):
        CoverageConfig(threshold=1.5)
    with pytest.raises(ValueError):
        CoverageConfig(threshold=-0.1)
    with pytest.raises(ValueError):
        CoverageConfig(threshold=True)
    with pytest.raises(ValueError):
        CoverageConfig(points=["a", "a"])
    with pytest.raises(ValueError):
        CoverageConfig(points=[" ", "b"])


def test_verify_jobs_must_be_positive(project):
    # jobs 校验在配置构造阶段即抛 ValueError。
    with pytest.raises(ValueError):
        _cfg(project, ["pass"], jobs=0)


def test_missing_source_input_error(project):
    _, _src, tbs = project
    cfg = VerifyConfig(
        sources=["/no/such/ghost.v"],
        testbenches=[TestSpec(testbench=tbs["pass"])],
        run_id="rid", duration="100ns", coverage=CoverageConfig(),
        workdir=str(project[0] / "w"),
    )
    with pytest.raises(InputError):
        verify(cfg)
