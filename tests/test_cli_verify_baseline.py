"""CLI verify --baseline 集成测试：schema v4、退出码与差异汇总。"""

import json
import shutil

import pytest

from rtl_lab.cli import main

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

TB_FAIL = """
`timescale 1ns/1ps
module tb_fail;
  initial begin
    $display("ASSERT FAIL ax boom");
    $display("COVER c3");
    #5 $finish;
  end
endmodule
"""


@pytest.fixture
def files(tmp_path):
    src = tmp_path / "d.v"
    src.write_text(DESIGN)
    tb = tmp_path / "tb_pass.v"
    tb.write_text(TB_PASS)
    tbf = tmp_path / "tb_fail.v"
    tbf.write_text(TB_FAIL)
    return tmp_path, src, tb, tbf


def _verify_argv(files, report, *, baseline=None, tb=None):
    tmp, src, tb_pass, tb_fail = files
    argv = [
        "verify", str(src),
        "--run-id", "rid", "--duration", "100ns",
        "--tb", str(tb or tb_pass),
        "--workdir", str(tmp / "w"),
        "--report", str(report),
    ]
    if baseline is not None:
        argv += ["--baseline", str(baseline)]
    return argv


def _make_baseline(files):
    """先跑一次 verify 生成 v3 基线报告。"""
    tmp = files[0]
    baseline = tmp / "base.json"
    assert main(_verify_argv(files, baseline)) == 0
    return baseline


def test_baseline_no_difference_exit_0_schema_v4(files, capsys):
    tmp = files[0]
    baseline = _make_baseline(files)
    report = tmp / "r.json"
    assert main(_verify_argv(files, report, baseline=baseline)) == 0
    data = json.load(open(report))
    assert data["schema_version"] == 4
    assert data["format"] == "rtl-lab-verification"
    assert data["result"] == "passed"
    # v3 字段及顺序之后追加 comparison。
    assert list(data.keys())[-1] == "comparison"
    comparison = data["comparison"]
    assert list(comparison.keys()) == ["baseline", "passed", "mismatches"]
    assert comparison["baseline"] == "base.json"  # 脱敏：工作目录之外只留文件名
    assert comparison["passed"] is True
    assert comparison["mismatches"] == []
    assert "基线差异 0 条" in capsys.readouterr().err


def test_baseline_without_flag_stays_schema_v3(files):
    tmp = files[0]
    report = tmp / "r.json"
    assert main(_verify_argv(files, report)) == 0
    data = json.load(open(report))
    assert data["schema_version"] == 3
    assert "comparison" not in data


def test_baseline_difference_exit_7_and_mismatches(files, capsys):
    tmp, _src, _tb, tb_fail = files
    baseline = _make_baseline(files)
    report = tmp / "r.json"
    # 当前运行换成失败测试台：测试台与断言均有差异。
    argv = _verify_argv(files, report, baseline=baseline, tb=tb_fail)
    assert main(argv) == 7
    data = json.load(open(report))
    assert data["schema_version"] == 4
    assert data["result"] == "failed"
    comparison = data["comparison"]
    assert comparison["passed"] is False
    kinds = {(m["kind"], m["name"]) for m in comparison["mismatches"]}
    assert ("testbench_added", "tb_fail") in kinds
    assert ("testbench_missing", "tb_pass") in kinds
    assert ("assertion_added", "ax") in kinds
    assert ("assertion_missing", "a1") in kinds
    for m in comparison["mismatches"]:
        assert list(m.keys()) == ["kind", "name", "expected", "actual"]
    err = capsys.readouterr().err
    assert f"基线差异 {len(comparison['mismatches'])} 条" in err


def test_baseline_missing_file_exit_2_no_report(files):
    tmp = files[0]
    report = tmp / "r.json"
    argv = _verify_argv(files, report, baseline=tmp / "nope.json")
    assert main(argv) == 2
    assert not report.exists()


def test_baseline_invalid_json_exit_2_no_report(files):
    tmp = files[0]
    bad = tmp / "bad.json"
    bad.write_text("{oops", encoding="utf-8")
    report = tmp / "r.json"
    argv = _verify_argv(files, report, baseline=bad)
    assert main(argv) == 2
    assert not report.exists()


def test_baseline_not_verification_report_exit_2(files):
    tmp = files[0]
    other = tmp / "run.json"
    other.write_text(json.dumps({"schema_version": 1, "tool": "x"}),
                     encoding="utf-8")
    report = tmp / "r.json"
    argv = _verify_argv(files, report, baseline=other)
    assert main(argv) == 2
    assert not report.exists()


def test_baseline_unsupported_version_exit_2(files):
    tmp = files[0]
    future = tmp / "future.json"
    future.write_text(json.dumps({
        "schema_version": 99, "format": "rtl-lab-verification",
    }), encoding="utf-8")
    report = tmp / "r.json"
    argv = _verify_argv(files, report, baseline=future)
    assert main(argv) == 2
    assert not report.exists()


def test_baseline_invalid_keeps_existing_report(files):
    tmp = files[0]
    report = tmp / "r.json"
    report.write_text('{"keep": true}\n', encoding="utf-8")
    argv = _verify_argv(files, report, baseline=tmp / "nope.json")
    assert main(argv) == 2
    assert json.load(open(report)) == {"keep": True}


def test_baseline_v4_report_can_serve_as_baseline(files):
    """带 comparison 的 v4 报告同样可作为下次对比的基线。"""
    tmp = files[0]
    baseline = _make_baseline(files)
    mid = tmp / "mid.json"
    assert main(_verify_argv(files, mid, baseline=baseline)) == 0
    report = tmp / "r.json"
    assert main(_verify_argv(files, report, baseline=mid)) == 0
    data = json.load(open(report))
    assert data["schema_version"] == 4
    assert data["comparison"]["passed"] is True
