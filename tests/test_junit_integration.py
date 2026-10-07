"""verify --junit 端到端集成测试：需要 iverilog/vvp，缺失时自动跳过。"""

import json
import os
import shutil
import xml.etree.ElementTree as ET

import pytest

from rtl_lab.cli import main
from rtl_lab.errors import InputError
from rtl_lab.junit import build_junit_xml
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
    }
    return tmp_path, str(src), tbs


def _cfg(project, tb_names, *, run_id="rid", threshold=1.0, points=None,
         skip=(), optional=(), jobs=1, seeds=None, report=True, junit=True):
    tmp_path, src, tbs = project
    specs = [
        TestSpec(
            testbench=tbs[n],
            required=n not in optional,
            skip=n in skip,
        )
        for n in tb_names
    ]
    return VerifyConfig(
        sources=[src],
        testbenches=specs,
        run_id=run_id,
        duration="100ns",
        coverage=CoverageConfig(threshold=threshold, points=list(points or [])),
        workdir=str(tmp_path / "work"),
        report_path=str(tmp_path / "report.json") if report else None,
        junit_path=str(tmp_path / "report.xml") if junit else None,
        jobs=jobs,
        seeds=seeds,
    )


def _parse(path):
    return ET.parse(path).getroot()


def test_passed_writes_xml_alongside_json(project):
    cfg = _cfg(project, ["pass", "pass2"])
    report = verify(cfg)
    assert os.path.isfile(cfg.report_path)
    assert os.path.isfile(cfg.junit_path)
    root = _parse(cfg.junit_path)
    assert root.tag == "testsuites"
    assert root.get("name") == "rid"
    assert root.get("tests") == "2"
    assert root.get("failures") == "0"
    assert root.get("skipped") == "0"
    assert root.get("errors") == "0"
    suites = root.findall("testsuite")
    assert [s.get("name") for s in suites] == ["tb_pass", "tb_pass2"]
    # XML 与返回的最终报告一致。
    assert open(cfg.junit_path, encoding="utf-8").read() == build_junit_xml(report)


def test_no_junit_path_writes_nothing(project):
    cfg = _cfg(project, ["pass"], junit=False)
    verify(cfg)
    assert not (project[0] / "report.xml").exists()


def test_failure_reasons_end_to_end(project):
    cfg = _cfg(project, ["pass", "afail", "sfail", "cfail"])
    report = verify(cfg)
    assert report["result"] == "failed"
    root = _parse(cfg.junit_path)
    by_suite = {s.get("name"): s for s in root.findall("testsuite")}
    ftypes = {
        "tb_afail": "AssertionFailure",
        "tb_sfail": "SimulationFailure",
        "tb_cfail": "CompilationFailure",
    }
    for name, ftype in ftypes.items():
        failure = by_suite[name].find("testcase").find("failure")
        assert failure.get("type") == ftype


def test_multiseed_generates_seed_suffixed_cases(project):
    cfg = _cfg(project, ["pass", "afail"], seeds=[1, 2])
    verify(cfg)
    root = _parse(cfg.junit_path)
    by_suite = {s.get("name"): s for s in root.findall("testsuite")}
    assert [c.get("name") for c in by_suite["tb_pass"].findall("testcase")] == [
        "tb_pass[seed=1]", "tb_pass[seed=2]"
    ]
    afail_cases = by_suite["tb_afail"].findall("testcase")
    assert [c.get("name") for c in afail_cases] == [
        "tb_afail[seed=1]", "tb_afail[seed=2]"
    ]
    assert all(
        c.find("failure").get("type") == "AssertionFailure"
        for c in afail_cases
    )


def test_multiseed_compilation_failure_single_same_name_case(project):
    cfg = _cfg(project, ["cfail"], seeds=[1, 2, 3], junit=True)
    report = verify(cfg)
    assert report["testbenches"][0]["reason"] == "compilation_failed"
    root = _parse(cfg.junit_path)
    cases = root.find("testsuite").findall("testcase")
    assert len(cases) == 1
    assert cases[0].get("name") == "tb_cfail"
    assert cases[0].find("failure").get("type") == "CompilationFailure"


def test_multiseed_simulation_failure_stops_further_seeds(project):
    cfg = _cfg(project, ["sfail"], seeds=[1, 2, 3])
    verify(cfg)
    root = _parse(cfg.junit_path)
    cases = root.find("testsuite").findall("testcase")
    assert [c.get("name") for c in cases] == [
        "tb_sfail[seed=1]"
    ]
    assert cases[0].find("failure").get("type") == "SimulationFailure"


def test_optional_and_required_skips(project):
    cfg = _cfg(
        project, ["pass", "pass2", "afail"],
        skip=["pass2", "afail"], optional=["pass2"],
    )
    verify(cfg)
    root = _parse(cfg.junit_path)
    by_suite = {s.get("name"): s for s in root.findall("testsuite")}
    opt_case = by_suite["tb_pass2"].find("testcase")
    assert opt_case.find("skipped") is not None
    assert opt_case.find("failure") is None
    req_case = by_suite["tb_afail"].find("testcase")
    assert req_case.find("failure").get("type") == "RequiredTestSkipped"
    assert root.get("skipped") == "1"
    # RequiredTestSkipped 计入 failures 而非 skipped。
    assert root.get("failures") == "1"


def test_coverage_failure_suite(project):
    # c1 命中、c2 点名但无统计 => 0.5 < 1.0。
    cfg = _cfg(project, ["pass"], points=["c1", "missing"])
    verify(cfg)
    root = _parse(cfg.junit_path)
    names = [s.get("name") for s in root.findall("testsuite")]
    assert names == ["tb_pass", "coverage"]
    cov = root.findall("testsuite")[-1]
    case = {c.get("name"): c for c in cov.findall("testcase")}["threshold"]
    failure = case.find("failure")
    assert failure.get("type") == "CoverageFailure"
    assert "1/2" in failure.text and "threshold=1.0" in failure.text


def test_baseline_mismatch_suite(project):
    tmp_path, src, tbs = project
    baseline_path = tmp_path / "baseline.json"
    base_cfg = VerifyConfig(
        sources=[src], testbenches=[TestSpec(testbench=tbs["pass"])],
        run_id="base", duration="100ns", coverage=CoverageConfig(),
        workdir=str(tmp_path / "wb"), report_path=str(baseline_path),
    )
    verify(base_cfg)

    cfg = VerifyConfig(
        sources=[src], testbenches=[TestSpec(testbench=tbs["afail"])],
        run_id="cur", duration="100ns", coverage=CoverageConfig(),
        workdir=str(tmp_path / "wc"),
        report_path=str(tmp_path / "cur.json"),
        baseline_path=str(baseline_path),
        junit_path=str(tmp_path / "cur.xml"),
    )
    report = verify(cfg)
    assert not report["comparison"]["passed"]
    root = _parse(cfg.junit_path)
    names = [s.get("name") for s in root.findall("testsuite")]
    assert names[-1] == "baseline"
    base_suite = root.findall("testsuite")[-1]
    failure = base_suite.find("testcase").find("failure")
    assert failure.get("type") == "BaselineMismatch"
    count = len(report["comparison"]["mismatches"])
    assert f"{count} mismatches" in failure.text


def test_xml_matches_report_stats(project):
    cfg = _cfg(project, ["pass", "afail"], seeds=[1, 2],
               points=["c1", "c3", "x"])
    verify(cfg)
    root = _parse(cfg.junit_path)
    # testcase 实际数量汇总。
    case_count = len(root.findall(".//testcase"))
    fail_count = len(root.findall(".//testcase/failure"))
    skip_count = len(root.findall(".//testcase/skipped"))
    assert int(root.get("tests")) == case_count
    assert int(root.get("failures")) == fail_count
    assert int(root.get("skipped")) == skip_count
    assert root.get("errors") == "0"
    # 两个 tb suite + coverage suite（2/3 命中，阈值 1.0 不达标）。
    assert [s.get("name") for s in root.findall("testsuite")] == [
        "tb_pass", "tb_afail", "coverage"
    ]


def test_xml_is_deterministic_across_runs(project):
    cfg1 = _cfg(project, ["pass", "afail"], seeds=[1, 2], run_id="det")
    r1 = verify(cfg1)
    text1 = open(cfg1.junit_path, encoding="utf-8").read()
    cfg2 = _cfg(project, ["pass", "afail"], seeds=[1, 2], run_id="det")
    cfg2.workdir = str(project[0] / "work2")
    cfg2.report_path = str(project[0] / "r2.json")
    cfg2.junit_path = str(project[0] / "r2.xml")
    r2 = verify(cfg2)
    text2 = open(cfg2.junit_path, encoding="utf-8").read()
    assert text1 == text2 == build_junit_xml(r1) == build_junit_xml(r2)


# ---------------- 配置与前置条件 ----------------

def test_junit_path_empty_raises_input_error(project):
    cfg = _cfg(project, ["pass"])
    cfg.junit_path = "   "
    with pytest.raises(InputError):
        verify(cfg)
    assert not (project[0] / "report.json").exists()
    assert not (project[0] / "report.xml").exists()


def test_junit_same_as_report_raises_input_error(project):
    cfg = _cfg(project, ["pass"])
    cfg.junit_path = cfg.report_path
    with pytest.raises(InputError):
        verify(cfg)


def test_junit_same_as_baseline_raises_input_error(project):
    tmp_path, src, tbs = project
    baseline_path = tmp_path / "baseline.json"
    base_cfg = VerifyConfig(
        sources=[src], testbenches=[TestSpec(testbench=tbs["pass"])],
        run_id="base", duration="100ns", coverage=CoverageConfig(),
        workdir=str(tmp_path / "wb"), report_path=str(baseline_path),
    )
    verify(base_cfg)
    cfg = VerifyConfig(
        sources=[src], testbenches=[TestSpec(testbench=tbs["pass"])],
        run_id="cur", duration="100ns", coverage=CoverageConfig(),
        workdir=str(tmp_path / "wc"),
        baseline_path=str(baseline_path),
        junit_path=str(baseline_path),
    )
    with pytest.raises(InputError):
        verify(cfg)
    # 基线文件未被覆盖（仍是基线报告）。
    data = json.load(open(baseline_path))
    assert data["run_id"] == "base"


def test_junit_path_is_directory_exit_8_and_no_outputs(project):
    cfg = _cfg(project, ["pass"])
    target_dir = project[0] / "xdir"
    target_dir.mkdir()
    cfg.junit_path = str(target_dir)  # 已存在的目录
    with pytest.raises(OSError):
        verify(cfg)
    assert not (project[0] / "report.json").exists()


def test_existing_json_and_xml_preserved_on_write_failure(project, monkeypatch):
    cfg = _cfg(project, ["pass"])
    old_json = project[0] / "report.json"
    old_xml = project[0] / "report.xml"
    old_json.write_text('{"keep": "json"}', encoding="utf-8")
    old_xml.write_text("<keep>xml</keep>", encoding="utf-8")

    import rtl_lab.verification as verification

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(verification.os, "replace", boom)
    with pytest.raises(OSError):
        verify(cfg)
    assert old_json.read_text(encoding="utf-8") == '{"keep": "json"}'
    assert old_xml.read_text(encoding="utf-8") == "<keep>xml</keep>"


def test_jobs_parallel_multiseed_order_stable(project):
    cfg = _cfg(project, ["pass2", "pass", "afail"], seeds=[3, 1], jobs=3)
    verify(cfg)
    root = _parse(cfg.junit_path)
    assert [s.get("name") for s in root.findall("testsuite")] == [
        "tb_pass2", "tb_pass", "tb_afail"
    ]
    for suite in root.findall("testsuite"):
        assert [c.get("name") for c in suite.findall("testcase")] == [
            f"{suite.get('name')}[seed=3]",
            f"{suite.get('name')}[seed=1]",
        ]


# ---------------- CLI ----------------

def _cli_base(project, *extra):
    tmp_path, src, tbs = project
    return [
        "verify", str(src), "--run-id", "rid", "--duration", "100ns",
        "--tb", str(tbs["pass"]),
        "--workdir", str(tmp_path / "w"),
        "--report", str(tmp_path / "r.json"),
        "--junit", str(tmp_path / "r.xml"),
        *extra,
    ], tmp_path / "r.xml"


def test_cli_junit_passed_exit_0(project):
    argv, xml_path = _cli_base(project)
    assert main(argv) == 0
    root = _parse(xml_path)
    assert root.get("failures") == "0"


def test_cli_junit_same_file_as_report_exit_2(project):
    tmp_path, src, tbs = project
    same = str(tmp_path / "same.json")
    argv = [
        "verify", str(src), "--run-id", "rid", "--duration", "100ns",
        "--tb", str(tbs["pass"]), "--workdir", str(tmp_path / "w"),
        "--report", same, "--junit", same,
    ]
    assert main(argv) == 2
    assert not (tmp_path / "same.json").exists()


def test_cli_manifest_with_junit(project):
    tmp_path, _src, tbs = project
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "schema_version": 1,
        "run_id": "mani",
        "duration": "100ns",
        "sources": ["d.v"],
        "testbenches": [
            {"testbench": "tb_pass.v"},
            {"testbench": "tb_pass2.v", "required": False, "skip": True},
        ],
        "coverage": {"threshold": 1.0},
        "workdir": "wm",
    }), encoding="utf-8")
    xml_path = tmp_path / "m.xml"
    argv = [
        "verify", "--manifest", str(manifest),
        "--junit", str(xml_path),
    ]
    assert main(argv) == 0  # pass2 为可选跳过，总体结论仍 passed
    root = _parse(xml_path)
    by_suite = {s.get("name"): s for s in root.findall("testsuite")}
    case = by_suite["tb_pass2"].find("testcase")
    assert case.find("skipped") is not None
