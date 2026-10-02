"""统一验证报告（schema v3）构建与聚合单测（不调用仿真器）。"""

import pytest

from rtl_lab.report import (
    TB_STATUSES,
    VERIFICATION_FORMAT,
    VERIFICATION_SCHEMA_VERSION,
    build_verification_report,
    ensure_report_writable,
)

def _outcome(name, *, status="passed", reason="passed", required=True,
             top=None, testbench_file=None, assertions=None, coverage=None,
             start="2026-01-01T00:00:00+00:00",
             end="2026-01-01T00:00:01+00:00",
             compile_argv=("/usr/bin/iverilog", "-o", "/w/sim.vvp", "/w/d.v"),
             simulate_argv=("/usr/bin/vvp", "sim.vvp", "+SEED=0")):
    return {
        "name": name,
        "top": top or name,
        "testbench_file": testbench_file or f"/w/tb/{name}.v",
        "status": status,
        "reason": reason,
        "required": required,
        "start_time": start,
        "end_time": end,
        "diagnostics": [],
        "assertions": assertions if assertions is not None else {
            "a1": {"status": "passed", "fail_count": 0}
        },
        "coverage": coverage if coverage is not None else {"c1": 1},
        "commands": {
            "compile": list(compile_argv),
            "simulate": list(simulate_argv),
        },
    }


def _build(outcomes, *, threshold=1.0, points=None, run_id="rid",
           sources=("/w/d.v",), tb_files=None):
    if tb_files is None:
        tb_files = [o["testbench_file"] for o in outcomes]
    return build_verification_report(
        run_id=run_id,
        sources=list(sources),
        testbench_files=tb_files,
        coverage_config={"threshold": threshold, "points": points or []},
        duration="100ns",
        compile_command=(
            outcomes[0]["commands"]["compile"] if outcomes else []
        ),
        simulate_command=(
            outcomes[0]["commands"]["simulate"] if outcomes else []
        ),
        outcomes=outcomes,
        generated_at="2026-01-01T00:00:02+00:00",
        workdir="/w",
        known_paths=(),
    )


def test_shape_and_version_markers():
    data, result = _build([_outcome("t1")])
    assert data["schema_version"] == VERIFICATION_SCHEMA_VERSION
    assert data["format"] == VERIFICATION_FORMAT
    assert list(data.keys()) == [
        "schema_version", "format", "run_id", "result", "generated_at",
        "tool", "config", "compilation", "testbenches", "testbench_summary",
        "assertion_summary", "assertions", "coverage_summary", "coverage",
        "skipped_required",
    ]
    assert result == "passed"
    assert data["run_id"] == "rid"
    assert data["result"] == "passed"
    assert data["generated_at"] == "2026-01-01T00:00:02+00:00"
    tb = data["testbenches"][0]
    assert list(tb.keys()) == [
        "name", "top", "testbench", "status", "reason", "required",
        "start_time", "end_time", "command", "diagnostics", "assertions",
        "coverage",
    ]
    assert tb["testbench"] == "tb/t1.v"


def test_compilation_records_actual_compiled_sources_order():
    data, _ = _build(
        [_outcome("t1")], sources=("/w/src/z.v", "/w/src/a.v")
    )
    assert data["compilation"]["sources"] == ["src/z.v", "src/a.v"]


def test_config_summary_fields():
    data, _ = _build([_outcome("t1")])
    assert data["config"]["duration"] == "100ns"
    assert data["config"]["coverage"] == {"threshold": 1.0, "points": []}
    # 顶层代表性命令已脱敏。
    assert data["config"]["compile"][0] == "iverilog"
    assert data["config"]["simulate"][0] == "vvp"


def test_counts_all_pass():
    data, result = _build([
        _outcome("t1", assertions={
            "a": {"status": "passed", "fail_count": 0},
            "b": {"status": "passed", "fail_count": 0},
        }, coverage={"c1": 1, "c2": 3}),
        _outcome("t2", assertions={
            "a": {"status": "passed", "fail_count": 0},
        }, coverage={"c1": 2}),
    ])
    assert result == "passed"
    assert data["testbench_summary"] == {
        "passed": 2, "failed": 0, "skipped": 0
    }
    # 断言按首次出现顺序合并，fail_count 跨台求和。
    assert [a["name"] for a in data["assertions"]] == ["a", "b"]
    assert data["assertion_summary"] == {
        "total": 2, "passed": 2, "failed": 0, "fail_count": 0
    }
    # 覆盖率点按首次出现顺序：c1 在 t1/t2 均命中，c2 仅 t1。
    assert [c["name"] for c in data["coverage"]] == ["c1", "c2"]
    by = {c["name"]: c for c in data["coverage"]}
    assert by["c1"] == {
        "name": "c1", "status": "hit", "hits": 3, "hit_testbenches": 2
    }
    assert by["c2"] == {
        "name": "c2", "status": "hit", "hits": 3, "hit_testbenches": 1
    }
    summary = data["coverage_summary"]
    assert summary["total_points"] == 2
    assert summary["hit_points"] == 2
    assert summary["ratio"] == 1.0
    assert summary["met"] is True
    assert summary["unavailable_points"] == []


def test_single_testbench_failure_makes_failed():
    data, result = _build([
        _outcome("t1"),
        _outcome("t2", status="failed", reason="simulation_failed",
                 assertions={}, coverage={}),
    ])
    assert result == "failed"
    assert data["result"] == "failed"
    assert data["testbench_summary"] == {
        "passed": 1, "failed": 1, "skipped": 0
    }
    assert [t["name"] for t in data["testbenches"]] == ["t1", "t2"]


def test_assertion_failure_aggregation_and_fail_count():
    data, result = _build([
        _outcome("t1", assertions={
            "x": {"status": "failed", "fail_count": 2},
        }, coverage={"c1": 1}),
        _outcome("t2", assertions={
            "x": {"status": "passed", "fail_count": 0},
        }, coverage={"c1": 1}),
    ])
    assert result == "failed"
    assert data["assertions"] == [
        {"name": "x", "status": "failed", "fail_count": 2}
    ]
    assert data["assertion_summary"]["failed"] == 1
    assert data["assertion_summary"]["fail_count"] == 2


def test_skipped_optional_does_not_fail_conclusion():
    data, result = _build([
        _outcome("t1"),
        _outcome("t2", status="skipped", reason="skipped", required=False,
                 assertions={}, coverage={}),
    ])
    assert result == "passed"
    assert data["testbench_summary"] == {
        "passed": 1, "failed": 0, "skipped": 1
    }
    assert data["skipped_required"] == []


def test_skipped_required_fails_conclusion():
    data, result = _build([
        _outcome("t1"),
        _outcome("t2", status="skipped", reason="skipped", required=True,
                 assertions={}, coverage={}),
    ])
    assert result == "failed"
    assert data["skipped_required"] == ["t2"]


def test_coverage_below_threshold_fails():
    data, result = _build(
        [_outcome("t1", coverage={"c1": 1, "c2": 0})],
        threshold=0.6,
    )
    # 2 个点，命中 1 个 => ratio 0.5 < 0.6 => failed。
    assert data["coverage_summary"]["ratio"] == 0.5
    assert result == "failed"
    assert data["coverage_summary"]["met"] is False


def test_coverage_threshold_boundary_inclusive():
    data, result = _build(
        [_outcome("t1", coverage={"c1": 1, "c2": 0})], threshold=0.5,
    )
    assert data["coverage_summary"]["ratio"] == 0.5
    assert result == "passed"
    assert data["coverage_summary"]["met"] is True


def test_missed_point_has_zero_hits():
    data, _ = _build([_outcome("t1", coverage={"c1": 1, "c2": 0})])
    by = {c["name"]: c for c in data["coverage"]}
    assert by["c2"]["status"] == "missed"
    assert by["c2"]["hits"] == 0
    assert by["c2"]["hit_testbenches"] == 0


def test_configured_point_without_stats_is_unavailable_counts_in_denom():
    data, result = _build(
        [_outcome("t1", coverage={"c1": 1})],
        threshold=1.0, points=["c1", "missing"],
    )
    by = {c["name"]: c for c in data["coverage"]}
    assert by["missing"] == {
        "name": "missing", "status": "unavailable",
        "hits": None, "hit_testbenches": None,
    }
    assert data["coverage_summary"]["total_points"] == 2
    assert data["coverage_summary"]["hit_points"] == 1
    assert data["coverage_summary"]["ratio"] == 0.5
    assert data["coverage_summary"]["unavailable_points"] == ["missing"]
    assert result == "failed"


def test_order_stable_independent_of_dict_iteration(tmp_path):
    # 以不同插入顺序构建，断言报告数组顺序只取决于测试台选择顺序。
    o1 = _outcome("beta", assertions={
        "z": {"status": "passed", "fail_count": 0},
        "a": {"status": "passed", "fail_count": 0},
    }, coverage={"zz": 1, "aa": 1})
    o2 = _outcome("alpha", assertions={
        "m": {"status": "passed", "fail_count": 0},
    }, coverage={"mm": 1})
    data, _ = _build([o1, o2])
    assert [t["name"] for t in data["testbenches"]] == ["beta", "alpha"]
    assert [a["name"] for a in data["assertions"]] == ["z", "a", "m"]
    assert [c["name"] for c in data["coverage"]] == ["zz", "aa", "mm"]


def test_diagnostics_sanitized():
    o = _outcome("t1")
    o["diagnostics"] = ["see /tmp/secret/x.v and /w/d.v"]
    data, _ = _build([o])
    blob = data["testbenches"][0]["diagnostics"][0]
    assert "/tmp/secret" not in blob and "x.v" in blob
    assert "/w/d.v" not in blob and "d.v" in blob


def test_duplicate_tb_name_raises():
    with pytest.raises(RuntimeError):
        _build([_outcome("same"), _outcome("same")])


def test_empty_outcomes_raises():
    with pytest.raises(RuntimeError):
        _build([])


def test_zero_coverage_points_is_failed_at_builder_without_raising():
    # 构建器本身不裁决“运行是否必须有覆盖率”（该运行级规则在 verify 中）；
    # 零点时 ratio 为 None、met 为 False，结论 failed。
    data, result = _build([_outcome("t1", coverage={})])
    assert data["coverage_summary"]["total_points"] == 0
    assert data["coverage_summary"]["ratio"] is None
    assert data["coverage_summary"]["met"] is False
    assert result == "failed"


def test_illegal_status_raises():
    o = _outcome("t1")
    o["status"] = "weird"
    with pytest.raises(RuntimeError):
        _build([o])


def test_status_enum_domain():
    assert TB_STATUSES == ("passed", "failed", "skipped")


def test_ensure_report_writable_existing_file_ok(tmp_path):
    f = tmp_path / "r.json"
    f.write_text("{}")
    ensure_report_writable(str(f))  # 不抛异常即通过


def test_ensure_report_writable_directory_rejected(tmp_path):
    with pytest.raises(OSError):
        ensure_report_writable(str(tmp_path))


def test_ensure_report_writable_nonexistent_under_existing_dir(tmp_path):
    target = str(tmp_path / "nested" / "r.json")
    ensure_report_writable(target)  # 最近存在祖先可写即通过


def test_ensure_report_writable_creates_nothing(tmp_path):
    target = tmp_path / "r.json"
    ensure_report_writable(str(target))
    assert not target.exists()  # 校验本身不落盘
