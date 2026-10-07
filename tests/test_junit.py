"""JUnit XML 构建单测：直接对合成 verify 报告断言 XML 结构（不调用仿真器）。"""

import xml.etree.ElementTree as ET

from rtl_lab.junit import build_junit_xml


def _parse(text):
    return ET.fromstring(text)


def _tb(name, *, status="passed", reason="passed", required=True,
        diagnostics=None, runs=None):
    tb = {
        "name": name,
        "status": status,
        "reason": reason,
        "required": required,
        "diagnostics": list(diagnostics or []),
    }
    if runs is not None:
        tb["runs"] = runs
    return tb


def _run(seed, status="passed", reason="passed", diagnostics=None):
    return {
        "seed": seed,
        "status": status,
        "reason": reason,
        "diagnostics": list(diagnostics or []),
    }


def _report(*testbenches, run_id="rid", met=True, hit=1, total=1,
            ratio=1.0, threshold=1.0, comparison=None):
    data = {
        "run_id": run_id,
        "testbenches": list(testbenches),
        "coverage_summary": {
            "met": met,
            "hit_points": hit,
            "total_points": total,
            "ratio": ratio,
            "threshold": threshold,
        },
    }
    if comparison is not None:
        data["comparison"] = comparison
    return data


def _cases(suite):
    return {
        case.get("name"): case
        for case in suite.findall("testcase")
    }


def _has_time_anywhere(root):
    for elem in root.iter():
        if "time" in elem.attrib or "timestamp" in elem.attrib:
            return True
    return False


def test_all_passing_structure_and_counts():
    text = build_junit_xml(_report(_tb("a"), _tb("b")))
    root = _parse(text)
    assert root.tag == "testsuites"
    assert root.get("name") == "rid"
    assert root.get("tests") == "2"
    assert root.get("failures") == "0"
    assert root.get("errors") == "0"
    assert root.get("skipped") == "0"
    suites = root.findall("testsuite")
    assert [s.get("name") for s in suites] == ["a", "b"]
    for suite in suites:
        assert suite.get("tests") == "1"
        assert suite.get("errors") == "0"
        case = suite.find("testcase")
        assert case.get("name") == suite.get("name")
        assert list(case) == []  # 通过项无子元素
    assert not _has_time_anywhere(root)


def test_xml_header_is_utf8():
    text = build_junit_xml(_report(_tb("a")))
    assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert text.endswith("\n")


def test_single_seed_failure_types_and_body():
    expected = {
        "sim": ("simulation_failed", "SimulationFailure"),
        "asr": ("assertion_failed", "AssertionFailure"),
        "inc": ("incomplete_statistics", "IncompleteStatistics"),
        "cmp": ("compilation_failed", "CompilationFailure"),
    }
    tbs = [
        _tb(name, status="failed", reason=reason, diagnostics=["diag", name])
        for name, (reason, _ftype) in expected.items()
    ]
    root = _parse(build_junit_xml(_report(*tbs, hit=4, total=4)))
    assert root.get("tests") == "4"
    assert root.get("failures") == "4"
    for suite in root.findall("testsuite"):
        case = suite.find("testcase")
        failure = case.find("failure")
        reason, ftype = expected[suite.get("name")]
        assert failure.get("type") == ftype
        assert failure.get("message") == reason
        body = failure.text
        assert body.splitlines()[0] == reason
        assert "diag" in body and suite.get("name") in body


def test_optional_skip_uses_skipped_element():
    report = _report(
        _tb("a"),
        _tb("b", status="skipped", reason="skipped", required=False),
    )
    root = _parse(build_junit_xml(report))
    assert root.get("skipped") == "1"
    assert root.get("failures") == "0"
    suite_b = root.findall("testsuite")[1]
    case = suite_b.find("testcase")
    assert case.find("skipped") is not None
    assert case.find("failure") is None


def test_required_skip_is_failure():
    report = _report(
        _tb("a"),
        _tb("b", status="skipped", reason="skipped", required=True),
    )
    root = _parse(build_junit_xml(report))
    assert root.get("skipped") == "0"
    assert root.get("failures") == "1"
    suite_b = root.findall("testsuite")[1]
    failure = suite_b.find("testcase").find("failure")
    assert failure.get("type") == "RequiredTestSkipped"
    assert failure.get("message") == "skipped"


def test_multiseed_cases_follow_execution_order():
    tb = _tb("a", runs=[
        _run(1),
        _run(2, status="failed", reason="assertion_failed",
             diagnostics=["boom"]),
        _run(3),
    ])
    root = _parse(build_junit_xml(_report(tb, hit=1, total=1)))
    suite = root.find("testsuite")
    cases = suite.findall("testcase")
    assert [c.get("name") for c in cases] == [
        "a[seed=1]", "a[seed=2]", "a[seed=3]"
    ]
    assert [c.get("classname") for c in cases] == ["a", "a", "a"]
    assert suite.get("tests") == "3"
    assert suite.get("failures") == "1"
    failure = cases[1].find("failure")
    assert failure.get("type") == "AssertionFailure"
    assert "boom" in failure.text


def test_multiseed_simulation_failure_stops_later_seeds():
    # 硬停止后只有实际执行的种子生成 testcase。
    tb = _tb("a", runs=[
        _run(5),
        _run(6, status="failed", reason="simulation_failed"),
    ])
    root = _parse(build_junit_xml(_report(tb, hit=1, total=1)))
    cases = root.find("testsuite").findall("testcase")
    assert [c.get("name") for c in cases] == ["a[seed=5]", "a[seed=6]"]
    assert cases[1].find("failure").get("type") == "SimulationFailure"


def test_multiseed_compilation_failed_empty_runs_is_single_same_name_case():
    tb = _tb("a", status="failed", reason="compilation_failed",
             diagnostics=["cc error"], runs=[])
    root = _parse(build_junit_xml(_report(tb, hit=0, total=0, ratio=None)))
    suite = root.find("testsuite")
    cases = suite.findall("testcase")
    assert len(cases) == 1
    assert cases[0].get("name") == "a"
    assert cases[0].find("failure").get("type") == "CompilationFailure"


def test_multiseed_skipped_empty_runs_is_single_same_name_case():
    tb = _tb("a", status="skipped", reason="skipped", required=False, runs=[])
    root = _parse(build_junit_xml(_report(tb, hit=1, total=1)))
    suite = root.find("testsuite")
    cases = suite.findall("testcase")
    assert len(cases) == 1
    assert cases[0].get("name") == "a"
    assert cases[0].find("skipped") is not None


def test_coverage_failure_suite_appended_last():
    report = _report(
        _tb("a"),
        met=False, hit=1, total=2, ratio=0.5, threshold=0.9,
    )
    root = _parse(build_junit_xml(report))
    suites = root.findall("testsuite")
    assert [s.get("name") for s in suites] == ["a", "coverage"]
    cov = suites[-1]
    assert cov.get("tests") == "1" and cov.get("failures") == "1"
    case = _cases(cov)["threshold"]
    assert case.get("classname") == "coverage"
    failure = case.find("failure")
    assert failure.get("type") == "CoverageFailure"
    body = failure.text
    assert "1/2" in body and "ratio=0.5" in body and "threshold=0.9" in body
    assert root.get("failures") == "1"


def test_coverage_ratio_none_rendered_stable():
    report = _report(
        _tb("a", status="failed", reason="compilation_failed", runs=[]),
        met=False, hit=0, total=0, ratio=None, threshold=1.0,
    )
    root = _parse(build_junit_xml(report))
    body = _cases(root.findall("testsuite")[-1])["threshold"] \
        .find("failure").text
    assert "ratio=None" in body


def test_baseline_mismatch_suite_after_coverage():
    report = _report(
        _tb("a"),
        met=False, hit=0, total=2, ratio=0.0,
        comparison={"passed": False, "mismatches": [1, 2, 3]},
    )
    root = _parse(build_junit_xml(report))
    suites = root.findall("testsuite")
    assert [s.get("name") for s in suites] == ["a", "coverage", "baseline"]
    base = suites[-1]
    case = _cases(base)["comparison"]
    failure = case.find("failure")
    assert failure.get("type") == "BaselineMismatch"
    assert "3 mismatches" in failure.text


def test_baseline_passed_produces_no_baseline_suite():
    report = _report(
        _tb("a"),
        comparison={"passed": True, "mismatches": []},
    )
    root = _parse(build_junit_xml(report))
    assert [s.get("name") for s in root.findall("testsuite")] == ["a"]


def test_stats_aggregated_from_testcases():
    report = _report(
        _tb("ok", runs=[_run(1), _run(2)]),
        _tb("mix", runs=[
            _run(1),
            _run(2, status="failed", reason="assertion_failed"),
        ]),
        _tb("opt", status="skipped", required=False),
    )
    root = _parse(build_junit_xml(report))
    assert root.get("tests") == "5"
    assert root.get("failures") == "1"
    assert root.get("skipped") == "1"
    assert root.get("errors") == "0"


def test_xml_escaping_of_attributes_and_body():
    report = _report(
        _tb('a&b<c>"\\\''),
        run_id='run & <run> "x"',
    )
    text = build_junit_xml(report)
    root = _parse(text)
    assert root.get("name") == 'run & <run> "x"'
    case = root.find("testsuite").find("testcase")
    assert case.get("name") == 'a&b<c>"\\\''


def test_failure_body_escaped():
    report = _report(
        _tb("a", status="failed", reason="assertion_failed",
            diagnostics=["line with <tag> & amp; quote \" done"]),
    )
    root = _parse(build_junit_xml(report))
    body = root.find(".//failure").text
    assert "<tag>" in body and "& amp;" in body and '"' in body


def test_unicode_is_wellformed_utf8():
    report = _report(_tb("测试台-α"), run_id="运行-1")
    text = build_junit_xml(report)
    text.encode("utf-8")  # 无编码异常
    root = _parse(text)
    assert root.get("name") == "运行-1"
    assert root.find("testsuite").get("name") == "测试台-α"


def test_deterministic_same_report_same_xml():
    report = _report(
        _tb("a", runs=[_run(1), _run(2, status="failed",
                                    reason="assertion_failed")]),
        met=False, hit=1, total=3, ratio=1 / 3,
        comparison={"passed": False, "mismatches": [{}]},
    )
    assert build_junit_xml(report) == build_junit_xml(report)


def test_no_timestamp_or_duration_in_output():
    text = build_junit_xml(_report(
        _tb("a", status="failed", reason="simulation_failed"),
        met=False, hit=0, total=1, ratio=0.0,
    ))
    for token in ("time=", "timestamp=", "duration="):
        assert token not in text
