"""CLI verify --manifest 测试：清单入口、互斥、退出码与报告等价性。"""

import json
import os
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
def project(tmp_path):
    """一个含相对路径清单的项目目录。"""
    (tmp_path / "rtl").mkdir()
    (tmp_path / "tb").mkdir()
    src = tmp_path / "rtl" / "d.v"
    src.write_text(DESIGN)
    tb = tmp_path / "tb" / "tb_pass.v"
    tb.write_text(TB_PASS)
    tb2 = tmp_path / "tb" / "tb_pass2.v"
    tb2.write_text(TB_PASS2)
    tbf = tmp_path / "tb" / "tb_fail.v"
    tbf.write_text(TB_FAIL)
    return tmp_path, src, tb, tb2, tbf


def _write_manifest(project, name="manifest.json", **overrides):
    tmp_path = project[0]
    data = {
        "schema_version": 1,
        "run_id": "rid",
        "duration": "100ns",
        "sources": ["rtl/d.v"],
        "testbenches": [{"testbench": "tb/tb_pass.v"}],
        "coverage": {"threshold": 1.0, "points": []},
        "workdir": "w",
    }
    data.update(overrides)
    path = tmp_path / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _scrub(obj):
    """递归删除报告中允许跨复现变化的时间戳字段。"""
    if isinstance(obj, dict):
        return {
            k: _scrub(v) for k, v in obj.items()
            if k not in ("generated_at", "start_time", "end_time")
        }
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


def test_manifest_passed_exit_0_and_report(project):
    tmp_path = project[0]
    manifest = _write_manifest(project)
    report = tmp_path / "r.json"
    argv = ["verify", "--manifest", str(manifest), "--report", str(report)]
    assert main(argv) == 0
    data = json.load(open(report))
    assert data["result"] == "passed"
    assert data["schema_version"] == 3
    assert data["run_id"] == "rid"
    assert [t["name"] for t in data["testbenches"]] == ["tb_pass"]


def test_manifest_equivalent_to_itemized(project):
    tmp_path, src, tb, _, _ = project
    manifest = _write_manifest(project)
    r_manifest = tmp_path / "r1.json"
    argv = ["verify", "--manifest", str(manifest), "--report", str(r_manifest)]
    assert main(argv) == 0

    r_itemized = tmp_path / "r2.json"
    argv = [
        "verify", str(src),
        "--run-id", "rid", "--duration", "100ns",
        "--tb", str(tb),
        "--workdir", str(tmp_path / "w"),
        "--report", str(r_itemized),
    ]
    assert main(argv) == 0

    data_manifest = _scrub(json.load(open(r_manifest)))
    data_itemized = _scrub(json.load(open(r_itemized)))
    assert data_manifest == data_itemized


def test_manifest_cwd_independent_and_report_at_launch_dir(
    project, monkeypatch
):
    tmp_path = project[0]
    manifest = _write_manifest(project)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    # 清单内相对路径按清单目录解析；--report 按启动目录解析。
    argv = ["verify", "--manifest", str(manifest), "--report", "out.json"]
    assert main(argv) == 0
    assert (elsewhere / "out.json").exists()
    data = json.load(open(elsewhere / "out.json"))
    assert data["result"] == "passed"


def test_manifest_multiseed_schema_v5(project):
    tmp_path = project[0]
    manifest = _write_manifest(project, seeds=[1, 2])
    report = tmp_path / "r.json"
    argv = ["verify", "--manifest", str(manifest), "--report", str(report)]
    assert main(argv) == 0
    data = json.load(open(report))
    assert data["schema_version"] == 5
    assert data["result"] == "passed"
    assert data["testbenches"][0]["seeds"] == [1, 2]
    assert [r["seed"] for r in data["testbenches"][0]["runs"]] == [1, 2]


def test_manifest_assertion_failure_exit_7_with_report(project):
    tmp_path = project[0]
    manifest = _write_manifest(
        project, testbenches=[{"testbench": "tb/tb_fail.v"}]
    )
    report = tmp_path / "r.json"
    argv = ["verify", "--manifest", str(manifest), "--report", str(report)]
    assert main(argv) == 7
    data = json.load(open(report))
    assert data["result"] == "failed"
    assert data["testbenches"][0]["reason"] == "assertion_failed"


def test_manifest_baseline_comparison(project):
    tmp_path = project[0]
    manifest = _write_manifest(project)
    baseline = tmp_path / "base.json"
    argv = ["verify", "--manifest", str(manifest), "--report", str(baseline)]
    assert main(argv) == 0

    report = tmp_path / "r.json"
    argv = [
        "verify", "--manifest", str(manifest),
        "--report", str(report), "--baseline", str(baseline),
    ]
    assert main(argv) == 0
    data = json.load(open(report))
    assert data["schema_version"] == 4
    assert data["comparison"]["passed"] is True
    assert data["result"] == "passed"


@pytest.mark.parametrize("extra", [
    ["--run-id", "x"],
    ["--duration", "1ns"],
    ["--tb", "x.v"],
    ["--workdir", "w2"],
    ["--coverage-threshold", "0.5"],
    ["--coverage-threshold", "0.0"],
    ["--cover", "c1"],
    ["--skip", "s"],
    ["--optional", "o"],
    ["--tb-seed", "n=1"],
    ["--seeds", "1,2"],
    ["--jobs", "2"],
    ["--incdir", "."],
    ["--define", "A=1"],
    ["--parameter", "p=1"],
    ["--timeout", "5"],
])
def test_manifest_conflicts_with_itemized_args_exit_2(project, extra):
    tmp_path = project[0]
    manifest = _write_manifest(project)
    report = tmp_path / "r.json"
    argv = [
        "verify", "--manifest", str(manifest), "--report", str(report),
    ] + extra
    assert main(argv) == 2
    assert not report.exists()


def test_manifest_conflicts_with_positional_source_exit_2(project):
    tmp_path, src, _, _, _ = project
    manifest = _write_manifest(project)
    report = tmp_path / "r.json"
    argv = [
        "verify", str(src), "--manifest", str(manifest),
        "--report", str(report),
    ]
    assert main(argv) == 2
    assert not report.exists()


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(schema_version=2),
    lambda d: d.update(unknown=1),
    lambda d: d.pop("run_id"),
    lambda d: d.update(sources="rtl/d.v"),
    lambda d: d["coverage"].update(foo=1),
])
def test_invalid_manifest_exit_2_no_report(project, mutate):
    tmp_path = project[0]
    data = json.loads(_write_manifest(project).read_text(encoding="utf-8"))
    mutate(data)
    manifest = tmp_path / "bad.json"
    manifest.write_text(json.dumps(data), encoding="utf-8")
    report = tmp_path / "r.json"
    argv = ["verify", "--manifest", str(manifest), "--report", str(report)]
    assert main(argv) == 2
    assert not report.exists()


def test_invalid_manifest_keeps_existing_report(project):
    tmp_path = project[0]
    manifest = _write_manifest(project, schema_version=2)
    report = tmp_path / "r.json"
    report.write_text("sentinel", encoding="utf-8")
    argv = ["verify", "--manifest", str(manifest), "--report", str(report)]
    assert main(argv) == 2
    assert report.read_text(encoding="utf-8") == "sentinel"


def test_missing_manifest_file_exit_2(project):
    tmp_path = project[0]
    report = tmp_path / "r.json"
    argv = [
        "verify", "--manifest", str(tmp_path / "nope.json"),
        "--report", str(report),
    ]
    assert main(argv) == 2
    assert not report.exists()


def test_itemized_missing_required_still_systemexit_2(project):
    tmp_path, src, tb, _, _ = project
    # 未给 --manifest 时，--run-id 仍为 argparse 级必填（SystemExit 2）。
    argv = ["verify", str(src), "--duration", "100ns", "--tb", str(tb),
            "--workdir", str(tmp_path / "w")]
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2


def test_itemized_verify_unchanged(project):
    # 逐项参数路径不受 --manifest 新增影响。
    tmp_path, src, tb, _, _ = project
    report = tmp_path / "r.json"
    argv = [
        "verify", str(src),
        "--run-id", "rid", "--duration", "100ns",
        "--tb", str(tb),
        "--workdir", str(tmp_path / "w"),
        "--report", str(report),
    ]
    assert main(argv) == 0
    data = json.load(open(report))
    assert data["result"] == "passed"
    assert data["schema_version"] == 3
