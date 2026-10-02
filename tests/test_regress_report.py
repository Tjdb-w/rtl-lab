"""多随机种子回归报告（schema v2）的聚合与脱敏单测（不调用仿真器）。"""

from rtl_lab.report import build_regress_report


def _run(seed, status, assertions=None, coverage=None,
         compile_argv=("/usr/bin/iverilog", "-o", "/w/sim.vvp", "/w/a.v"),
         simulate_argv=None, diagnostics=()):
    if simulate_argv is None:
        simulate_argv = ("/usr/bin/vvp", "sim.vvp", f"+SEED={seed}")
    return {
        "seed": seed,
        "status": status,
        "diagnostics": list(diagnostics),
        "assertions": assertions or {},
        "coverage": coverage or {},
        "command": {"compile": list(compile_argv),
                    "simulate": list(simulate_argv)},
    }


def _build(runs, *, status="passed", seeds=None, diagnostics=(),
           compile_command=None):
    return build_regress_report(
        tool="icarus-verilog",
        compile_command=(
            compile_command
            if compile_command is not None
            else (runs[0]["command"]["compile"]
                  if runs
                  else ["/usr/bin/iverilog", "-o", "/w/sim.vvp", "/w/a.v"])
        ),
        sources=["/w/src/a.v"],
        testbench="/w/tb/tb.v",
        top="tb",
        duration="100ns",
        seeds=[r["seed"] for r in runs] if seeds is None else seeds,
        status=status,
        diagnostics=list(diagnostics),
        runs=runs,
        workdir="/w",
        known_paths=("/w/sim.vvp", "/w/watchdog.v"),
    )


def test_top_level_shape_v2():
    r = _build([_run(1, "passed")])
    assert r["schema_version"] == 2
    assert list(r.keys()) == [
        "schema_version", "tool", "command", "sources", "testbench", "top",
        "duration", "seeds", "status", "diagnostics", "runs",
        "assertions", "coverage", "failed_seeds",
    ]
    assert r["seeds"] == [1]
    assert "seed" not in r


def test_run_entry_has_exactly_five_fields():
    r = _build([_run(1, "passed",
                     assertions={"a": {"status": "passed", "fail_count": 0}},
                     coverage={"c": 1})])
    entry = r["runs"][0]
    assert list(entry.keys()) == [
        "seed", "status", "diagnostics", "assertions", "coverage",
    ]
    assert entry["seed"] == 1
    assert entry["status"] == "passed"
    assert entry["assertions"] == [
        {"name": "a", "status": "passed", "fail_count": 0}
    ]
    assert entry["coverage"] == [{"name": "c", "hits": 1}]


def test_runs_follow_seed_order():
    r = _build([_run(7, "passed"), _run(2, "passed"), _run(9, "passed")])
    assert [run["seed"] for run in r["runs"]] == [7, 2, 9]
    assert r["seeds"] == [7, 2, 9]


def test_assertion_aggregation_failures_and_first_seen_order():
    runs = [
        _run(1, "passed", assertions={
            "z": {"status": "passed", "fail_count": 0},
            "a": {"status": "passed", "fail_count": 0},
        }),
        _run(2, "assertion_failed", assertions={
            "a": {"status": "failed", "fail_count": 2},
            "m": {"status": "failed", "fail_count": 1},
        }),
        _run(3, "assertion_failed", assertions={
            "z": {"status": "failed", "fail_count": 3},
        }),
    ]
    r = _build(runs, status="assertion_failed")
    # 首次出现顺序跨种子保留。
    assert [a["name"] for a in r["assertions"]] == ["z", "a", "m"]
    by_name = {a["name"]: a for a in r["assertions"]}
    assert by_name["z"] == {"name": "z", "status": "failed", "fail_count": 3}
    assert by_name["a"] == {"name": "a", "status": "failed", "fail_count": 2}
    assert by_name["m"] == {"name": "m", "status": "failed", "fail_count": 1}
    assert r["failed_seeds"] == [2, 3]


def test_assertion_passes_again_still_counts_prior_fails():
    runs = [
        _run(1, "assertion_failed",
             assertions={"x": {"status": "failed", "fail_count": 1}}),
        _run(2, "passed",
             assertions={"x": {"status": "passed", "fail_count": 0}}),
    ]
    r = _build(runs, status="assertion_failed")
    # 任一种子最终失败即 failed；fail_count 汇总。
    assert r["assertions"] == [
        {"name": "x", "status": "failed", "fail_count": 1}
    ]
    assert r["failed_seeds"] == [1]


def test_coverage_aggregation_hits_and_hit_runs():
    runs = [
        _run(1, "passed", coverage={"c1": 2, "c2": 1}),
        _run(2, "passed", coverage={"c1": 0, "c3": 1}),
        _run(3, "passed", coverage={"c1": 3}),
    ]
    r = _build(runs)
    assert [c["name"] for c in r["coverage"]] == ["c1", "c2", "c3"]
    by_name = {c["name"]: c for c in r["coverage"]}
    assert by_name["c1"] == {"name": "c1", "hits": 5, "hit_runs": 2}
    assert by_name["c2"] == {"name": "c2", "hits": 1, "hit_runs": 1}
    assert by_name["c3"] == {"name": "c3", "hits": 1, "hit_runs": 1}


def test_failed_seeds_includes_simulation_failures():
    runs = [
        _run(1, "passed"),
        _run(2, "simulation_failed"),
    ]
    r = _build(runs, status="simulation_failed", seeds=[1, 2, 3])
    assert r["failed_seeds"] == [2]
    # 停止后续种子：runs 只保留已执行的，但 seeds 仍为全部请求种子。
    assert [run["seed"] for run in r["runs"]] == [1, 2]
    assert r["seeds"] == [1, 2, 3]


def test_command_argv_sanitized():
    runs = [
        _run(1, "passed",
             compile_argv=("/usr/bin/iverilog", "-g2012", "-o",
                           "/w/sim.vvp", "/w/src/a.v"),
             simulate_argv=("/usr/bin/vvp", "-n", "sim.vvp", "+SEED=1")),
    ]
    r = _build(runs)
    # 工具位置脱敏为 basename，工作目录内路径转相对路径。
    assert r["command"]["compile"] == [
        "iverilog", "-g2012", "-o", "sim.vvp", "src/a.v",
    ]
    assert r["command"]["simulate"] == ["vvp", "-n", "sim.vvp", "+SEED=1"]


def test_command_simulate_uses_first_seed_argv():
    runs = [_run(10, "passed"), _run(20, "passed")]
    r = _build(runs)
    assert "+SEED=10" in r["command"]["simulate"]


def test_compile_failed_has_no_runs_empty_simulate():
    r = _build([], status="compile_failed", seeds=[1, 2],
               diagnostics=["some compile error\n"])
    assert r["status"] == "compile_failed"
    assert r["runs"] == []
    assert r["failed_seeds"] == []
    assert r["assertions"] == []
    assert r["coverage"] == []
    assert r["command"]["simulate"] == []
    assert r["command"]["compile"]  # 编译 argv 仍保留


def test_run_diagnostics_sanitized():
    runs = [_run(1, "passed",
                 diagnostics=["error at /w/src/a.v and /tmp/secret/x.v"])]
    r = _build(runs)
    blob = r["runs"][0]["diagnostics"][0]
    assert "/w/src/a.v" not in blob and "src/a.v" in blob
    assert "/tmp/secret" not in blob and "x.v" in blob


def test_top_level_diagnostics_sanitized():
    r = _build([], status="compile_failed", seeds=[1],
               diagnostics=["boom /tmp/secret/proj/a.v"])
    assert "/tmp/secret" not in r["diagnostics"][0]
