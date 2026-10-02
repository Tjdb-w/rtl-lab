"""基线对比（schema v4 comparison）纯函数与基线加载校验单测。

不调用仿真器：报告以最小合法形状手工构造，另用 build_verification_report
抽查真实报告形状下的投影与字段顺序。
"""

import copy
import json
import os

import pytest

from rtl_lab.errors import InputError
from rtl_lab.report import (
    VERIFICATION_BASELINE_SCHEMA_VERSION,
    VERIFICATION_FORMAT,
    VERIFICATION_SCHEMA_VERSION,
    attach_verification_comparison,
    build_verification_comparison,
    build_verification_report,
    load_verification_baseline,
)


# ---------------- 最小报告构造 ----------------

def _tb_entry(name, *, status="passed", reason="passed", required=True,
              top=None, testbench=None, start="2026-01-01T00:00:00+00:00",
              end="2026-01-01T00:00:01+00:00", assertions=None,
              coverage=None, diagnostics=None):
    return {
        "name": name,
        "top": top or name,
        "testbench": testbench or f"tb/{name}.v",
        "status": status,
        "reason": reason,
        "required": required,
        "start_time": start,
        "end_time": end,
        "command": {"compile": ["iverilog"], "simulate": ["vvp"]},
        "diagnostics": diagnostics or [],
        "assertions": assertions or [],
        "coverage": coverage or [],
    }


def _as_entry(name, status="passed", fail_count=0):
    return {"name": name, "status": status, "fail_count": fail_count}


def _cov_entry(name, status="hit", hits=1, hit_testbenches=1):
    return {"name": name, "status": status, "hits": hits,
            "hit_testbenches": hit_testbenches}


def _report(*, tbs=(), assertions=(), coverage=()):
    return {
        "schema_version": VERIFICATION_SCHEMA_VERSION,
        "format": VERIFICATION_FORMAT,
        "result": "passed",
        "testbenches": [dict(t) for t in tbs],
        "assertions": [dict(a) for a in assertions],
        "coverage": [dict(c) for c in coverage],
    }


def _compare(baseline, current=None, *, baseline_path="/w/base.json",
             workdir="/w"):
    if current is None:
        current = copy.deepcopy(baseline)
    return build_verification_comparison(
        baseline_path=baseline_path, baseline=baseline,
        current=current, workdir=workdir,
    )


# ---------------- 无差异 ----------------

def test_identical_reports_pass_with_empty_mismatches():
    rep = _report(
        tbs=[_tb_entry("t1")],
        assertions=[_as_entry("a1")],
        coverage=[_cov_entry("c1")],
    )
    cmp_ = _compare(rep)
    assert cmp_["passed"] is True
    assert cmp_["mismatches"] == []
    assert cmp_["baseline"] == "base.json"


def test_timestamp_and_non_compared_tb_fields_ignored():
    baseline = _report(tbs=[_tb_entry("t1")])
    current = copy.deepcopy(baseline)
    tb = current["testbenches"][0]
    # 同名测试台仅比较 status/reason；其余字段变化不得产生差异。
    tb["start_time"] = "2026-09-09T09:09:09+00:00"
    tb["end_time"] = "2026-09-09T09:09:10+00:00"
    tb["top"] = "renamed_top"
    tb["testbench"] = "tb/elsewhere.v"
    tb["required"] = False
    tb["command"] = {"compile": ["other"], "simulate": []}
    tb["diagnostics"] = ["something changed"]
    cmp_ = _compare(baseline, current)
    assert cmp_["passed"] is True
    assert cmp_["mismatches"] == []


# ---------------- 测试台差异 ----------------

def test_testbench_missing_uses_full_baseline_record():
    t1 = _tb_entry("t1")
    t2 = _tb_entry("t2", status="failed", reason="simulation_failed")
    baseline = _report(tbs=[t1, t2])
    current = _report(tbs=[t1])
    cmp_ = _compare(baseline, current)
    assert cmp_["passed"] is False
    (m,) = cmp_["mismatches"]
    assert set(m.keys()) == {"kind", "name", "expected", "actual"}
    assert m["kind"] == "testbench.missing"
    assert m["name"] == "t2"
    assert m["expected"] == t2
    assert m["actual"] is None


def test_testbench_added_uses_full_current_record():
    t1 = _tb_entry("t1")
    t2 = _tb_entry("t2", status="failed", reason="compilation_failed")
    baseline = _report(tbs=[t1])
    current = _report(tbs=[t1, t2])
    cmp_ = _compare(baseline, current)
    (m,) = cmp_["mismatches"]
    assert m["kind"] == "testbench.added"
    assert m["name"] == "t2"
    assert m["expected"] is None
    assert m["actual"] == t2


def test_testbench_status_reason_are_separate_scalar_diffs():
    baseline = _report(tbs=[_tb_entry("t1")])
    current = _report(tbs=[
        _tb_entry("t1", status="failed", reason="assertion_failed")
    ])
    cmp_ = _compare(baseline, current)
    assert [(m["kind"], m["name"]) for m in cmp_["mismatches"]] == [
        ("testbench.status", "t1"),
        ("testbench.reason", "t1"),
    ]
    by = {m["kind"]: m for m in cmp_["mismatches"]}
    assert by["testbench.status"]["expected"] == "passed"
    assert by["testbench.status"]["actual"] == "failed"
    assert by["testbench.reason"]["expected"] == "passed"
    assert by["testbench.reason"]["actual"] == "assertion_failed"


def test_testbench_only_reason_changes():
    baseline = _report(tbs=[
        _tb_entry("t1", status="failed", reason="simulation_failed")
    ])
    current = _report(tbs=[
        _tb_entry("t1", status="failed", reason="compilation_failed")
    ])
    cmp_ = _compare(baseline, current)
    assert [(m["kind"], m["name"]) for m in cmp_["mismatches"]] == [
        ("testbench.reason", "t1")
    ]


# ---------------- 断言差异 ----------------

def test_assertion_missing_and_added_full_records():
    baseline = _report(assertions=[_as_entry("a1"), _as_entry("a2")])
    current = _report(assertions=[_as_entry("a1"), _as_entry("a3")])
    cmp_ = _compare(baseline, current)
    by = {(m["kind"], m["name"]): m for m in cmp_["mismatches"]}
    miss = by[("assertion.missing", "a2")]
    assert miss["expected"] == _as_entry("a2")
    assert miss["actual"] is None
    add = by[("assertion.added", "a3")]
    assert add["expected"] is None
    assert add["actual"] == _as_entry("a3")


def test_assertion_status_and_fail_count_separate_diffs():
    baseline = _report(assertions=[_as_entry("x", "passed", 0)])
    current = _report(assertions=[_as_entry("x", "failed", 2)])
    cmp_ = _compare(baseline, current)
    assert [m["kind"] for m in cmp_["mismatches"]] == [
        "assertion.status", "assertion.fail_count"
    ]
    by = {m["kind"]: m for m in cmp_["mismatches"]}
    assert (by["assertion.status"]["expected"],
            by["assertion.status"]["actual"]) == ("passed", "failed")
    assert (by["assertion.fail_count"]["expected"],
            by["assertion.fail_count"]["actual"]) == (0, 2)


def test_assertion_only_fail_count_changes_when_status_same():
    baseline = _report(assertions=[_as_entry("x", "failed", 1)])
    current = _report(assertions=[_as_entry("x", "failed", 3)])
    cmp_ = _compare(baseline, current)
    assert [(m["kind"], m["name"]) for m in cmp_["mismatches"]] == [
        ("assertion.fail_count", "x")
    ]


# ---------------- 覆盖率差异 ----------------

def test_coverage_missing_added_full_records():
    baseline = _report(coverage=[_cov_entry("c1")])
    current = _report(coverage=[_cov_entry("c2")])
    cmp_ = _compare(baseline, current)
    by = {(m["kind"], m["name"]): m for m in cmp_["mismatches"]}
    miss = by[("coverage.missing", "c1")]
    assert miss["expected"] == _cov_entry("c1")
    assert miss["actual"] is None
    add = by[("coverage.added", "c2")]
    assert add["expected"] is None
    assert add["actual"] == _cov_entry("c2")


def test_coverage_status_hits_and_hit_testbenches_diffs():
    baseline = _report(coverage=[
        _cov_entry("c1", "hit", hits=2, hit_testbenches=2)
    ])
    # 状态 hit -> missed：status/hits/hit_testbenches 全部不同。
    current = _report(coverage=[
        _cov_entry("c1", "missed", hits=0, hit_testbenches=0)
    ])
    cmp_ = _compare(baseline, current)
    assert [m["kind"] for m in cmp_["mismatches"]] == [
        "coverage.status", "coverage.hits", "coverage.hit_testbenches"
    ]


def test_coverage_only_hit_testbenches_changes():
    baseline = _report(coverage=[
        _cov_entry("c1", "hit", hits=2, hit_testbenches=2)
    ])
    current = _report(coverage=[
        _cov_entry("c1", "hit", hits=2, hit_testbenches=1)
    ])
    cmp_ = _compare(baseline, current)
    assert [(m["kind"], m["name"]) for m in cmp_["mismatches"]] == [
        ("coverage.hit_testbenches", "c1")
    ]


def test_coverage_unavailable_none_values_compared():
    baseline = _report(coverage=[
        _cov_entry("cm", "unavailable", hits=None, hit_testbenches=None)
    ])
    current = _report(coverage=[_cov_entry("cm", "hit", hits=1,
                                           hit_testbenches=1)])
    cmp_ = _compare(baseline, current)
    kinds = [m["kind"] for m in cmp_["mismatches"]]
    assert kinds == ["coverage.status", "coverage.hits",
                     "coverage.hit_testbenches"]
    status_m = next(m for m in cmp_["mismatches"]
                    if m["kind"] == "coverage.hits")
    assert status_m["expected"] is None
    assert status_m["actual"] == 1


# ---------------- 排序 ----------------

def test_mismatches_sorted_by_category_then_unicode_codepoint():
    # 基线独有的记录全部表现为 missing，便于只验证排序。
    baseline = _report(
        tbs=[_tb_entry("zb"), _tb_entry("aa")],
        assertions=[_as_entry("中"), _as_entry("a")],
        coverage=[_cov_entry("c_b"), _cov_entry("c_a")],
    )
    current = _report()
    cmp_ = _compare(baseline, current)
    assert [(m["kind"].split(".")[0], m["name"])
            for m in cmp_["mismatches"]] == [
        ("testbench", "aa"),
        ("testbench", "zb"),
        ("assertion", "a"),
        ("assertion", "中"),
        ("coverage", "c_a"),
        ("coverage", "c_b"),
    ]


def test_same_name_field_diffs_are_not_merged():
    baseline = _report(
        tbs=[_tb_entry("t1")],
        assertions=[_as_entry("x", "passed", 0)],
    )
    current = _report(
        tbs=[_tb_entry("t1", status="failed", reason="assertion_failed")],
        assertions=[_as_entry("x", "failed", 4)],
    )
    cmp_ = _compare(baseline, current)
    # 同名问题分别保留：t1 两条、x 条，类别顺序 testbench 先于 assertion。
    assert [(m["kind"], m["name"]) for m in cmp_["mismatches"]] == [
        ("testbench.status", "t1"),
        ("testbench.reason", "t1"),
        ("assertion.status", "x"),
        ("assertion.fail_count", "x"),
    ]
    assert all(set(m) == {"kind", "name", "expected", "actual"}
               for m in cmp_["mismatches"])


# ---------------- 基线路径脱敏 ----------------

def test_baseline_path_sanitized_inside_workdir():
    rep = _report()
    cmp_ = _compare(rep, baseline_path="/w/sub/base.json", workdir="/w")
    assert cmp_["baseline"] == "sub/base.json"


def test_baseline_path_sanitized_outside_workdir_is_basename():
    rep = _report()
    cmp_ = _compare(rep, baseline_path="/elsewhere/secret/base.json",
                    workdir="/w")
    assert cmp_["baseline"] == "base.json"


def test_relative_baseline_path_preserved():
    rep = _report()
    cmp_ = _compare(rep, baseline_path="reports/base.json", workdir="/w")
    assert cmp_["baseline"] == "reports/base.json"


# ---------------- 真实构建报告形状 ----------------

def _built_outcome(name, *, status="passed", reason="passed",
                   assertions=None, coverage=None):
    return {
        "name": name,
        "top": name,
        "testbench_file": f"/w/tb/{name}.v",
        "status": status,
        "reason": reason,
        "required": True,
        "start_time": "2026-01-01T00:00:00+00:00",
        "end_time": "2026-01-01T00:00:01+00:00",
        "diagnostics": [],
        "assertions": assertions if assertions is not None else {
            "a1": {"status": "passed", "fail_count": 0}
        },
        "coverage": coverage if coverage is not None else {"c1": 1},
        "commands": {"compile": ["/usr/bin/iverilog"],
                     "simulate": ["/usr/bin/vvp", "sim.vvp"]},
    }


def _build(outcomes):
    data, _ = build_verification_report(
        run_id="rid", sources=["/w/d.v"],
        testbench_files=[o["testbench_file"] for o in outcomes],
        coverage_config={"threshold": 1.0, "points": []},
        duration="100ns",
        compile_command=outcomes[0]["commands"]["compile"],
        simulate_command=outcomes[0]["commands"]["simulate"],
        outcomes=outcomes,
        generated_at="2026-01-01T00:00:02+00:00",
        workdir="/w",
    )
    return data


def test_missing_full_record_matches_real_built_tb_entry():
    # t2 不贡献独有断言/覆盖率聚合，使其移除只产生测试台缺失一条差异。
    baseline = _build([
        _built_outcome("t1"),
        _built_outcome("t2", assertions={}, coverage={}),
    ])
    current = _build([_built_outcome("t1")])
    cmp_ = build_verification_comparison(
        baseline_path="/w/base.json", baseline=baseline, current=current,
        workdir="/w",
    )
    (m,) = cmp_["mismatches"]
    assert m["kind"] == "testbench.missing"
    # 完整记录即真实报告中的脱敏条目（恰为投影的 12 个字段）。
    assert m["expected"] == baseline["testbenches"][1]


# ---------------- 追加 comparison / 盖 v4 戳 ----------------

def test_attach_comparison_appends_after_v3_fields_and_bumps_v4():
    data = _build([_built_outcome("t1")])
    v3_keys = list(data.keys())
    comparison = {"baseline": "base.json", "passed": True, "mismatches": []}
    attach_verification_comparison(data, comparison)
    assert data["schema_version"] == VERIFICATION_BASELINE_SCHEMA_VERSION
    assert list(data.keys()) == v3_keys + ["comparison"]
    assert data["comparison"] is comparison


def test_attach_comparison_with_mismatches_forces_failed():
    data = _build([_built_outcome("t1")])
    assert data["result"] == "passed"
    attach_verification_comparison(
        data, {"baseline": "b.json", "passed": False,
               "mismatches": [{"kind": "x", "name": "n",
                               "expected": 1, "actual": 2}]}
    )
    assert data["schema_version"] == 4
    assert data["result"] == "failed"


def test_attach_comparison_without_mismatches_keeps_current_result():
    data = _build([_built_outcome(
        "t1", status="failed", reason="simulation_failed",
        assertions={}, coverage={},
    )])
    assert data["result"] == "failed"
    attach_verification_comparison(
        data, {"baseline": "b.json", "passed": True, "mismatches": []}
    )
    # 无差异时按当前检查决定 result：当前 failed 仍 failed。
    assert data["result"] == "failed"


# ---------------- load_verification_baseline ----------------

def _write(tmp_path, name, obj):
    p = tmp_path / name
    if isinstance(obj, str):
        p.write_text(obj, encoding="utf-8")
    else:
        p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_load_accepts_v3_and_v4_reports(tmp_path):
    for version in (VERIFICATION_SCHEMA_VERSION,
                    VERIFICATION_BASELINE_SCHEMA_VERSION):
        path = _write(tmp_path, f"v{version}.json",
                      {"schema_version": version,
                       "format": VERIFICATION_FORMAT})
        data = load_verification_baseline(path)
        assert data["schema_version"] == version


def test_load_missing_path_raises(tmp_path):
    with pytest.raises(InputError):
        load_verification_baseline(str(tmp_path / "nope.json"))


def test_load_directory_raises(tmp_path):
    with pytest.raises(InputError):
        load_verification_baseline(str(tmp_path))


def test_load_empty_path_raises():
    with pytest.raises(InputError):
        load_verification_baseline("   ")


def test_load_malformed_json_raises(tmp_path):
    path = _write(tmp_path, "bad.json", "{not json")
    with pytest.raises(InputError):
        load_verification_baseline(path)


def test_load_non_object_json_raises(tmp_path):
    path = _write(tmp_path, "arr.json", [1, 2, 3])
    with pytest.raises(InputError):
        load_verification_baseline(path)


def test_load_run_regress_report_wrong_format_raises(tmp_path):
    # schema v1/v2 报告没有 verification format 标识。
    path = _write(tmp_path, "v1.json",
                  {"schema_version": 1, "status": "passed"})
    with pytest.raises(InputError):
        load_verification_baseline(path)
    path2 = _write(tmp_path, "v2.json",
                   {"schema_version": 2, "seeds": [0]})
    with pytest.raises(InputError):
        load_verification_baseline(path2)


def test_load_unsupported_schema_version_raises(tmp_path):
    path = _write(tmp_path, "future.json",
                  {"schema_version": 99, "format": VERIFICATION_FORMAT})
    with pytest.raises(InputError):
        load_verification_baseline(path)


@pytest.mark.skipif(os.geteuid() == 0, reason="root 仍可读取 chmod 000 文件")
def test_load_unreadable_file_raises(tmp_path):
    path = tmp_path / "noread.json"
    path.write_text(json.dumps(
        {"schema_version": 3, "format": VERIFICATION_FORMAT}
    ), encoding="utf-8")
    os.chmod(path, 0o000)
    try:
        with pytest.raises(InputError):
            load_verification_baseline(str(path))
    finally:
        os.chmod(path, 0o644)
