"""CLI verify --manifest 测试：清单入口、互斥规则与退出码。"""

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


def _write_manifest(tmp_path, **overrides):
    data = {
        "schema_version": 1,
        "run_id": "rid",
        "duration": "100ns",
        "sources": ["d.v"],
        "testbenches": [{"testbench": "tb_pass.v"}],
        "coverage": {"threshold": 1.0, "points": []},
        "workdir": "w",
    }
    data.update(overrides)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_manifest_passed_exit_0(files):
    tmp, _src, _tb, _tbf = files
    manifest = _write_manifest(tmp)
    report = tmp / "r.json"
    argv = ["verify", "--manifest", str(manifest), "--report", str(report)]
    assert main(argv) == 0
    data = json.load(open(report))
    assert data["result"] == "passed"
    assert data["schema_version"] == 3
    assert data["format"] == "rtl-lab-verification"
    assert data["run_id"] == "rid"
    assert [t["name"] for t in data["testbenches"]] == ["tb_pass"]
    # 工作目录按清单目录解析。
    assert (tmp / "w").is_dir()


def test_manifest_equivalent_to_itemwise(files, monkeypatch):
    tmp, _src, _tb, _tbf = files
    manifest = _write_manifest(tmp)
    assert main(["verify", "--manifest", str(manifest),
                 "--report", str(tmp / "m.json")]) == 0
    monkeypatch.chdir(tmp)
    argv = [
        "verify", "d.v", "--run-id", "rid", "--duration", "100ns",
        "--tb", "tb_pass.v", "--workdir", "w2", "--report", "i.json",
    ]
    assert main(argv) == 0
    m = json.load(open(tmp / "m.json"))
    i = json.load(open(tmp / "i.json"))
    for key in ("result", "schema_version", "run_id",
                "testbench_summary", "assertion_summary", "coverage_summary"):
        assert m[key] == i[key]
    assert m["config"]["duration"] == i["config"]["duration"] == "100ns"
    assert m["config"]["coverage"] == i["config"]["coverage"]
    assert [t["name"] for t in m["testbenches"]] == [
        t["name"] for t in i["testbenches"]
    ]


def test_manifest_failed_exit_7(files):
    tmp, _src, _tb, _tbf = files
    manifest = _write_manifest(
        tmp, testbenches=[{"testbench": "tb_fail.v"}],
    )
    report = tmp / "r.json"
    argv = ["verify", "--manifest", str(manifest), "--report", str(report)]
    assert main(argv) == 7
    data = json.load(open(report))
    assert data["result"] == "failed"


def test_manifest_multiseed_schema_v5(files):
    tmp, _src, _tb, _tbf = files
    manifest = _write_manifest(tmp, seeds=[1, 2])
    report = tmp / "r.json"
    argv = ["verify", "--manifest", str(manifest), "--report", str(report)]
    assert main(argv) == 0
    data = json.load(open(report))
    assert data["schema_version"] == 5
    assert data["testbenches"][0]["seeds"] == [1, 2]
    assert [r["seed"] for r in data["testbenches"][0]["runs"]] == [1, 2]


def test_manifest_cwd_independent(files, monkeypatch):
    tmp, _src, _tb, _tbf = files
    proj = tmp / "proj"
    proj.mkdir()
    (proj / "d.v").write_text(DESIGN)
    (proj / "tb_pass.v").write_text(TB_PASS)
    manifest = _write_manifest(proj)
    elsewhere = tmp / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    # 清单与报告均用相对路径：清单按自身目录解析输入，报告按启动目录解析。
    assert main(["verify", "--manifest", "../proj/manifest.json",
                 "--report", "r.json"]) == 0
    data = json.load(open(elsewhere / "r.json"))
    assert data["result"] == "passed"
    assert (proj / "w").is_dir()
    assert not (elsewhere / "w").exists()


@pytest.mark.parametrize("extra", [
    ["--run-id", "x"],
    ["--duration", "100ns"],
    ["--tb", "tb_pass.v"],
    ["--workdir", "w"],
    ["--coverage-threshold", "0.5"],
    ["--cover", "c1"],
    ["--skip", "tb_pass"],
    ["--optional", "tb_pass"],
    ["--tb-seed", "tb_pass=1"],
    ["--seeds", "1,2"],
    ["--jobs", "2"],
    ["--incdir", "."],
    ["--define", "A=1"],
    ["--parameter", "top.W=8"],
    ["--timeout", "10"],
])
def test_manifest_conflicts_with_itemwise_args_exit_2(files, extra):
    tmp, _src, _tb, _tbf = files
    manifest = _write_manifest(tmp)
    report = tmp / "r.json"
    argv = ["verify", "--manifest", str(manifest),
            "--report", str(report)] + extra
    assert main(argv) == 2
    assert not report.exists()


def test_manifest_conflicts_with_positional_sources_exit_2(files):
    tmp, src, _tb, _tbf = files
    manifest = _write_manifest(tmp)
    report = tmp / "r.json"
    argv = ["verify", str(src), "--manifest", str(manifest),
            "--report", str(report)]
    assert main(argv) == 2
    assert not report.exists()


def test_manifest_missing_file_exit_2_no_report(files):
    tmp, _src, _tb, _tbf = files
    report = tmp / "r.json"
    argv = ["verify", "--manifest", str(tmp / "absent.json"),
            "--report", str(report)]
    assert main(argv) == 2
    assert not report.exists()


def test_manifest_invalid_json_exit_2_no_report(files):
    tmp, _src, _tb, _tbf = files
    manifest = tmp / "manifest.json"
    manifest.write_text("{oops", encoding="utf-8")
    report = tmp / "r.json"
    argv = ["verify", "--manifest", str(manifest), "--report", str(report)]
    assert main(argv) == 2
    assert not report.exists()


def test_manifest_unknown_key_exit_2_no_report(files):
    tmp, _src, _tb, _tbf = files
    manifest = _write_manifest(tmp, bogus=1)
    report = tmp / "r.json"
    argv = ["verify", "--manifest", str(manifest), "--report", str(report)]
    assert main(argv) == 2
    assert not report.exists()


def test_manifest_bad_schema_version_exit_2_no_report(files):
    tmp, _src, _tb, _tbf = files
    manifest = _write_manifest(tmp, schema_version=2)
    report = tmp / "r.json"
    argv = ["verify", "--manifest", str(manifest), "--report", str(report)]
    assert main(argv) == 2
    assert not report.exists()


def test_manifest_bad_field_exit_2_no_report(files):
    tmp, _src, _tb, _tbf = files
    manifest = _write_manifest(tmp, sources=["missing.v"])
    report = tmp / "r.json"
    argv = ["verify", "--manifest", str(manifest), "--report", str(report)]
    assert main(argv) == 2
    assert not report.exists()


def test_manifest_all_skipped_exit_8_no_report(files):
    tmp, _src, _tb, _tbf = files
    manifest = _write_manifest(
        tmp, testbenches=[{"testbench": "tb_pass.v", "skip": True}],
    )
    report = tmp / "r.json"
    argv = ["verify", "--manifest", str(manifest), "--report", str(report)]
    assert main(argv) == 8
    assert not report.exists()


def test_manifest_report_unwritable_exit_8(files):
    tmp, _src, _tb, _tbf = files
    manifest = _write_manifest(tmp)
    argv = ["verify", "--manifest", str(manifest), "--report", str(tmp)]
    assert main(argv) == 8


def test_manifest_with_baseline(files):
    tmp, _src, _tb, _tbf = files
    manifest = _write_manifest(tmp)
    baseline = tmp / "base.json"
    assert main(["verify", "--manifest", str(manifest),
                 "--report", str(baseline)]) == 0
    report = tmp / "r.json"
    argv = ["verify", "--manifest", str(manifest),
            "--baseline", str(baseline), "--report", str(report)]
    assert main(argv) == 0
    data = json.load(open(report))
    assert data["schema_version"] == 4
    assert data["comparison"]["passed"] is True


def test_no_manifest_missing_args_still_exit_2(files):
    # 无清单行为不变：缺少必填逐项参数仍是 usage 错误（SystemExit 2）。
    with pytest.raises(SystemExit) as exc:
        main(["verify"])
    assert exc.value.code == 2
