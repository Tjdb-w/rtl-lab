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
