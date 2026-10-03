"""多种子矩阵统一验证报告（schema v5）构建与聚合单测（不调用仿真器）。"""

import pytest

from rtl_lab.report import (
    RUN_REASONS,
    RUN_STATUSES,
    VERIFICATION_MULTISEED_SCHEMA_VERSION,
    build_verification_report,
)

_SEEDS = (1, 2, 3)
_COMPILE = ("/usr/bin/iverilog", "-o", "/w/sim.vvp", "/w/d.v")


def _run(seed, *, status="passed", reason="passed", assertions=None,
         coverage=None, simulate=None, diagnostics=None):
    return {
        "seed": seed,
        "status": status,
        "reason": reason,
        "diagnostics": list(diagnostics or []),
        "assertions": assertions if assertions is not None else {
            "a1": {"status": "passed", "fail_count": 0},
        },
        "coverage": coverage if coverage is not None else {"c1": 1},
        "command": {
            "simulate": list(simulate) if simulate is not None
            else ["/usr/bin/vvp", "sim.vvp", f"+SEED={seed}"],
        },
    }


def _moutcome(name, *, status="passed", reason="passed", required=True,
              seeds=_SEEDS, runs=None, assertions=None, coverage=None,
              diagnostics=None, compile_argv=_COMPILE,
              simulate_argv=("/usr/bin/vvp", "sim.vvp", "+SEED=1")):
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
        "assertions": assertions if assertions is not None else {},
        "coverage": coverage if coverage is not None else {},
        "commands": {
            "compile": list(compile_argv),
            "simulate": list(simulate_argv),
        },
        "seeds": list(seeds),
        "runs": (
            [_run(s) for s in seeds] if runs is None and status == "passed"
            else list(runs or [])
        ),
    }


def _build(outcomes, *, threshold=1.0, points=None, seeds=_SEEDS):
    return build_verification_report(
        run_id="rid",
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
        seeds=list(seeds),
    )


def test_v5_markers_and_entry_shape():
    data, result = _build([_moutcome("t1")])
    assert data["schema_version"] == VERIFICATION_MULTISEED_SCHEMA_VERSION == 5
    assert data["format"] == "rtl-lab-verification"
    assert result == "passed"
    tb = data["testbenches"][0]
    # 既有 12 个字段保持不变，seeds/runs 追加在最后。
    assert list(tb.keys()) == [
        "name", "top", "testbench", "status", "reason", "required",
        "start_time", "end_time", "command", "diagnostics", "assertions",
        "coverage", "seeds", "runs",
    ]
    assert tb["seeds"] == [1, 2, 3]
    assert [r["seed"] for r in tb["runs"]] == [1, 2, 3]
    # 每个 run 恰含七个字段，simulate 为该种子的仿真 argv。
    for run in tb["runs"]:
        assert list(run.keys()) == [
            "seed", "status", "reason", "diagnostics", "assertions",
            "coverage", "simulate",
        ]
        assert run["simulate"][-1] == f"+SEED={run['seed']}"
        assert run["status"] == "passed" and run["reason"] == "passed"


def test_all_seeds_pass_tb_passed():
    data, result = _build([_moutcome("t1")])
    tb = data["testbenches"][0]
    assert tb["status"] == "passed" and tb["reason"] == "passed"
    assert data["testbench_summary"] == {
        "passed": 1, "failed": 0, "skipped": 0
    }
    assert result == "passed"


def test_assertion_aggregated_across_seeds_fail_count_sums():
    runs = [
        _run(1, assertions={"a": {"status": "failed", "fail_count": 2}},
             coverage={"c1": 1}),
        _run(2, status="failed", reason="assertion_failed",
             assertions={"a": {"status": "failed", "fail_count": 1}},
             coverage={"c1": 0}),
        _run(3, assertions={"a": {"status": "passed", "fail_count": 0}},
             coverage={"c1": 2}),
    ]
    o = _moutcome("t1", status="failed", reason="assertion_failed", runs=runs)
    data, result = _build([o])
    tb = data["testbenches"][0]
    assert tb["reason"] == "assertion_failed"
    # 条目级断言：按名取跨种子终态，fail_count 累加。
    assert tb["assertions"] == [
        {"name": "a", "status": "failed", "fail_count": 3}
    ]
    # 条目级 coverage：hits 跨种子累加。
    assert tb["coverage"] == [{"name": "c1", "hits": 3}]
    # 顶层聚合一致。
    assert data["assertions"] == [
        {"name": "a", "status": "failed", "fail_count": 3}
    ]
    by = {c["name"]: c for c in data["coverage"]}
    assert by["c1"] == {
        "name": "c1", "status": "hit", "hits": 3, "hit_testbenches": 1
    }
    assert result == "failed"
    assert data["assertion_summary"] == {
        "total": 1, "passed": 0, "failed": 1, "fail_count": 3
    }


def test_seed_order_preserved_in_runs():
    runs = [
        _run(7, coverage={"c1": 1}),
        _run(0, assertions={"a2": {"status": "passed", "fail_count": 0}},
             coverage={"c2": 1}),
    ]
    o = _moutcome("t1", seeds=(7, 0), runs=runs)
    data, _ = _build([o], seeds=(7, 0))
    tb = data["testbenches"][0]
    assert [r["seed"] for r in tb["runs"]] == [7, 0]
    # 断言/覆盖率按种子执行的首次出现顺序排列。
    assert [a["name"] for a in tb["assertions"]] == ["a1", "a2"]
    assert [c["name"] for c in tb["coverage"]] == ["c1", "c2"]


def test_simulation_failed_runs_are_seed_prefix():
    # 种子 2 仿真非零退出：停止该台后续种子，runs 仅为种子列表前缀。
    runs = [
        _run(1),
        _run(2, status="failed", reason="simulation_failed",
             assertions={}, coverage={}),
    ]
    o = _moutcome("t1", status="failed", reason="simulation_failed",
                  runs=runs)
    data, result = _build([o])
    tb = data["testbenches"][0]
    assert [r["seed"] for r in tb["runs"]] == [1, 2]
    assert tb["seeds"] == [1, 2, 3]  # 列表完整保留
    assert tb["reason"] == "simulation_failed"
    assert result == "failed"


def test_incomplete_statistics_run_fails_tb():
    runs = [
        _run(1),
        _run(2, status="failed", reason="incomplete_statistics",
             assertions={"a1": {"status": "passed", "fail_count": 0}},
             coverage={}),
        _run(3),
    ]
    o = _moutcome("t1", status="failed", reason="incomplete_statistics",
                  runs=runs)
    data, result = _build([o])
    tb = data["testbenches"][0]
    assert tb["reason"] == "incomplete_statistics"
    assert [r["reason"] for r in tb["runs"]] == [
        "passed", "incomplete_statistics", "passed"
    ]
    assert result == "failed"


def test_compilation_failed_has_seeds_but_empty_runs():
    o = _moutcome("t1", status="failed", reason="compilation_failed",
                  runs=[], assertions={}, coverage={}, simulate_argv=())
    data, result = _build([o])
    tb = data["testbenches"][0]
    assert tb["seeds"] == [1, 2, 3]
    assert tb["runs"] == []
    assert tb["command"]["simulate"] == []
    assert tb["reason"] == "compilation_failed"
    assert result == "failed"
    assert data["coverage_summary"]["total_points"] == 0


def test_skipped_tb_has_seeds_and_empty_runs():
    o = _moutcome("t2", status="skipped", reason="skipped", required=False,
                  runs=[], assertions={}, coverage={})
    data, result = _build([_moutcome("t1"), o])
    t2 = data["testbenches"][1]
    assert t2["seeds"] == [1, 2, 3]
    assert t2["runs"] == []
    assert result == "passed"  # 非必测跳过不影响结论


def test_coverage_hit_testbenches_deduped_across_seeds():
    # 同一测试台的两个种子都命中 c1：hits 累加，但 hit_testbenches 只计 1。
    runs = [
        _run(1, coverage={"c1": 2}),
        _run(2, coverage={"c1": 3}),
        _run(3, coverage={"c1": 0}),
    ]
    o1 = _moutcome("t1", runs=runs)
    o2 = _moutcome("t2", runs=[
        _run(1, assertions={"a1": {"status": "passed", "fail_count": 0}},
             coverage={"c1": 1}),
        _run(2, assertions={"a1": {"status": "passed", "fail_count": 0}},
             coverage={"c1": 0}),
        _run(3, assertions={"a1": {"status": "passed", "fail_count": 0}},
             coverage={"c1": 0}),
    ])
    data, _ = _build([o1, o2])
    by = {c["name"]: c for c in data["coverage"]}
    # hits=2+3+0+1 = 6；跨 2 个测试台命中。
    assert by["c1"]["hits"] == 6
    assert by["c1"]["hit_testbenches"] == 2


def test_coverage_ratio_is_hit_points_over_total_points():
    # c1 至少一个种子命中；c2 全部种子零命中 => 1/2。
    runs = [
        _run(1, coverage={"c1": 1, "c2": 0}),
        _run(2, coverage={"c1": 0, "c2": 0}),
    ]
    o = _moutcome("t1", seeds=(1, 2), runs=runs)
    data, result = _build([o], threshold=1.0, seeds=(1, 2))
    summary = data["coverage_summary"]
    assert summary["total_points"] == 2
    assert summary["hit_points"] == 1
    assert summary["ratio"] == 0.5
    assert result == "failed"


def test_configured_point_never_seen_is_unavailable():
    o = _moutcome("t1", runs=[_run(1), _run(2), _run(3)], seeds=(1, 2, 3))
    data, result = _build([o], points=["c1", "missing"])
    assert data["coverage_summary"]["unavailable_points"] == ["missing"]
    assert result == "failed"


def test_run_diagnostics_and_simulate_sanitized():
    runs = [
        _run(1, diagnostics=["see /tmp/secret/x.v"]),
    ]
    o = _moutcome("t1", seeds=(1,), runs=runs)
    data, _ = _build([o], seeds=(1,))
    run = data["testbenches"][0]["runs"][0]
    blob = run["diagnostics"][0]
    assert "/tmp/secret" not in blob and "x.v" in blob
    # 绝对路径的 vvp 脱敏为 basename。
    assert run["simulate"][0] == "vvp"


def test_run_seed_must_be_prefix_of_seed_list():
    # runs 种子与列表不一致（缺了 2）。
    runs = [_run(1), _run(3)]
    o = _moutcome("t1", seeds=(1, 2, 3), runs=runs)
    with pytest.raises(RuntimeError):
        _build([o])


def test_run_seed_order_must_match_list():
    runs = [_run(2), _run(1)]
    o = _moutcome("t1", seeds=(1, 2), runs=runs)
    with pytest.raises(RuntimeError):
        _build([o], seeds=(1, 2))


def test_executed_tb_with_empty_runs_raises():
    o = _moutcome("t1", runs=[])
    with pytest.raises(RuntimeError):
        _build([o])


def test_compilation_failed_tb_with_runs_raises():
    o = _moutcome("t1", status="failed", reason="compilation_failed",
                  runs=[_run(1)])
    with pytest.raises(RuntimeError):
        _build([o])


def test_illegal_run_status_and_reason_raise():
    o = _moutcome("t1", runs=[_run(1, status="weird")])
    with pytest.raises(RuntimeError):
        _build([o])
    o = _moutcome("t1", runs=[_run(1, status="failed", reason="skipped")])
    with pytest.raises(RuntimeError):
        _build([o])


def test_run_status_and_reason_domains():
    assert RUN_STATUSES == ("passed", "failed")
    assert RUN_REASONS == (
        "passed", "simulation_failed", "assertion_failed",
        "incomplete_statistics",
    )


def test_multiseed_tb_order_stable():
    o1 = _moutcome("beta", runs=[
        _run(1, assertions={"z": {"status": "passed", "fail_count": 0}},
             coverage={"zz": 1}),
    ], seeds=(1,))
    o2 = _moutcome("alpha", runs=[
        _run(1, assertions={"m": {"status": "passed", "fail_count": 0}},
             coverage={"mm": 1}),
    ], seeds=(1,))
    data, _ = _build([o1, o2], seeds=(1,))
    assert [t["name"] for t in data["testbenches"]] == ["beta", "alpha"]
    assert [a["name"] for a in data["assertions"]] == ["z", "m"]
    assert [c["name"] for c in data["coverage"]] == ["zz", "mm"]


def test_no_seeds_argument_stays_v3_shape():
    # 不传 seeds：构建结果与 v3 完全一致（条目无 seeds/runs）。
    o = {
        "name": "t1", "top": "t1", "testbench_file": "/w/tb/t1.v",
        "status": "passed", "reason": "passed", "required": True,
        "start_time": "2026-01-01T00:00:00+00:00",
        "end_time": "2026-01-01T00:00:01+00:00",
        "diagnostics": [],
        "assertions": {"a1": {"status": "passed", "fail_count": 0}},
        "coverage": {"c1": 1},
        "commands": {"compile": list(_COMPILE),
                     "simulate": ["/usr/bin/vvp", "sim.vvp", "+SEED=0"]},
    }
    data, _ = build_verification_report(
        run_id="rid", sources=["/w/d.v"], testbench_files=["/w/tb/t1.v"],
        coverage_config={"threshold": 1.0, "points": []}, duration="100ns",
        compile_command=o["commands"]["compile"],
        simulate_command=o["commands"]["simulate"],
        outcomes=[o], generated_at="2026-01-01T00:00:02+00:00",
        workdir="/w",
    )
    assert data["schema_version"] == 3
    assert list(data["testbenches"][0].keys()) == [
        "name", "top", "testbench", "status", "reason", "required",
        "start_time", "end_time", "command", "diagnostics", "assertions",
        "coverage",
    ]
