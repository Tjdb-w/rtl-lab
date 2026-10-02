"""统一验证报告（schema v3）纯构建层与配置校验单测（不调用仿真器）。"""

import json
import os

import pytest

from rtl_lab.verify import (
    CoverageConfig,
    TestSpec,
    VERIFY_SCHEMA_VERSION,
    VerifyConfig,
    _check_output_writable,
    _validate_config,
    build_verify_report,
)


# ---------------------------------------------------------------------------
# 构造夹具
# ---------------------------------------------------------------------------

def _spec(name, path=None, **kw):
    return TestSpec(path or f"/w/tb/{name}.v", name=name, **kw)


def _result(name, *, run_id=None, status="passed", returncode=0,
            started_at="2026-01-01T00:00:00+00:00",
            ended_at="2026-01-01T00:00:01+00:00",
            assertions=None, coverage=None,
            compile_command=None, simulate_command=None,
            compiled_files=None, diagnostics=None,
            _run_id="r1"):
    if run_id is None:
        run_id = _run_id
    return {
        "run_id": run_id,
        "name": name,
        "status": status,
        "returncode": returncode,
        "started_at": started_at,
        "ended_at": ended_at,
        "diagnostics": ["ok\n"] if diagnostics is None else diagnostics,
        "assertions": assertions if assertions is not None else {
            f"a_{name}": {"status": "passed", "fail_count": 0},
        },
        "coverage": coverage if coverage is not None else {
            f"c_{name}": 1,
        },
        "compile_command": compile_command or [
            "/usr/bin/iverilog", "-g2012", "-o", f"/w/{name}.vvp",
            "/w/src/d.v", f"/w/tb/{name}.v",
        ],
        "simulate_command": simulate_command if simulate_command is not None
        else ["/usr/bin/vvp", "-n", f"{name}.vvp", "+SEED=0"],
        "compiled_files": compiled_files if compiled_files is not None else [
            "/w/src/d.v", f"/w/tb/{name}.v", f"/w/watchdog_{name}.v",
        ],
    }


def _skip_record(name, run_id="r1"):
    return {"name": name, "run_id": run_id, "status": "skipped"}


def _build(names=("t1",), *, results=None, coverage=None, specs=None,
           sources=("/w/src/d.v",), run_id="r1", default_top="tb",
           duration="100ns"):
    if specs is None:
        specs = [_spec(n) for n in names]
    if results is None:
        results = [
            _skip_record(s.name, run_id=run_id) if s.skip
            else _result(s.name, _run_id=run_id)
            for s in specs
        ]
    return build_verify_report(
        run_id=run_id,
        sources=list(sources),
        testbenches=specs,
        default_top=default_top,
        duration=duration,
        coverage_config=coverage or CoverageConfig(),
        results=results,
        workdir="/w",
        known_paths=("/w/out.vvp",),
    )


# ---------------------------------------------------------------------------
# 结构
# ---------------------------------------------------------------------------

def test_schema_version_and_fixed_key_order():
    r = _build()
    assert r["schema_version"] == 3
    assert VERIFY_SCHEMA_VERSION == 3
    assert list(r.keys()) == [
        "schema_version", "run_id", "tool", "config_summary",
        "compiled_files", "testbenches", "totals", "assertions",
        "coverage_summary", "conclusion",
    ]


def test_run_id_recorded():
    assert _build(run_id="build-42")["run_id"] == "build-42"


def test_tb_entry_shape_and_order():
    r = _build(("z", "a", "m"))
    assert [t["name"] for t in r["testbenches"]] == ["z", "a", "m"]
    entry = r["testbenches"][0]
    assert list(entry.keys()) == [
        "name", "testbench", "status", "end_reason", "required", "top",
        "seed", "skip_reason", "started_at", "ended_at",
        "assertions", "coverage", "diagnostics",
    ]
    assert entry["status"] == "passed"
    assert entry["end_reason"] is None
    assert entry["required"] is True
    assert entry["top"] == "tb"
    assert entry["seed"] == 0
    assert entry["testbench"] == "tb/z.v"


def test_totals_counts():
    specs = [_spec("p1"), _spec("p2")]
    results = [_result("p1"), _result("p2")]
    r = _build(specs=specs, results=results)
    assert r["totals"] == {"total": 2, "passed": 2, "failed": 0,
                           "skipped": 0}


def test_assertion_totals_and_results():
    specs = [_spec("t1")]
    results = [_result("t1", assertions={
        "ok": {"status": "passed", "fail_count": 0},
        "bad": {"status": "failed", "fail_count": 2},
    })]
    r = _build(specs=specs, results=results)
    assert r["assertions"]["total"] == 2
    assert r["assertions"]["passed"] == 1
    assert r["assertions"]["failed"] == 1
    assert [a["name"] for a in r["assertions"]["results"]] == ["ok", "bad"]


def test_coverage_summary_ratio():
    specs = [_spec("t1")]
    results = [_result("t1", coverage={"hit1": 3, "hit2": 1, "miss": 0})]
    r = _build(specs=specs, results=results)
    cov = r["coverage_summary"]
    assert cov["points_total"] == 3
    assert cov["points_hit"] == 2
    assert cov["hit_ratio"] == round(2 / 3, 6)
    assert cov["threshold"] is None
    assert cov["threshold_met"] is True
    assert isinstance(cov["threshold_met"], bool)


# ---------------------------------------------------------------------------
# 稳定顺序与可复现
# ---------------------------------------------------------------------------

def test_output_order_independent_of_result_collection_order():
    specs = [_spec("z"), _spec("a"), _spec("m")]
    shuffled = [_result("m"), _result("z"), _result("a")]
    r = _build(specs=specs, results=shuffled)
    assert [t["name"] for t in r["testbenches"]] == ["z", "a", "m"]


def test_compiled_files_sorted_and_sanitized():
    specs = [_spec("b"), _spec("a")]
    results = [
        _result("b", compiled_files=["/w/src/z.v", "/w/tb/b.v"]),
        _result("a", compiled_files=["/w/src/a.v", "/w/tb/a.v",
                                     "/tmp/secret/x.v"]),
    ]
    r = _build(specs=specs, results=results)
    # 并集脱敏、稳定排序；工作目录内保留相对路径，工作目录外仅留 basename。
    assert r["compiled_files"]["files"] == [
        "src/a.v", "src/z.v", "tb/a.v", "tb/b.v", "x.v",
    ]
    assert r["compiled_files"]["design_sources"] == ["src/d.v"]
    assert list(r["compiled_files"]["per_testbench"]) == ["b", "a"]
    assert r["compiled_files"]["per_testbench"]["b"] == ["src/z.v", "tb/b.v"]


def test_report_body_deterministic_ignoring_timestamps():
    specs = [_spec("t1")]

    def build_with(ts):
        return _build(specs=specs, results=[_result(
            "t1", started_at=ts, ended_at=ts,
        )])

    r1 = build_with("2026-01-01T00:00:00+00:00")
    r2 = build_with("2026-02-02T11:22:33+00:00")
    body1 = _strip_timestamps(r1)
    body2 = _strip_timestamps(r2)
    assert body1 == body2
    # 时间戳字段确实存在且允许不同。
    assert r1["testbenches"][0]["started_at"] != r2["testbenches"][0]["started_at"]


def _strip_timestamps(report):
    import copy
    data = copy.deepcopy(report)
    for tb in data["testbenches"]:
        tb["started_at"] = None
        tb["ended_at"] = None
    return data


def test_json_is_valid_utf8_and_platform_independent(tmp_path):
    r = _build(specs=[_spec("t1")],
               results=[_result("t1", diagnostics=["中文诊断 ✓\n"])])
    path = tmp_path / "r.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False)
    raw = path.read_bytes()
    assert "中文".encode("utf-8") in raw
    loaded = json.loads(raw.decode("utf-8"))
    assert loaded["testbenches"][0]["diagnostics"] == ["中文诊断 ✓\n"]


# ---------------------------------------------------------------------------
# 失败 / 跳过 / 覆盖率阈值
# ---------------------------------------------------------------------------

def test_assertion_failure_still_builds_full_report():
    specs = [_spec("t1")]
    results = [_result("t1", status="assertion_failed", returncode=0,
                       assertions={"a": {"status": "failed", "fail_count": 1}})]
    r = _build(specs=specs, results=results)
    assert r["conclusion"] == "failed"
    tb = r["testbenches"][0]
    assert tb["status"] == "failed"
    assert tb["end_reason"] == "assertion_failed"
    assert r["totals"]["failed"] == 1


def test_simulation_failure_tb_status():
    specs = [_spec("t1"), _spec("t2")]
    results = [
        _result("t1", status="simulation_failed", returncode=1,
                assertions={}, coverage={}),
        _result("t2"),
    ]
    r = _build(specs=specs, results=results)
    assert r["conclusion"] == "failed"
    statuses = {t["name"]: (t["status"], t["end_reason"])
                for t in r["testbenches"]}
    assert statuses["t1"] == ("failed", "simulation_failed")
    assert statuses["t2"] == ("passed", None)


def test_compile_failure_tb_keeps_compile_command():
    specs = [_spec("t1"), _spec("t2")]
    results = [
        _result("t1", status="compile_failed", returncode=1,
                assertions={}, coverage={}, simulate_command=[],
                diagnostics=["error: unknown module\n"]),
        _result("t2"),
    ]
    r = _build(specs=specs, results=results)
    assert r["conclusion"] == "failed"
    t1 = r["testbenches"][0]
    assert t1["end_reason"] == "compile_failed"
    assert r["config_summary"]["compile_commands"]["t1"]
    assert "t1" not in r["config_summary"]["simulate_commands"]


def test_passed_without_assertions_is_missing_stats_failed():
    specs = [_spec("t1"), _spec("t2")]
    results = [
        _result("t1", assertions={}),
        _result("t2"),
    ]
    r = _build(specs=specs, results=results)
    t1 = r["testbenches"][0]
    assert t1["status"] == "failed"
    assert t1["end_reason"] == "missing_stats"
    assert r["conclusion"] == "failed"


def test_optional_skip_does_not_fail_overall():
    specs = [_spec("t1", required=False, skip=True, skip_reason="wip"),
             _spec("t2")]
    results = [_skip_record("t1"), _result("t2")]
    r = _build(specs=specs, results=results)
    skipped = r["testbenches"][0]
    assert skipped["status"] == "skipped"
    assert skipped["skip_reason"] == "wip"
    assert skipped["started_at"] is None and skipped["ended_at"] is None
    assert r["totals"] == {"total": 2, "passed": 1, "failed": 0,
                           "skipped": 1}
    assert r["conclusion"] == "passed"


def test_required_skip_fails_overall_but_keeps_others():
    specs = [_spec("t1", skip=True), _spec("t2")]
    results = [_skip_record("t1"), _result("t2")]
    r = _build(specs=specs, results=results)
    assert r["conclusion"] == "failed"
    # 另一个测试台结果不受影响。
    assert r["testbenches"][1]["status"] == "passed"


def test_threshold_below_fails_above_passes():
    specs = [_spec("t1")]
    results = [_result("t1", coverage={"a": 1, "b": 0})]
    r_low = _build(specs=specs, results=results,
                   coverage=CoverageConfig(threshold=0.6))
    assert r_low["conclusion"] == "failed"
    assert r_low["coverage_summary"]["threshold_met"] is False
    r_ok = _build(specs=specs, results=results,
                  coverage=CoverageConfig(threshold=0.5))
    assert r_ok["conclusion"] == "passed"
    assert r_ok["coverage_summary"]["threshold_met"] is True


def test_required_coverage_missing_fails():
    specs = [_spec("t1")]
    results = [_result("t1", coverage={"seen": 1})]
    r = _build(specs=specs, results=results,
               coverage=CoverageConfig(required_points=["seen", "want"]))
    assert r["conclusion"] == "failed"
    assert r["coverage_summary"]["required_points"] == ["seen", "want"]
    assert r["coverage_summary"]["missing_required_points"] == ["want"]


def test_required_coverage_zero_hits_fails():
    specs = [_spec("t1")]
    results = [_result("t1", coverage={"p": 0})]
    r = _build(specs=specs, results=results,
               coverage=CoverageConfig(required_points=["p"]))
    assert r["conclusion"] == "failed"
    assert r["coverage_summary"]["missing_required_points"] == ["p"]


def test_assertions_aggregated_across_testbenches_first_seen_order():
    specs = [_spec("t1"), _spec("t2")]
    results = [
        _result("t1", assertions={
            "z": {"status": "passed", "fail_count": 0},
            "a": {"status": "passed", "fail_count": 0},
        }),
        _result("t2", assertions={
            "a": {"status": "failed", "fail_count": 2},
            "m": {"status": "failed", "fail_count": 1},
        }),
    ]
    r = _build(specs=specs, results=results)
    assert [a["name"] for a in r["assertions"]["results"]] == ["z", "a", "m"]
    by = {a["name"]: a for a in r["assertions"]["results"]}
    assert by["a"] == {"name": "a", "status": "failed", "fail_count": 2}
    assert by["m"]["status"] == "failed"


def test_coverage_accumulated_across_testbenches():
    specs = [_spec("t1"), _spec("t2")]
    results = [
        _result("t1", coverage={"c": 2}),
        _result("t2", coverage={"c": 3}),
    ]
    r = _build(specs=specs, results=results)
    assert r["coverage_summary"]["points_total"] == 1
    assert r["coverage_summary"]["points_hit"] == 1


# ---------------------------------------------------------------------------
# 归属 / 命名冲突 / 重复记录
# ---------------------------------------------------------------------------

def test_empty_run_id_raises_value_error():
    with pytest.raises(ValueError):
        _build(run_id="")
    with pytest.raises(ValueError):
        _build(run_id="   ")


def test_result_run_id_mismatch_raises_runtime_error():
    specs = [_spec("t1")]
    with pytest.raises(RuntimeError, match="不属于本次运行"):
        _build(specs=specs, results=[_result("t1", run_id="other")])


def test_unknown_result_raises_runtime_error():
    specs = [_spec("t1")]
    extra = _result("ghost")
    with pytest.raises(RuntimeError, match="不属于本次运行"):
        _build(specs=specs, results=[_result("t1"), extra])


def test_missing_result_raises_runtime_error():
    specs = [_spec("t1"), _spec("t2")]
    with pytest.raises(RuntimeError, match="缺少属于本次运行"):
        _build(specs=specs, results=[_result("t1")])


def test_duplicate_result_record_raises_runtime_error():
    specs = [_spec("t1")]
    with pytest.raises(RuntimeError, match="重复结果记录"):
        _build(specs=specs, results=[_result("t1"), _result("t1")])


def test_naming_conflict_dict_key_vs_embedded_name_raises():
    specs = [_spec("t1"), _spec("t2")]
    payload = _result("t1")
    payload["name"] = "t2"  # 内嵌名称与归属键不一致
    with pytest.raises(RuntimeError, match="命名冲突"):
        _build(specs=specs, results={"t1": payload, "t2": _result("t2")})


def test_skip_record_for_non_skip_spec_rejected():
    specs = [_spec("t1")]
    with pytest.raises(RuntimeError):
        _build(specs=specs, results=[_skip_record("t1")])


def test_execution_result_for_skip_spec_rejected():
    specs = [_spec("t1", skip=True)]
    with pytest.raises(RuntimeError):
        _build(specs=specs, results=[_result("t1")])


def test_invalid_status_enum_rejected():
    specs = [_spec("t1")]
    bad = _result("t1", status="weird")
    with pytest.raises(RuntimeError):
        _build(specs=specs, results=[bad])


def test_invalid_assertion_fail_count_rejected():
    specs = [_spec("t1")]
    bad = _result("t1", assertions={"a": {"status": "passed",
                                          "fail_count": -1}})
    with pytest.raises(RuntimeError):
        _build(specs=specs, results=[bad])


# ---------------------------------------------------------------------------
# 空结果
# ---------------------------------------------------------------------------

def test_all_skipped_raises_runtime_error():
    specs = [_spec("t1", skip=True), _spec("t2", required=False, skip=True)]
    with pytest.raises(RuntimeError, match="无可执行测试台"):
        _build(specs=specs, results=[_skip_record("t1"),
                                      _skip_record("t2")])


def test_no_coverage_all_passing_raises_runtime_error():
    specs = [_spec("t1"), _spec("t2")]
    results = [
        _result("t1", coverage={}),
        _result("t2", coverage={}),
    ]
    with pytest.raises(RuntimeError, match="没有任何覆盖率结果"):
        _build(specs=specs, results=results)


def test_no_coverage_with_failing_tb_still_builds_report():
    specs = [_spec("t1"), _spec("t2")]
    results = [
        _result("t1", status="assertion_failed",
                assertions={"a": {"status": "failed", "fail_count": 1}},
                coverage={}),
        _result("t2", status="simulation_failed", returncode=1,
                assertions={}, coverage={}),
    ]
    r = _build(specs=specs, results=results)
    assert r["conclusion"] == "failed"
    assert r["coverage_summary"]["points_total"] == 0


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------

def test_report_scrubs_machine_paths():
    specs = [_spec("t1")]
    results = [_result("t1", diagnostics=["see /tmp/secret/x.v please\n"])]
    r = _build(specs=specs, results=results)
    blob = json.dumps(r, ensure_ascii=False)
    assert "/tmp/secret" not in blob
    assert "/usr/bin" not in blob
    assert "x.v" in blob


# ---------------------------------------------------------------------------
# VerifyConfig 校验与输出位置
# ---------------------------------------------------------------------------

def _config(tmp_path, **kw):
    src = tmp_path / "d.v"
    src.write_text("module d; endmodule\n")
    tb = tmp_path / "tb.v"
    tb.write_text("module tb; initial #1 $finish; endmodule\n")
    base = dict(
        sources=[str(src)],
        testbenches=[TestSpec(str(tb), name="t1")],
        default_top="tb",
        duration="100ns",
        run_id="r1",
    )
    base.update(kw)
    return VerifyConfig(**base), src, tb


def test_validate_config_happy_path(tmp_path):
    cfg, _, _ = _config(tmp_path)
    amount, unit = _validate_config(cfg)
    assert (amount, unit) == (100, "ns")


def test_duplicate_tb_name_is_value_error(tmp_path):
    _cfg, src, tb = _config(tmp_path)
    tb2 = tmp_path / "tb2.v"
    tb2.write_text(tb.read_text())
    cfg = VerifyConfig(
        sources=[str(src)],
        testbenches=[TestSpec(str(tb), name="dup"),
                     TestSpec(str(tb2), name="dup")],
        default_top="tb", duration="100ns", run_id="r",
    )
    with pytest.raises(ValueError, match="名称冲突"):
        _validate_config(cfg)


def test_bad_threshold_is_value_error(tmp_path):
    cfg, _, _ = _config(tmp_path, coverage=CoverageConfig(threshold=1.5))
    with pytest.raises(ValueError):
        _validate_config(cfg)


def test_duplicate_required_points_value_error(tmp_path):
    cfg, _, _ = _config(tmp_path,
                        coverage=CoverageConfig(required_points=["p", "p"]))
    with pytest.raises(ValueError):
        _validate_config(cfg)


def test_bad_seed_is_value_error(tmp_path):
    _cfg, src, tb = _config(tmp_path)
    cfg = VerifyConfig(
        sources=[str(src)],
        testbenches=[TestSpec(str(tb), name="t", seed=-1)],
        default_top="tb", duration="100ns", run_id="r",
    )
    with pytest.raises(ValueError):
        _validate_config(cfg)


def test_missing_source_is_value_error(tmp_path):
    cfg, _, _ = _config(tmp_path, sources=[str(tmp_path / "ghost.v")])
    with pytest.raises(ValueError):
        _validate_config(cfg)


def test_output_dir_missing_raises_os_error(tmp_path):
    with pytest.raises(OSError):
        _check_output_writable(str(tmp_path / "no-such-dir" / "r.json"))


def test_output_path_is_directory_raises_os_error(tmp_path):
    with pytest.raises(OSError):
        _check_output_writable(str(tmp_path))


def test_output_none_is_allowed():
    assert _check_output_writable(None) is None


def test_output_empty_path_is_value_error():
    with pytest.raises(ValueError):
        _check_output_writable("")


def test_output_unwritable_dir_raises_os_error(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root 对只读目录仍可写")
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    with pytest.raises(OSError):
        _check_output_writable(str(ro / "r.json"))
