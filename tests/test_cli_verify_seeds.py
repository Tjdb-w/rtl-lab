"""CLI verify --seeds 测试：schema v5、退出码、互斥与基线（需 iverilog/vvp）。"""

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

TB_MATRIX = """
`timescale 1ns/1ps
module tb_matrix;
  integer seed;
  initial begin
    if (!$value$plusargs("SEED=%d", seed)) seed = 0;
    $display("ASSERT PASS a1");
    if (seed == 2) $display("ASSERT FAIL a1 boom");
    if (seed == 5) $fatal(1, "simboom");
    $display("COVER c1");
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
    tbm = tmp_path / "tb_matrix.v"
    tbm.write_text(TB_MATRIX)
    return tmp_path, src, tb, tbm


def _argv(files, tb, seeds, *, report=True, baseline=None, extra=()):
    tmp, src, _, _ = files
    argv = [
        "verify", str(src), "--run-id", "rid", "--duration", "100ns",
        "--tb", str(tb), "--seeds", seeds,
        "--workdir", str(tmp / "w"),
    ]
    if report:
        argv += ["--report", str(tmp / "r.json")]
    if baseline is not None:
        argv += ["--baseline", str(baseline)]
    argv += list(extra)
    return argv, tmp / "r.json"


def test_cli_matrix_pass_exit_0_schema_v5(files):
    _, src, tb, _ = files
    argv, report = _argv(files, tb, "1,2,3")
    assert main(argv) == 0
    data = json.load(open(report))
    assert data["schema_version"] == 5
    assert data["result"] == "passed"
    assert data["testbenches"][0]["seeds"] == [1, 2, 3]


def test_cli_matrix_leading_zeros(files):
    _, _, tb, _ = files
    argv, report = _argv(files, tb, "00,007")
    assert main(argv) == 0
    data = json.load(open(report))
    assert data["testbenches"][0]["seeds"] == [0, 7]


def test_cli_matrix_failure_exit_7_report_written(files):
    _, _, _, tbm = files
    argv, report = _argv(files, tbm, "1,2")
    assert main(argv) == 7
    data = json.load(open(report))
    assert data["schema_version"] == 5
    assert data["result"] == "failed"


def test_cli_matrix_simulation_failure_exit_7_stops_seeds(files):
    _, _, _, tbm = files
    argv, report = _argv(files, tbm, "1,2,3,5,9")
    assert main(argv) == 7
    tb = json.load(open(report))["testbenches"][0]
    # 种子 5 非零退出后停止：9 不执行。
    assert [r["seed"] for r in tb["runs"]] == [1, 2, 3, 5]
    assert tb["seeds"] == [1, 2, 3, 5, 9]


@pytest.mark.parametrize("bad", ["1,1", "-1", "1,,2", "abc", "1.5", ",1"])
def test_cli_matrix_bad_seeds_exit_2_no_report(files, bad):
    _, _, tb, _ = files
    argv, report = _argv(files, tb, bad)
    assert main(argv) == 2
    assert not report.exists()


def test_cli_seeds_and_tb_seed_conflict_exit_2_no_report(files):
    _, _, tb, _ = files
    argv, report = _argv(
        files, tb, "1,2", extra=["--tb-seed", "tb_pass=9"]
    )
    assert main(argv) == 2
    assert not report.exists()


def test_cli_tb_seed_without_seeds_flag_unchanged(files):
    # 不带 --seeds 时 --tb-seed 仍正常工作，报告为 schema v3。
    tmp, src, tb, _ = files
    argv = [
        "verify", str(src), "--run-id", "rid", "--duration", "100ns",
        "--tb", str(tb), "--tb-seed", "tb_pass=9",
        "--workdir", str(tmp / "w"), "--report", str(tmp / "r.json"),
    ]
    assert main(argv) == 0
    data = json.load(open(tmp / "r.json"))
    assert data["schema_version"] == 3
    assert "+SEED=9" in data["testbenches"][0]["command"]["simulate"]


def test_cli_matrix_baseline_no_diff_exit_0(files, capsys):
    _, _, tb, _ = files
    argv1, _ = _argv(files, tb, "1,2")
    tmp = files[0]
    baseline = tmp / "base.json"
    argv1[-1] = str(baseline)  # 替换 --report 目标
    assert main(argv1) == 0
    argv2, report = _argv(files, tb, "1,2", baseline=baseline)
    assert main(argv2) == 0
    data = json.load(open(report))
    # v5 不因 baseline 升版，comparison 追加在最后。
    assert data["schema_version"] == 5
    assert list(data.keys())[-1] == "comparison"
    assert data["comparison"]["passed"] is True
    assert "基线差异 0 条" in capsys.readouterr().err


def test_cli_matrix_baseline_diff_exit_7(files):
    _, _, tb, tbm = files
    tmp = files[0]
    baseline = tmp / "base.json"
    argv_base, _ = _argv(files, tb, "1,2")
    argv_base[-1] = str(baseline)
    assert main(argv_base) == 0
    argv_cmp, report = _argv(files, tbm, "1,2", baseline=baseline)
    assert main(argv_cmp) == 7
    data = json.load(open(report))
    assert data["schema_version"] == 5
    assert data["comparison"]["passed"] is False
    assert data["comparison"]["mismatches"]


def test_cli_matrix_invalid_baseline_exit_2_no_report(files):
    _, _, tb, _ = files
    tmp = files[0]
    argv, report = _argv(files, tb, "1,2", baseline=tmp / "nope.json")
    assert main(argv) == 2
    assert not report.exists()


def test_cli_matrix_jobs_parallel_order_stable(files):
    _, src, tb, tbm = files
    tmp = files[0]
    argv = [
        "verify", str(src), "--run-id", "rid", "--duration", "100ns",
        "--tb", str(tbm), "--tb", str(tb), "--seeds", "3,1,2",
        "--jobs", "4",
        "--workdir", str(tmp / "w"), "--report", str(tmp / "r.json"),
    ]
    assert main(argv) == 7
    data = json.load(open(tmp / "r.json"))
    assert [t["name"] for t in data["testbenches"]] == [
        "tb_matrix", "tb_pass"
    ]
    assert [r["seed"] for r in data["testbenches"][0]["runs"]] == [3, 1, 2]


def test_cli_without_seeds_stays_v3(files):
    tmp, src, tb, _ = files
    argv = [
        "verify", str(src), "--run-id", "rid", "--duration", "100ns",
        "--tb", str(tb),
        "--workdir", str(tmp / "w"), "--report", str(tmp / "r.json"),
    ]
    assert main(argv) == 0
    data = json.load(open(tmp / "r.json"))
    assert data["schema_version"] == 3
    assert "seeds" not in data["testbenches"][0]
