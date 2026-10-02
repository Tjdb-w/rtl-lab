"""CLI verify 子命令测试：退出码、报告落盘与参数解析。"""

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

TB_PASS2 = """
`timescale 1ns/1ps
module other_top;
  initial begin
    $display("ASSERT PASS a2");
    $display("COVER c2");
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
    tb2 = tmp_path / "tb_pass2.v"
    tb2.write_text(TB_PASS2)
    tbf = tmp_path / "tb_fail.v"
    tbf.write_text(TB_FAIL)
    return tmp_path, src, tb, tb2, tbf


def _base(files, run_id="rid"):
    tmp, src, tb, tb2, _ = files
    return [
        "verify", str(src),
        "--run-id", run_id, "--duration", "100ns",
        "--tb", str(tb),
        "--workdir", str(tmp / "w"),
        "--report", str(tmp / "r.json"),
    ], tmp / "r.json"


def test_verify_passed_exit_0(files):
    argv, report = _base(files)
    assert main(argv) == 0
    data = json.load(open(report))
    assert data["result"] == "passed"
    assert data["schema_version"] == 3
    assert data["format"] == "rtl-lab-verification"


def test_verify_failed_exit_7(files):
    tmp, src, tb, _tb2, tbf = files
    argv = [
        "verify", str(src), "--run-id", "r", "--duration", "100ns",
        "--tb", str(tb), "--tb", str(tbf),
        "--workdir", str(tmp / "w"), "--report", str(tmp / "r.json"),
    ]
    assert main(argv) == 7
    data = json.load(open(tmp / "r.json"))
    assert data["result"] == "failed"


def test_verify_empty_run_id_exit_8_no_report(files):
    argv, report = _base(files, run_id="   ")
    assert main(argv) == 8
    assert not report.exists()


def test_verify_report_path_is_directory_exit_8(files):
    tmp, src, tb, _tb2, _tbf = files
    argv = [
        "verify", str(src), "--run-id", "r", "--duration", "100ns",
        "--tb", str(tb), "--workdir", str(tmp / "w"),
        "--report", str(tmp),  # 目录
    ]
    assert main(argv) == 8


def test_verify_all_skipped_exit_8(files):
    tmp, src, tb, _tb2, _tbf = files
    argv = [
        "verify", str(src), "--run-id", "r", "--duration", "100ns",
        "--tb", str(tb), "--skip", "tb_pass",
        "--workdir", str(tmp / "w"), "--report", str(tmp / "r.json"),
    ]
    assert main(argv) == 8
    assert not (tmp / "r.json").exists()


def test_verify_skipped_optional_passes_exit_0(files):
    tmp, src, tb, tb2, _tbf = files
    argv = [
        "verify", str(src), "--run-id", "r", "--duration", "100ns",
        "--tb", str(tb), "--tb", f"{tb2}@other_top@other_top",
        "--skip", "other_top", "--optional", "other_top",
        "--workdir", str(tmp / "w"), "--report", str(tmp / "r.json"),
    ]
    assert main(argv) == 0
    data = json.load(open(tmp / "r.json"))
    assert data["result"] == "passed"
    assert data["skipped_required"] == []


def test_verify_tb_spec_with_explicit_top_and_name(files):
    tmp, src, _tb, tb2, _tbf = files
    argv = [
        "verify", str(src), "--run-id", "r", "--duration", "100ns",
        "--tb", f"{tb2}@other_top@friendly",
        "--workdir", str(tmp / "w"), "--report", str(tmp / "r.json"),
    ]
    assert main(argv) == 0
    data = json.load(open(tmp / "r.json"))
    entry = data["testbenches"][0]
    assert entry["name"] == "friendly"
    assert entry["top"] == "other_top"


def test_verify_coverage_below_threshold_exit_7(files):
    argv, report = _base(files)
    argv += ["--cover", "c1", "--cover", "missing",
             "--coverage-threshold", "1.0"]
    assert main(argv) == 7
    data = json.load(open(report))
    assert data["coverage_summary"]["unavailable_points"] == ["missing"]


def test_verify_bad_tb_spec_exit_2(files):
    argv = [
        "verify", str(files[1]), "--run-id", "r", "--duration", "100ns",
        "--tb", "@nope", "--workdir", str(files[0] / "w"),
    ]
    assert main(argv) == 2


def test_verify_bad_threshold_exit_2(files):
    argv, _ = _base(files)
    argv += ["--coverage-threshold", "2.0"]
    assert main(argv) == 2


def test_verify_bad_tb_seed_format_exit_2(files):
    argv, _ = _base(files)
    argv += ["--tb-seed", "no-equal-sign"]
    assert main(argv) == 2


def test_verify_jobs_concurrent_order_stable(files):
    tmp, src, tb, tb2, tbf = files
    argv = [
        "verify", str(src), "--run-id", "r", "--duration", "100ns",
        "--tb", f"{tb2}@other_top@other_top", "--tb", str(tb),
        "--tb", str(tbf),
        "--jobs", "3",
        "--workdir", str(tmp / "w"), "--report", str(tmp / "r.json"),
    ]
    assert main(argv) == 7
    data = json.load(open(tmp / "r.json"))
    # 结果顺序恒为 CLI 选择顺序，与并发完成顺序无关。
    assert [t["name"] for t in data["testbenches"]] == [
        "other_top", "tb_pass", "tb_fail"
    ]


# ---------------- --baseline 基线对比 ----------------

def _verify_argv(files, report, *, tb=None, baseline=None, run_id="r"):
    tmp, src, tb_default, _tb2, _tbf = files
    argv = [
        "verify", str(src), "--run-id", run_id, "--duration", "100ns",
        "--tb", str(tb if tb is not None else tb_default),
        "--workdir", str(tmp / "w"), "--report", str(report),
    ]
    if baseline is not None:
        argv += ["--baseline", str(baseline)]
    return argv


def test_verify_without_baseline_stays_schema_v3(files):
    tmp = files[0]
    report = tmp / "v3.json"
    assert main(_verify_argv(files, report)) == 0
    data = json.load(open(report))
    assert data["schema_version"] == 3
    assert "comparison" not in data


def test_verify_baseline_identical_exit_0_v4(files, capsys):
    tmp = files[0]
    base_report = tmp / "base.json"
    assert main(_verify_argv(files, base_report, run_id="base")) == 0
    cur_report = tmp / "cur.json"
    argv = _verify_argv(files, cur_report, baseline=base_report,
                        run_id="cur")
    assert main(argv) == 0
    data = json.load(open(cur_report))
    assert data["schema_version"] == 4
    assert data["format"] == "rtl-lab-verification"
    assert data["result"] == "passed"
    assert list(data.keys())[-1] == "comparison"
    assert data["comparison"] == {
        "baseline": "base.json", "passed": True, "mismatches": [],
    }
    err = capsys.readouterr().err
    assert "基线差异 0 条" in err


def test_verify_baseline_mismatch_exit_7_and_counts(files, capsys):
    tmp, src, tb, _tb2, tbf = files
    base_report = tmp / "base.json"
    assert main(_verify_argv(files, base_report, tb=tb, run_id="base")) == 0
    cur_report = tmp / "cur.json"
    argv = [
        "verify", str(src), "--run-id", "cur", "--duration", "100ns",
        "--tb", str(tb), "--tb", str(tbf),
        "--workdir", str(tmp / "w2"), "--report", str(cur_report),
        "--baseline", str(base_report),
    ]
    assert main(argv) == 7
    data = json.load(open(cur_report))
    assert data["schema_version"] == 4
    assert data["result"] == "failed"
    cmp_ = data["comparison"]
    assert cmp_["passed"] is False
    kinds = {(m["kind"], m["name"]) for m in cmp_["mismatches"]}
    assert ("testbench.added", "tb_fail") in kinds
    assert ("assertion.added", "ax") in kinds
    assert ("coverage.added", "c3") in kinds
    # 每条差异固定四字段。
    for m in cmp_["mismatches"]:
        assert set(m) == {"kind", "name", "expected", "actual"}
    # 汇总行末尾给出差异条数。
    err = capsys.readouterr().err
    assert f"基线差异 {len(cmp_['mismatches'])} 条" in err


def test_verify_baseline_missing_exit_2_no_report(files):
    tmp = files[0]
    report = tmp / "r.json"
    missing = tmp / "no-baseline.json"
    argv = _verify_argv(files, report, baseline=missing)
    assert main(argv) == 2
    assert not report.exists()


def test_verify_baseline_malformed_json_exit_2_preserves_report(files):
    tmp = files[0]
    bad = tmp / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    report = tmp / "r.json"
    report.write_text('{"keep": true}', encoding="utf-8")
    argv = _verify_argv(files, report, baseline=bad)
    assert main(argv) == 2
    assert json.load(open(report)) == {"keep": True}


def test_verify_baseline_wrong_format_exit_2(files):
    tmp = files[0]
    v1 = tmp / "v1.json"
    v1.write_text(json.dumps({"schema_version": 1, "status": "passed"}),
                  encoding="utf-8")
    report = tmp / "r.json"
    assert main(_verify_argv(files, report, baseline=v1)) == 2
    assert not report.exists()


def test_verify_baseline_unsupported_version_exit_2(files):
    tmp = files[0]
    future = tmp / "future.json"
    future.write_text(json.dumps({
        "schema_version": 42, "format": "rtl-lab-verification"
    }), encoding="utf-8")
    report = tmp / "r.json"
    assert main(_verify_argv(files, report, baseline=future)) == 2


def test_verify_precondition_error_with_baseline_exit_8_keeps_report(files):
    # 现有前置条件错误仍返回 8（即便基线本身合法），且保留已有报告。
    tmp = files[0]
    base_report = tmp / "base.json"
    assert main(_verify_argv(files, base_report, run_id="base")) == 0
    report = tmp / "r.json"
    report.write_text('{"keep": true}', encoding="utf-8")
    argv = _verify_argv(files, report, baseline=base_report, run_id="  ")
    assert main(argv) == 8
    assert json.load(open(report)) == {"keep": True}
