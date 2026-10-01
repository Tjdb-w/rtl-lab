"""报告脱敏与结构单测（不调用仿真器）。"""

from rtl_lab.report import (
    Report, build_report, sanitize_path, sanitize_text, sanitize_argv,
)


def _minimal(**overrides):
    base = dict(
        tool="icarus-verilog",
        command={"compile": ["/usr/bin/iverilog", "-o", "/w/out.vvp", "/w/a.v"],
                 "simulate": ["/usr/bin/vvp", "out.vvp", "+SEED=1"]},
        sources=["/w/src/a.v", "/w/src/b.sv"],
        testbench="/w/tb/tb.v",
        top="tb",
        duration="100ns",
        seed=1,
        status="passed",
        diagnostics=["ok\n"],
        assertions={"a": {"status": "passed", "fail_count": 0}},
        coverage={"c": 1},
        workdir="/w",
    )
    base.update(overrides)
    return build_report(**base)


def test_top_level_keys_fixed():
    r = _minimal()
    assert list(r.keys()) == [
        "schema_version", "tool", "command", "sources", "testbench", "top",
        "duration", "seed", "status", "diagnostics", "assertions", "coverage",
    ]
    assert r["schema_version"] == 1


def test_sources_order_preserved():
    r = _minimal()
    assert r["sources"] == ["src/a.v", "src/b.sv"]
    assert r["testbench"] == "tb/tb.v"


def test_sanitize_external_absolute_path_keeps_basename_only():
    assert sanitize_path("/tmp/secret/proj/a.v", "/w") == "a.v"


def test_relative_paths_preserved():
    assert sanitize_path("src/a.v", "/w") == "src/a.v"
    assert sanitize_path("../x.v", "/w") == "../x.v"


def test_argv_scrubs_tool_abs_path():
    safe = sanitize_argv(["/usr/bin/iverilog", "-g2012", "/w/a.v"], "/w")
    assert safe == ["iverilog", "-g2012", "a.v"]


def test_diagnostics_workdir_prefix_scrubbed():
    out = sanitize_text("error at /w/src/a.v line 2", "/w")
    assert "/w" not in out


def test_diagnostics_external_abs_path_scrubbed():
    out = sanitize_text("see /tmp/secret/x.v please", "/w")
    assert "/tmp/secret" not in out
    assert "x.v" in out


def test_known_input_paths_replaced_with_relative_name():
    out = sanitize_text(
        "note /w/src/a.v:3 thing", "/w",
        known_paths=("/w/src/a.v",),
    )
    assert out.startswith("note src/a.v:3")


def test_report_contains_no_external_abs_paths():
    r = _minimal(diagnostics=["use /tmp/secret/tb.v and /w/src/a.v\n"])
    blob = str(r)
    assert "/tmp/secret" not in blob
    assert "/usr/bin" not in blob


def test_assertion_and_coverage_shapes():
    r = _minimal()
    assert r["assertions"] == [
        {"name": "a", "status": "passed", "fail_count": 0}
    ]
    assert r["coverage"] == [{"name": "c", "hits": 1}]


def test_report_dict_carries_raw_streams():
    r = Report({"status": "passed"}, raw_stdout="out", raw_stderr="err")
    assert r["status"] == "passed"
    assert r.raw_stdout == "out"
    assert r.raw_stderr == "err"
