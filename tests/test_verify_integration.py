"""verify 统一验证入口的端到端集成测试：需要 iverilog/vvp。"""

import copy
import json
import shutil

import pytest

from rtl_lab.cli import main
from rtl_lab.verify import (
    CoverageConfig,
    TestSpec,
    VerifyConfig,
    verify,
)

pytestmark = pytest.mark.skipif(
    shutil.which("iverilog") is None or shutil.which("vvp") is None,
    reason="需要安装 Icarus Verilog",
)

DESIGN = """
module counter(input clk, input rst, output reg [3:0] q);
  always @(posedge clk or posedge rst)
    if (rst) q <= 4'd0; else q <= q + 4'd1;
endmodule
"""

TB_OK = """
`timescale 1ns/1ps
module tb;
  initial begin
    $display("ASSERT PASS a_{tag}");
    $display("COVER c_common");
    $display("COVER c_{tag}");
    #5 $finish;
  end
endmodule
"""

TB_FAIL_ASSERT = """
`timescale 1ns/1ps
module tb;
  initial begin
    $display("ASSERT PASS a_keep");
    #1 $display("ASSERT FAIL a_keep expected 1 got 0");
    $display("COVER c_common");
    #5 $finish;
  end
endmodule
"""

TB_FATAL = """
`timescale 1ns/1ps
module tb;
  initial #3 $fatal(1, "fatalboom");
endmodule
"""

TB_NO_COVER = """
`timescale 1ns/1ps
module tb;
  initial begin
    $display("ASSERT PASS a_silent");
    #5 $finish;
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
    src.write_text(DESIGN)
    return tmp_path, src


def _write_tb(tmp_path, name, text, tag=None):
    path = tmp_path / f"{name}.v"
    path.write_text(text.replace("{tag}", tag or name))
    return path


def _spec(path, name, **kw):
    return TestSpec(str(path), name=name, **kw)


def _cfg(project, specs, *, run_id="run-1", coverage=None,
         report="report.json", duration="100ns"):
    tmp_path, src = project
    return VerifyConfig(
        sources=[str(src)],
        testbenches=specs,
        default_top="tb",
        duration=duration,
        run_id=run_id,
        coverage=coverage or CoverageConfig(),
        workdir=str(tmp_path / "work"),
        report_path=str(tmp_path / report) if report else None,
    )


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _strip_ts(data):
    data = copy.deepcopy(data)
    for tb in data["testbenches"]:
        tb["started_at"] = None
        tb["ended_at"] = None
    return data


# ---------------------------------------------------------------------------
# 通过 / 失败 / 编译失败
# ---------------------------------------------------------------------------

def test_all_passed_report_shape(project):
    tmp_path, src = project
    tb1 = _write_tb(tmp_path, "tb_alpha", TB_OK)
    tb2 = _write_tb(tmp_path, "tb_beta", TB_OK)
    cfg = _cfg(project, [_spec(tb1, "alpha"), _spec(tb2, "beta")])
    report = verify(cfg)
    assert report["schema_version"] == 3
    assert report["run_id"] == "run-1"
    assert report["conclusion"] == "passed"
    assert [t["name"] for t in report["testbenches"]] == ["alpha", "beta"]
    assert report["totals"] == {"total": 2, "passed": 2, "failed": 0,
                                 "skipped": 0}
    # 每个测试台独立编译：实际编译文件含两个 tb。
    files = report["compiled_files"]["files"]
    assert "tb_alpha.v" in files and "tb_beta.v" in files
    assert report["compiled_files"]["design_sources"] == ["counter.v"]
    # 覆盖率跨测试台聚合：c_common 命中 2 次，两个私有点各 1 次。
    cov = report["coverage_summary"]
    assert cov["points_total"] == 3 and cov["points_hit"] == 3
    assert report["assertions"]["total"] == 2
    on_disk = _load(cfg.report_path)
    assert on_disk["conclusion"] == "passed"


def test_spec_order_is_report_order_regardless_of_names(project):
    tmp_path, _ = project
    # 文件名与显式名称均不按字典序，报告仍按配置顺序。
    tbs = [_write_tb(tmp_path, f"tb_{n}", TB_OK) for n in ("zzz", "aaa", "mmm")]
    specs = [_spec(tbs[i], n) for i, n in enumerate(("zzz", "aaa", "mmm"))]
    report = verify(_cfg(project, specs))
    assert [t["name"] for t in report["testbenches"]] == ["zzz", "aaa", "mmm"]


def test_assertion_failure_builds_full_failed_report(project):
    tmp_path, _ = project
    tb_bad = _write_tb(tmp_path, "tb_bad", TB_FAIL_ASSERT)
    tb_ok = _write_tb(tmp_path, "tb_ok", TB_OK)
    cfg = _cfg(project, [_spec(tb_bad, "bad"), _spec(tb_ok, "ok")])
    report = verify(cfg)
    assert report["conclusion"] == "failed"
    assert report["totals"]["failed"] == 1 and report["totals"]["passed"] == 1
    bad, ok = report["testbenches"]
    assert bad["status"] == "failed"
    assert bad["end_reason"] == "assertion_failed"
    assert ok["status"] == "passed"
    # 断言失败不影响另一测试台，且断言仍跨台汇总。
    assert report["assertions"]["failed"] == 1
    assert _load(cfg.report_path)["conclusion"] == "failed"


def test_fatal_testbench_does_not_stop_others(project):
    tmp_path, _ = project
    tb_fatal = _write_tb(tmp_path, "tb_fatal", TB_FATAL)
    tb_ok = _write_tb(tmp_path, "tb_ok", TB_OK)
    report = verify(_cfg(project, [_spec(tb_fatal, "fatal"),
                                    _spec(tb_ok, "ok")]))
    assert report["conclusion"] == "failed"
    by_name = {t["name"]: t for t in report["testbenches"]}
    assert by_name["fatal"]["end_reason"] == "simulation_failed"
    assert by_name["ok"]["status"] == "passed"
    assert any("fatalboom" in d for d in by_name["fatal"]["diagnostics"])


def test_compile_failure_keeps_full_report_and_other_tb(project):
    tmp_path, _ = project
    tb_bad = _write_tb(tmp_path, "tb_bad", TB_COMPERR)
    tb_ok = _write_tb(tmp_path, "tb_ok", TB_OK)
    report = verify(_cfg(project, [_spec(tb_bad, "bad"), _spec(tb_ok, "ok")]))
    assert report["conclusion"] == "failed"
    by_name = {t["name"]: t for t in report["testbenches"]}
    assert by_name["bad"]["end_reason"] == "compile_failed"
    assert by_name["ok"]["status"] == "passed"
    # 编译失败的测试台没有仿真命令，但保留编译命令。
    assert report["config_summary"]["compile_commands"]["bad"]
    assert "bad" not in report["config_summary"]["simulate_commands"]


def test_all_testbenches_failing_without_coverage_still_reports(project):
    tmp_path, _ = project
    tb1 = _write_tb(tmp_path, "tb_bad1", TB_COMPERR)
    tb2 = _write_tb(tmp_path, "tb_bad2", TB_COMPERR)
    report_path = tmp_path / "r.json"
    cfg = _cfg(project, [_spec(tb1, "bad1"), _spec(tb2, "bad2")],
               report=str(report_path))
    report = verify(cfg)
    # 全部测试台失败且无覆盖率：仍必须生成完整 failed 报告，而非报错。
    assert report["conclusion"] == "failed"
    assert {t["name"] for t in report["testbenches"]} == {"bad1", "bad2"}
    assert all(t["end_reason"] == "compile_failed"
               for t in report["testbenches"])
    assert report["coverage_summary"]["points_total"] == 0
    assert _load(report_path)["conclusion"] == "failed"


def test_missing_stats_tb_marked_failed(project):
    tmp_path, _ = project
    # 通过退出但既无断言也无覆盖的测试台（另一台提供覆盖率，避免整体
    # 落入“没有任何覆盖率结果”）。
    tb_empty = tmp_path / "tb_empty.v"
    tb_empty.write_text(
        "`timescale 1ns/1ps\nmodule tb; initial #5 $finish; endmodule\n"
    )
    tb_ok = _write_tb(tmp_path, "tb_ok", TB_OK)
    report = verify(_cfg(project, [_spec(tb_empty, "empty"),
                                    _spec(tb_ok, "ok")]))
    by_name = {t["name"]: t for t in report["testbenches"]}
    assert by_name["empty"]["status"] == "failed"
    assert by_name["empty"]["end_reason"] == "missing_stats"
    assert report["conclusion"] == "failed"


# ---------------------------------------------------------------------------
# 跳过
# ---------------------------------------------------------------------------

def test_optional_skip_passes(project):
    tmp_path, _ = project
    tb_wip = _write_tb(tmp_path, "tb_wip", TB_FAIL_ASSERT)
    tb_ok = _write_tb(tmp_path, "tb_ok", TB_OK)
    cfg = _cfg(project, [
        _spec(tb_wip, "wip", skip=True, skip_reason="not ready",
              required=False),
        _spec(tb_ok, "ok"),
    ])
    report = verify(cfg)
    assert report["conclusion"] == "passed"
    wip = report["testbenches"][0]
    assert wip["status"] == "skipped"
    assert wip["skip_reason"] == "not ready"
    assert wip["assertions"] == [] and wip["started_at"] is None
    assert report["totals"]["skipped"] == 1


def test_required_skip_fails(project):
    tmp_path, _ = project
    tb_wip = _write_tb(tmp_path, "tb_wip", TB_OK)
    tb_ok = _write_tb(tmp_path, "tb_ok", TB_OK)
    report = verify(_cfg(project, [
        _spec(tb_wip, "wip", skip=True, skip_reason="blocked"),
        _spec(tb_ok, "ok"),
    ]))
    assert report["conclusion"] == "failed"
    assert report["testbenches"][1]["status"] == "passed"


# ---------------------------------------------------------------------------
# 覆盖率阈值
# ---------------------------------------------------------------------------

def test_threshold_fail_and_pass(project):
    tmp_path, _ = project
    # tb1 实际命中 c_common 与 c_t1；声明全集再含一个永不观测的 c_gap。
    tb1 = _write_tb(tmp_path, "tb1", TB_OK)
    specs = [_spec(tb1, "t1")]

    # 3 个点中 2 个命中（2/3 ≈ 0.667）：阈值 0.5 阈值过但必测点缺、
    # 阈值 0.9 直接失败；无必测点、全集恰为观测点时通过。
    cov = CoverageConfig(
        threshold=0.5, points=["c_common", "c_tb1", "c_gap"],
        required_points=["c_common", "c_gap"],
    )
    report_ok = verify(_cfg(project, specs, coverage=cov))
    assert report_ok["coverage_summary"]["hit_ratio"] == round(2 / 3, 6)
    assert report_ok["coverage_summary"]["missing_points"] == ["c_gap"]
    # 必测点 c_gap 未命中 => 仍 failed。
    assert report_ok["conclusion"] == "failed"
    assert report_ok["coverage_summary"]["missing_required_points"] == [
        "c_gap"
    ]
    # 逐点结果稳定、未观测点 hits=0。
    assert report_ok["coverage_summary"]["points"] == [
        {"name": "c_common", "hits": 1},
        {"name": "c_tb1", "hits": 1},
        {"name": "c_gap", "hits": 0},
    ]

    cov_pass = CoverageConfig(points=["c_common", "c_tb1"])
    report_pass = verify(_cfg(project, specs, coverage=cov_pass))
    assert report_pass["conclusion"] == "passed"

    cov_fail = CoverageConfig(
        threshold=0.9, points=["c_common", "c_tb1", "c_gap"],
    )
    report_fail = verify(_cfg(project, specs, coverage=cov_fail))
    assert report_fail["conclusion"] == "failed"
    assert report_fail["coverage_summary"]["threshold_met"] is False


def test_declared_points_with_zero_observed_still_reports(project):
    tmp_path, _ = project
    # 测试台有断言但不打印任何 COVER；声明了覆盖点全集，不触发
    # “没有任何覆盖率结果”，而是得到 0 覆盖率的 failed 完整报告。
    tb = _write_tb(tmp_path, "tb_silent", TB_NO_COVER)
    cov = CoverageConfig(threshold=1.0, points=["p1", "p2"])
    report = verify(_cfg(project, [_spec(tb, "silent")], coverage=cov))
    assert report["conclusion"] == "failed"
    cs = report["coverage_summary"]
    assert cs["points_total"] == 2 and cs["points_hit"] == 0
    assert cs["hit_ratio"] == 0.0 and cs["threshold_met"] is False
    assert cs["missing_points"] == ["p1", "p2"]


# ---------------------------------------------------------------------------
# 前置校验错误
# ---------------------------------------------------------------------------

def test_empty_run_id_raises_value_error(project):
    tmp_path, src = project
    tb = _write_tb(tmp_path, "tb_ok", TB_OK)
    cfg = VerifyConfig(
        sources=[str(src)], testbenches=[_spec(tb, "t")],
        default_top="tb", duration="100ns", run_id="   ",
        workdir=str(tmp_path / "w"),
        report_path=str(tmp_path / "r.json"),
    )
    with pytest.raises(ValueError):
        verify(cfg)
    assert not (tmp_path / "r.json").exists()


def test_unwritable_output_raises_os_error(project):
    tmp_path, src = project
    tb = _write_tb(tmp_path, "tb_ok", TB_OK)
    cfg = VerifyConfig(
        sources=[str(src)], testbenches=[_spec(tb, "t")],
        default_top="tb", duration="100ns", run_id="r",
        workdir=str(tmp_path / "w"),
        report_path=str(tmp_path / "no-such-dir" / "r.json"),
    )
    with pytest.raises(OSError):
        verify(cfg)
    assert not (tmp_path / "no-such-dir").exists()


def test_no_coverage_result_raises_and_keeps_existing_report(project):
    tmp_path, _ = project
    tb = _write_tb(tmp_path, "tb_silent", TB_NO_COVER)
    report_path = tmp_path / "keep.json"
    report_path.write_text('{"sentinel": true}\n', encoding="utf-8")
    cfg = VerifyConfig(
        # sources 仍需真实存在
        sources=[str(tmp_path / "counter.v")],
        testbenches=[_spec(tb, "silent")],
        default_top="tb", duration="100ns", run_id="r",
        workdir=str(tmp_path / "w"),
        report_path=str(report_path),
    )
    with pytest.raises(RuntimeError, match="没有任何覆盖率结果"):
        verify(cfg)
    # 已有报告原样保留。
    assert _load(report_path) == {"sentinel": True}


def test_all_skipped_raises_and_keeps_existing_report(project):
    tmp_path, _ = project
    tb = _write_tb(tmp_path, "tb_ok", TB_OK)
    report_path = tmp_path / "keep.json"
    report_path.write_text('{"sentinel": 2}\n', encoding="utf-8")
    cfg = VerifyConfig(
        sources=[str(tmp_path / "counter.v")],
        testbenches=[_spec(tb, "t", skip=True, required=False)],
        default_top="tb", duration="100ns", run_id="r",
        workdir=str(tmp_path / "w"),
        report_path=str(report_path),
    )
    with pytest.raises(RuntimeError, match="无可执行测试台"):
        verify(cfg)
    assert _load(report_path) == {"sentinel": 2}


def test_duplicate_tb_names_rejected(project):
    tmp_path, src = project
    tb1 = _write_tb(tmp_path, "tb1", TB_OK)
    tb2 = _write_tb(tmp_path, "tb2", TB_OK)
    cfg = VerifyConfig(
        sources=[str(src)],
        testbenches=[_spec(tb1, "dup"), _spec(tb2, "dup")],
        default_top="tb", duration="100ns", run_id="r",
    )
    with pytest.raises(ValueError, match="名称冲突"):
        verify(cfg)


# ---------------------------------------------------------------------------
# 可复现性
# ---------------------------------------------------------------------------

def test_same_input_same_run_id_reproducible_body(project):
    tmp_path, _ = project
    tb1 = _write_tb(tmp_path, "tb_alpha", TB_OK)
    tb2 = _write_tb(tmp_path, "tb_beta", TB_OK)
    specs = [_spec(tb1, "alpha"), _spec(tb2, "beta")]

    cfg1 = _cfg(project, specs, run_id="fixed-id", report="r1.json")
    r1 = _strip_ts(verify(cfg1))
    cfg2 = _cfg(project, specs, run_id="fixed-id", report="r2.json")
    r2 = _strip_ts(verify(cfg2))
    assert r1 == r2

    # 落盘 JSON 除时间戳外逐字节一致。
    import re
    text1 = (tmp_path / "r1.json").read_text(encoding="utf-8")
    text2 = (tmp_path / "r2.json").read_text(encoding="utf-8")
    ts_re = re.compile(r'"(started_at|ended_at)": "[^"]*"')
    assert ts_re.sub(r'"\1": null', text1) == ts_re.sub(r'"\1": null', text2)


def test_report_does_not_leak_machine_paths(project):
    tmp_path, _ = project
    tb = _write_tb(tmp_path, "tb_ok", TB_OK)
    report = verify(_cfg(project, [_spec(tb, "ok")]))
    blob = json.dumps(report, ensure_ascii=False)
    assert str(tmp_path) not in blob
    assert "/usr/bin" not in blob


def test_report_is_valid_utf8_json(project):
    tmp_path, _ = project
    tb = _write_tb(tmp_path, "tb_ok", TB_OK)
    verify(_cfg(project, [_spec(tb, "ok")]))
    raw = (tmp_path / "report.json").read_bytes()
    json.loads(raw.decode("utf-8"))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli_argv(project, tb_args, *, run_id="r1", threshold=None,
              extra=()):
    tmp_path, src = project
    argv = [
        "verify", str(src), "--run-id", run_id,
        "--top", "tb", "--duration", "100ns",
        "--workdir", str(tmp_path / "w"),
        "--report", str(tmp_path / "cli.json"),
    ]
    for arg in tb_args:
        argv += ["--tb", arg]
    if threshold is not None:
        argv += ["--cov-threshold", str(threshold)]
    argv += list(extra)
    return argv


def test_cli_verify_passed_exit_zero(project, capsys):
    tmp_path, _ = project
    tb = _write_tb(tmp_path, "tb_ok", TB_OK)
    argv = _cli_argv(project, [f"ok={tb}"])
    assert main(argv) == 0
    data = _load(tmp_path / "cli.json")
    assert data["schema_version"] == 3 and data["conclusion"] == "passed"
    captured = capsys.readouterr()
    assert "r1" in captured.err and "通过" in captured.err


def test_cli_verify_failed_exit_six(project):
    tmp_path, _ = project
    tb = _write_tb(tmp_path, "tb_bad", TB_FAIL_ASSERT)
    argv = _cli_argv(project, [str(tb)])  # 无 NAME= 时取文件 stem
    assert main(argv) == 6
    data = _load(tmp_path / "cli.json")
    assert data["conclusion"] == "failed"
    assert data["testbenches"][0]["name"] == "tb_bad"


def test_cli_verify_empty_run_id_exit_two(project):
    tmp_path, src = project
    tb = _write_tb(tmp_path, "tb_ok", TB_OK)
    argv = ["verify", str(src), "--run-id", "", "--tb", str(tb),
            "--top", "tb", "--duration", "100ns"]
    assert main(argv) == 2
    assert not (tmp_path / "cli.json").exists()


def test_cli_verify_unwritable_output_exit_seven(project):
    tmp_path, src = project
    tb = _write_tb(tmp_path, "tb_ok", TB_OK)
    argv = ["verify", str(src), "--run-id", "r", "--tb", str(tb),
            "--top", "tb", "--duration", "100ns",
            "--report", str(tmp_path / "nodir" / "r.json")]
    assert main(argv) == 7


def test_cli_verify_skip_optional_exit_zero(project):
    tmp_path, _ = project
    tb_wip = _write_tb(tmp_path, "tb_wip", TB_FAIL_ASSERT)
    tb_ok = _write_tb(tmp_path, "tb_ok", TB_OK)
    argv = _cli_argv(project, [f"wip={tb_wip}", f"ok={tb_ok}"],
                     extra=["--tb-skip", "wip=work in progress",
                            "--tb-optional", "wip"])
    assert main(argv) == 0
    data = _load(tmp_path / "cli.json")
    by_name = {t["name"]: t for t in data["testbenches"]}
    assert by_name["wip"]["status"] == "skipped"
    assert by_name["wip"]["skip_reason"] == "work in progress"
    assert data["conclusion"] == "passed"


def test_cli_verify_threshold_fail_exit_six(project):
    tmp_path, _ = project
    tb = _write_tb(tmp_path, "tb_ok", TB_OK)
    argv = _cli_argv(project, [f"ok={tb}"], threshold=1.0,
                     extra=["--cov-required", "missing_point"])
    assert main(argv) == 6
    data = _load(tmp_path / "cli.json")
    assert data["coverage_summary"]["missing_required_points"] == [
        "missing_point"
    ]


def test_cli_verify_unknown_tb_reference_exit_two(project):
    tmp_path, _ = project
    tb = _write_tb(tmp_path, "tb_ok", TB_OK)
    argv = _cli_argv(project, [f"ok={tb}"],
                     extra=["--tb-seed", "ghost=3"])
    assert main(argv) == 2
