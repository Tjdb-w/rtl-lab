"""基线加载与结构化对比单测（不调用仿真器）。"""

import json

import pytest

from rtl_lab.baseline import (
    compare_verification_reports,
    load_baseline_report,
)
from rtl_lab.errors import InputError


def _tb(name, *, status="passed", reason="passed"):
    return {
        "name": name,
        "top": name,
        "testbench": f"tb/{name}.v",
        "status": status,
        "reason": reason,
        "required": True,
        "start_time": "2026-01-01T00:00:00+00:00",
        "end_time": "2026-01-01T00:00:01+00:00",
        "command": {"compile": ["iverilog"], "simulate": ["vvp"]},
        "diagnostics": [],
        "assertions": [],
        "coverage": [],
    }


def _report(*, testbenches=(), assertions=(), coverage=()):
    return {
        "schema_version": 3,
        "format": "rtl-lab-verification",
        "run_id": "rid",
        "result": "passed",
        "testbenches": list(testbenches),
        "assertions": list(assertions),
        "coverage": list(coverage),
    }


def _compare(current, baseline, baseline_path="/w/base.json", workdir="/w"):
    return compare_verification_reports(
        current, baseline, baseline_path=baseline_path, workdir=workdir
    )


# ---- load_baseline_report ----


def test_load_baseline_ok_v3(tmp_path):
    f = tmp_path / "base.json"
    f.write_text(json.dumps(_report()), encoding="utf-8")
    data = load_baseline_report(str(f))
    assert data["format"] == "rtl-lab-verification"


def test_load_baseline_ok_v4(tmp_path):
    report = _report()
    report["schema_version"] = 4
    report["comparison"] = {
        "baseline": "older.json", "passed": True, "mismatches": [],
    }
    f = tmp_path / "base.json"
    f.write_text(json.dumps(report), encoding="utf-8")
    assert load_baseline_report(str(f))["schema_version"] == 4


def test_load_baseline_missing_file(tmp_path):
    with pytest.raises(InputError):
        load_baseline_report(str(tmp_path / "nope.json"))


def test_load_baseline_directory_rejected(tmp_path):
    with pytest.raises(InputError):
        load_baseline_report(str(tmp_path))


def test_load_baseline_invalid_json(tmp_path):
    f = tmp_path / "base.json"
    f.write_text("{not json", encoding="utf-8")
    with pytest.raises(InputError):
        load_baseline_report(str(f))


def test_load_baseline_not_verification_report(tmp_path):
    f = tmp_path / "base.json"
    # schema v1 单次运行报告：没有 format 标识。
    f.write_text(json.dumps({"schema_version": 1, "tool": "x"}),
                 encoding="utf-8")
    with pytest.raises(InputError):
        load_baseline_report(str(f))


def test_load_baseline_non_dict_json(tmp_path):
    f = tmp_path / "base.json"
    f.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(InputError):
        load_baseline_report(str(f))


def test_load_baseline_unsupported_version(tmp_path):
    report = _report()
    report["schema_version"] = 5
    f = tmp_path / "base.json"
    f.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(InputError):
        load_baseline_report(str(f))


# ---- compare_verification_reports ----


def test_compare_identical_reports_pass():
    current = _report(
        testbenches=[_tb("t1")],
        assertions=[{"name": "a1", "status": "passed", "fail_count": 0}],
        coverage=[{
            "name": "c1", "status": "hit", "hits": 2, "hit_testbenches": 1,
        }],
    )
    comparison = _compare(current, _report(
        testbenches=[_tb("t1")],
        assertions=[{"name": "a1", "status": "passed", "fail_count": 0}],
        coverage=[{
            "name": "c1", "status": "hit", "hits": 2, "hit_testbenches": 1,
        }],
    ))
    assert comparison["passed"] is True
    assert comparison["mismatches"] == []
    assert comparison["baseline"] == "base.json"


def test_compare_baseline_path_outside_workdir_keeps_basename():
    comparison = _compare(
        _report(), _report(), baseline_path="/elsewhere/dir/base.json",
    )
    assert comparison["baseline"] == "base.json"


def test_compare_testbench_missing_and_added():
    current = _report(testbenches=[_tb("new_tb")])
    baseline = _report(testbenches=[_tb("old_tb")])
    comparison = _compare(current, baseline)
    assert comparison["passed"] is False
    assert comparison["mismatches"] == [
        {
            "kind": "testbench_added",
            "name": "new_tb",
            "expected": None,
            "actual": current["testbenches"][0],
        },
        {
            "kind": "testbench_missing",
            "name": "old_tb",
            "expected": baseline["testbenches"][0],
            "actual": None,
        },
    ]


def test_compare_testbench_status_and_reason_kept_separately():
    current = _report(testbenches=[
        _tb("t1", status="failed", reason="simulation_failed"),
    ])
    baseline = _report(testbenches=[_tb("t1")])
    comparison = _compare(current, baseline)
    assert comparison["mismatches"] == [
        {
            "kind": "testbench_status", "name": "t1",
            "expected": "passed", "actual": "failed",
        },
        {
            "kind": "testbench_reason", "name": "t1",
            "expected": "passed", "actual": "simulation_failed",
        },
    ]


def test_compare_assertion_status_and_fail_count():
    current = _report(assertions=[
        {"name": "a1", "status": "failed", "fail_count": 3},
    ])
    baseline = _report(assertions=[
        {"name": "a1", "status": "passed", "fail_count": 0},
    ])
    comparison = _compare(current, baseline)
    assert comparison["mismatches"] == [
        {
            "kind": "assertion_status", "name": "a1",
            "expected": "passed", "actual": "failed",
        },
        {
            "kind": "assertion_fail_count", "name": "a1",
            "expected": 0, "actual": 3,
        },
    ]


def test_compare_assertion_missing_and_added():
    current = _report(assertions=[
        {"name": "a2", "status": "passed", "fail_count": 0},
    ])
    baseline = _report(assertions=[
        {"name": "a1", "status": "passed", "fail_count": 0},
    ])
    comparison = _compare(current, baseline)
    kinds = {(m["kind"], m["name"]) for m in comparison["mismatches"]}
    assert kinds == {
        ("assertion_added", "a2"), ("assertion_missing", "a1"),
    }
    added = next(m for m in comparison["mismatches"] if m["name"] == "a2")
    assert added["expected"] is None
    assert added["actual"] == current["assertions"][0]
    missing = next(m for m in comparison["mismatches"] if m["name"] == "a1")
    assert missing["expected"] == baseline["assertions"][0]
    assert missing["actual"] is None


def test_compare_coverage_status_hits_and_hit_testbenches():
    current = _report(coverage=[{
        "name": "c1", "status": "missed", "hits": 0, "hit_testbenches": 0,
    }])
    baseline = _report(coverage=[{
        "name": "c1", "status": "hit", "hits": 5, "hit_testbenches": 2,
    }])
    comparison = _compare(current, baseline)
    assert comparison["mismatches"] == [
        {"kind": "coverage_status", "name": "c1",
         "expected": "hit", "actual": "missed"},
        {"kind": "coverage_hits", "name": "c1",
         "expected": 5, "actual": 0},
        {"kind": "coverage_hit_testbenches", "name": "c1",
         "expected": 2, "actual": 0},
    ]


def test_compare_coverage_unavailable_null_scalars():
    current = _report(coverage=[{
        "name": "c1", "status": "unavailable",
        "hits": None, "hit_testbenches": None,
    }])
    baseline = _report(coverage=[{
        "name": "c1", "status": "hit", "hits": 1, "hit_testbenches": 1,
    }])
    comparison = _compare(current, baseline)
    by_kind = {m["kind"]: m for m in comparison["mismatches"]}
    assert by_kind["coverage_hits"]["expected"] == 1
    assert by_kind["coverage_hits"]["actual"] is None
    assert by_kind["coverage_hit_testbenches"]["actual"] is None


def test_compare_category_order_and_unicode_name_order():
    # 类别顺序 testbench < assertion < coverage；类别内按码点排序
    # （大写 'Z' 码点小于小写 'a'）。
    current = _report(
        testbenches=[_tb("b"), _tb("Z")],
        assertions=[
            {"name": "ä", "status": "failed", "fail_count": 1},
            {"name": "a", "status": "failed", "fail_count": 1},
        ],
        coverage=[{
            "name": "c1", "status": "hit", "hits": 2, "hit_testbenches": 1,
        }],
    )
    baseline = _report(
        testbenches=[_tb("a"), _tb("Z", status="failed",
                                   reason="assertion_failed")],
        assertions=[
            {"name": "ä", "status": "passed", "fail_count": 0},
            {"name": "a", "status": "passed", "fail_count": 0},
        ],
        coverage=[{
            "name": "c1", "status": "hit", "hits": 1, "hit_testbenches": 1,
        }],
    )
    comparison = _compare(current, baseline)
    assert [(m["kind"], m["name"]) for m in comparison["mismatches"]] == [
        ("testbench_status", "Z"),
        ("testbench_reason", "Z"),
        ("testbench_missing", "a"),
        ("testbench_added", "b"),
        ("assertion_status", "a"),
        ("assertion_fail_count", "a"),
        ("assertion_status", "ä"),
        ("assertion_fail_count", "ä"),
        ("coverage_hits", "c1"),
    ]


def test_compare_ignores_uncompared_fields():
    # 时间戳、诊断、命令等不参与对比；仅对齐字段决定差异。
    tb_current = _tb("t1")
    tb_current["start_time"] = "2026-02-02T00:00:00+00:00"
    tb_current["diagnostics"] = ["something"]
    current = _report(testbenches=[tb_current])
    baseline = _report(testbenches=[_tb("t1")])
    comparison = _compare(current, baseline)
    assert comparison["passed"] is True
    assert comparison["mismatches"] == []
