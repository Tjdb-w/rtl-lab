"""verify 的 JUnit XML 输出（``--junit`` / ``VerifyConfig.junit_path``）测试。"""

import json
import shutil
import xml.etree.ElementTree as ET

import pytest

from rtl_lab.cli import main
from rtl_lab.errors import InputError
from rtl_lab.junit import build_junit_xml
from rtl_lab.report import build_verification_report
from rtl_lab.verification import CoverageConfig, TestSpec, VerifyConfig, verify


def _outcome(name, *, status="passed", reason="passed", required=True,
             diagnostics=None, assertions=None, coverage=None):
    return {
        "name": name,
        "top": name,
        "testbench_file": f"/w/tb/{name}.v",
        "status": status,
        "reason": reason,
        "required": required,
        "start_time": "2026-01-01T00:00:00+00:00",
        "end_time": "2026-01-01T00:00:01+00:00",
        "diagnostics": list(diagnostics or []),
        "assertions": assertions if assertions is not None else {
            "a1": {"status": "passed", "fail_count": 0}
        },
        "coverage": coverage if coverage is not None else {"c1": 1},
        "commands": {
            "compile": ["/usr/bin/iverilog", "-o", "/w/sim.vvp", "/w/d.v"],
            "simulate": ["/usr/bin/vvp", "sim.vvp", "+SEED=0"],
        },
    }


def _seed_run(seed, *, status="passed", reason="passed", diagnostics=None):
    return {
        "seed": seed,
        "status": status,
        "reason": reason,
        "diagnostics": list(diagnostics or []),
        "assertions": {"a1": {"status": "passed", "fail_count": 0}},
        "coverage": {"c1": 1},
        "command": {"simulate": ["/usr/bin/vvp", "sim.vvp", f"+SEED={seed}"]},
    }


def _ms_outcome(name, runs, *, status, reason, seeds=(1, 2)):
    outcome = _outcome(name, status=status, reason=reason)
    outcome["seeds"] = list(seeds)
    outcome["runs"] = runs
    return outcome


def _build(outcomes, *, threshold=1.0, points=None, run_id="rid",
           seeds=None):
    return build_verification_report(
        run_id=run_id,
        sources=["/w/d.v"],
        testbench_files=[o["testbench_file"] for o in outcomes],
        coverage_config={"threshold": threshold, "points": points or []},
        duration="100ns",
        compile_command=outcomes[0]["commands"]["compile"],
        simulate_command=outcomes[0]["commands"]["simulate"],
        outcomes=outcomes,
        generated_at="2026-01-01T00:00:02+00:00",
        workdir="/w",
        known_paths=(),
        seeds=seeds,
    )[0]


def _parse(xml_text):
    return ET.fromstring(xml_text)


def test_passed_single_seed_structure():
    xml = _parse(build_junit_xml(_build([_outcome("tb1")], run_id="run-1")))
    assert xml.tag == "testsuites"
    assert xml.get("name") == "run-1"
    assert xml.get("tests") == "1"
    assert xml.get("failures") == "0"
    assert xml.get("skipped") == "0"
    assert xml.get("errors") == "0"
    (suite,) = xml.findall("testsuite")
    assert suite.get("name") == "tb1"
    assert suite.get("tests") == "1"
    assert suite.get("errors") == "0"
    (case,) = suite.findall("testcase")
    assert case.get("name") == "tb1"
    assert list(case) == []  # 通过项无子元素


def test_failure_reason_type_mapping():
    outcomes = [
        _outcome("tb_sim", status="failed", reason="simulation_failed",
                 diagnostics=["sim boom"]),
        _outcome("tb_assert", status="failed", reason="assertion_failed",
                 diagnostics=["ASSERT FAIL a1 boom"]),
        _outcome("tb_stats", status="failed", reason="incomplete_statistics"),
        _outcome("tb_compile", status="failed", reason="compilation_failed",
                 diagnostics=["syntax error"], assertions={}, coverage={}),
    ]
    xml = _parse(build_junit_xml(_build(outcomes)))
    cases = xml.findall("testsuite/testcase")
    assert [c.get("name") for c in cases] == [
        "tb_sim", "tb_assert", "tb_stats", "tb_compile",
    ]
    types = [
        c.find("failure").get("type") for c in cases
    ]
    assert types == [
        "SimulationFailure", "AssertionFailure",
        "IncompleteStatistics", "CompilationFailure",
    ]
    # failure 内容取脱敏 reason 与 diagnostics。
    assert "simulation_failed" in cases[0].find("failure").text
    assert "sim boom" in cases[0].find("failure").text
    assert "ASSERT FAIL a1 boom" in cases[1].find("failure").text
    assert "syntax error" in cases[3].find("failure").text
    assert xml.get("failures") == "4"


def test_skipped_optional_and_required():
    outcomes = [
        _outcome("tb_ok"),
        _outcome("tb_opt", status="skipped", reason="skipped",
                 required=False, assertions={}, coverage={}),
        _outcome("tb_req", status="skipped", reason="skipped",
                 required=True, assertions={}, coverage={}),
    ]
    xml = _parse(build_junit_xml(_build(outcomes)))
    cases = {
        c.get("name"): c for c in xml.findall("testsuite/testcase")
    }
    opt_skip = cases["tb_opt"].find("skipped")
    assert opt_skip is not None and opt_skip.get("message") is None
    req_skip = cases["tb_req"].find("skipped")
    assert req_skip is not None
    assert req_skip.get("message") == "RequiredTestSkipped"
    assert cases["tb_ok"].find("skipped") is None
    assert xml.get("skipped") == "2"
    assert xml.get("tests") == "3"


def test_multiseed_testcases_in_seed_order():
    runs = [
        _seed_run(7),
        _seed_run(8, status="failed", reason="assertion_failed",
                  diagnostics=["ASSERT FAIL a1 boom"]),
    ]
    outcome = _ms_outcome(
        "tb_m", runs, status="failed", reason="assertion_failed",
        seeds=(7, 8),
    )
    xml = _parse(build_junit_xml(_build([outcome], seeds=[7, 8])))
    (suite,) = xml.findall("testsuite")
    assert suite.get("name") == "tb_m"
    cases = suite.findall("testcase")
    assert [c.get("name") for c in cases] == [
        "tb_m[seed=7]", "tb_m[seed=8]",
    ]
    assert list(cases[0]) == []
    failure = cases[1].find("failure")
    assert failure.get("type") == "AssertionFailure"
    assert "ASSERT FAIL a1 boom" in failure.text
    assert xml.get("tests") == "2"
    assert xml.get("failures") == "1"


def test_multiseed_compilation_failed_single_testcase():
    outcome = _ms_outcome(
        "tb_c", [], status="failed", reason="compilation_failed",
        seeds=(1, 2),
    )
    outcome["diagnostics"] = ["syntax error"]
    xml = _parse(build_junit_xml(_build([outcome], seeds=[1, 2])))
    suites = {s.get("name"): s for s in xml.findall("testsuite")}
    cases = suites["tb_c"].findall("testcase")
    assert [c.get("name") for c in cases] == ["tb_c"]
    assert cases[0].find("failure").get("type") == "CompilationFailure"


def test_coverage_threshold_testcase():
    outcome = _outcome("tb1", coverage={"c1": 0})
    xml = _parse(build_junit_xml(
        _build([outcome], threshold=0.5)
    ))
    suites = {
        s.get("name"): s for s in xml.findall("testsuite")
    }
    case = suites["coverage"].find("testcase")
    assert case.get("name") == "threshold"
    failure = case.find("failure")
    assert failure.get("type") == "CoverageFailure"
    assert "hit_points=0" in failure.text
    assert "total_points=1" in failure.text
    assert "ratio=0.0" in failure.text
    assert "threshold=0.5" in failure.text
    assert xml.get("failures") == "1"


def test_coverage_met_no_coverage_suite():
    xml = _parse(build_junit_xml(_build([_outcome("tb1")])))
    assert [s.get("name") for s in xml.findall("testsuite")] == ["tb1"]


def test_baseline_mismatch_testcase():
    report = _build([_outcome("tb1")])
    report["comparison"] = {
        "baseline": "base.json",
        "passed": False,
        "mismatches": [
            {"kind": "testbench_status", "name": "tb1",
             "expected": "passed", "actual": "failed"},
            {"kind": "coverage_hits", "name": "c1",
             "expected": 1, "actual": 0},
        ],
    }
    xml = _parse(build_junit_xml(report))
    suites = {s.get("name"): s for s in xml.findall("testsuite")}
    case = suites["baseline"].find("testcase")
    assert case.get("name") == "comparison"
    failure = case.find("failure")
    assert failure.get("type") == "BaselineMismatch"
    assert "mismatches=2" in failure.text


def test_baseline_passed_no_baseline_suite():
    report = _build([_outcome("tb1")])
    report["comparison"] = {
        "baseline": "base.json", "passed": True, "mismatches": [],
    }
    xml = _parse(build_junit_xml(report))
    assert [s.get("name") for s in xml.findall("testsuite")] == ["tb1"]


def test_xml_escaping():
    outcome = _outcome(
        'tb<&"', status="failed", reason="simulation_failed",
        diagnostics=["boom & <bang> \"q\""],
    )
    xml_text = build_junit_xml(_build([outcome]))
    # 原始特殊字符不得出现在标记中（必须被转义），且可解析还原。
    assert 'name="tb<&"' not in xml_text
    xml = _parse(xml_text)
    case = xml.find("testsuite/testcase")
    assert case.get("name") == 'tb<&"'
    assert "boom & <bang> \"q\"" in case.find("failure").text


def test_deterministic_no_timestamps():
    report = _build([_outcome("tb1")])
    first = build_junit_xml(report)
    second = build_junit_xml(report)
    assert first == second
    assert "timestamp" not in first
    assert "time=" not in first
    assert report["generated_at"] not in first
    assert first.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert first.endswith("\n")


# ---- 配置校验（不调用仿真器） ----


def _config(tmp_path, **overrides):
    src = tmp_path / "d.v"
    src.write_text("module d; endmodule\n")
    tb = tmp_path / "tb.v"
    tb.write_text("module tb; endmodule\n")
    kwargs = {
        "sources": [str(src)],
        "testbenches": [TestSpec(testbench=str(tb))],
        "run_id": "rid",
        "duration": "100ns",
        "coverage": CoverageConfig(),
        "workdir": str(tmp_path / "w"),
    }
    kwargs.update(overrides)
    return VerifyConfig(**kwargs)


def test_junit_path_empty_raises_input_error(tmp_path):
    config = _config(tmp_path, junit_path="")
    with pytest.raises(InputError):
        verify(config)


def test_junit_path_same_as_report_raises_input_error(tmp_path):
    out = str(tmp_path / "out.json")
    config = _config(
        tmp_path, report_path=out, junit_path=out,
    )
    with pytest.raises(InputError):
        verify(config)


def test_junit_path_same_as_baseline_raises_input_error(tmp_path):
    base = str(tmp_path / "base.json")
    config = _config(
        tmp_path, baseline_path=base, junit_path=base,
    )
    with pytest.raises(InputError):
        verify(config)


def test_junit_path_unwritable_raises_os_error(tmp_path):
    config = _config(tmp_path, junit_path=str(tmp_path))  # 目录
    with pytest.raises(OSError):
        verify(config)


# ---- CLI 集成（需要 Icarus Verilog） ----

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


needs_iverilog = pytest.mark.skipif(
    shutil.which("iverilog") is None or shutil.which("vvp") is None,
    reason="需要安装 Icarus Verilog",
)


def _argv(files, *extra, junit="junit.xml", report="r.json"):
    tmp, src, tb, _tbf = files
    argv = [
        "verify", str(src),
        "--run-id", "rid", "--duration", "100ns",
        "--tb", str(tb),
        "--workdir", str(tmp / "w"),
    ]
    if report is not None:
        argv += ["--report", str(tmp / report)]
    if junit is not None:
        argv += ["--junit", str(tmp / junit)]
    return argv + list(extra)


@needs_iverilog
def test_cli_junit_written_with_report(files):
    tmp = files[0]
    assert main(_argv(files)) == 0
    assert (tmp / "r.json").exists()
    xml = _parse((tmp / "junit.xml").read_text(encoding="utf-8"))
    assert xml.get("name") == "rid"
    assert xml.get("tests") == "1"
    assert xml.get("failures") == "0"
    case = xml.find("testsuite/testcase")
    assert case.get("name") == "tb_pass"
    assert list(case) == []


@needs_iverilog
def test_cli_junit_without_report(files):
    tmp = files[0]
    assert main(_argv(files, report=None)) == 0
    assert (tmp / "junit.xml").exists()


@needs_iverilog
def test_cli_junit_assertion_failure(files):
    tmp, src, _tb, tbf = files
    argv = _argv(files) + ["--tb", str(tbf)]
    assert main(argv) == 7
    xml = _parse((tmp / "junit.xml").read_text(encoding="utf-8"))
    cases = {
        c.get("name"): c for c in xml.findall("testsuite/testcase")
    }
    failure = cases["tb_fail"].find("failure")
    assert failure.get("type") == "AssertionFailure"
    assert "assertion_failed" in failure.text
    assert xml.get("failures") == "1"


@needs_iverilog
def test_cli_junit_multiseed(files):
    tmp = files[0]
    argv = _argv(files) + ["--seeds", "1,2"]
    assert main(argv) == 0
    xml = _parse((tmp / "junit.xml").read_text(encoding="utf-8"))
    cases = xml.findall("testsuite/testcase")
    assert [c.get("name") for c in cases] == [
        "tb_pass[seed=1]", "tb_pass[seed=2]",
    ]


@needs_iverilog
def test_cli_junit_same_as_report_exit_2(files):
    tmp = files[0]
    argv = _argv(files, junit="r.json")
    assert main(argv) == 2
    assert not (tmp / "r.json").exists()


@needs_iverilog
def test_cli_junit_same_as_baseline_exit_2(files):
    tmp = files[0]
    # 先生成一份基线报告，再把 --junit 指向同一路径。
    assert main(_argv(files, junit=None, report="base.json")) == 0
    argv = _argv(files, junit="base.json") + [
        "--baseline", str(tmp / "base.json"),
    ]
    assert main(argv) == 2


@needs_iverilog
def test_cli_junit_empty_exit_2(files):
    tmp, src, tb, _tbf = files
    argv = [
        "verify", str(src), "--run-id", "rid", "--duration", "100ns",
        "--tb", str(tb), "--workdir", str(tmp / "w"),
        "--report", str(tmp / "r.json"), "--junit", "",
    ]
    assert main(argv) == 2
    assert not (tmp / "r.json").exists()


@needs_iverilog
def test_cli_junit_unwritable_exit_8_no_outputs(files):
    tmp = files[0]
    argv = _argv(files, junit=".")  # junit 路径为目录
    assert main(argv) == 8
    assert not (tmp / "r.json").exists()


@needs_iverilog
def test_cli_junit_input_error_no_xml(files):
    tmp = files[0]
    argv = _argv(files)
    argv[1] = str(tmp / "missing.v")  # 不存在的源文件
    assert main(argv) == 2
    assert not (tmp / "junit.xml").exists()
    assert not (tmp / "r.json").exists()


@needs_iverilog
def test_cli_junit_with_manifest(files):
    tmp, src, tb, _tbf = files
    manifest = tmp / "m.json"
    manifest.write_text(json.dumps({
        "schema_version": 1,
        "run_id": "mid",
        "duration": "100ns",
        "sources": [str(src)],
        "testbenches": [{"testbench": str(tb)}],
        "coverage": {"threshold": 1.0},
        "workdir": str(tmp / "w"),
    }))
    argv = [
        "verify", "--manifest", str(manifest),
        "--report", str(tmp / "r.json"),
        "--junit", str(tmp / "junit.xml"),
    ]
    assert main(argv) == 0
    xml = _parse((tmp / "junit.xml").read_text(encoding="utf-8"))
    assert xml.get("name") == "mid"
    data = json.loads((tmp / "r.json").read_text(encoding="utf-8"))
    assert data["run_id"] == "mid"
