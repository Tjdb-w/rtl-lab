"""--timeout 进程超时测试：输入校验、超时标记与各命令的阶段失败归并。"""

import json
import os
import shutil
import time

import pytest

from rtl_lab.cli import main
from rtl_lab.errors import InputError
from rtl_lab.runner import parse_timeout
from rtl_lab.tools import PROCESS_TIMEOUT_MARKER, compile_sources, run_simulation

needs_icarus = pytest.mark.skipif(
    shutil.which("iverilog") is None or shutil.which("vvp") is None,
    reason="需要安装 Icarus Verilog",
)

DESIGN = "module d; endmodule\n"

TB_PASS = """
`timescale 1ns/1ps
module tb;
  initial begin
    $display("ASSERT PASS a");
    $display("COVER c");
    #5 $finish;
  end
endmodule
"""

TB_PASS2 = """
`timescale 1ns/1ps
module tb2;
  initial begin
    $display("ASSERT PASS a2");
    $display("COVER c2");
    #5 $finish;
  end
endmodule
"""

# 零延迟死循环：仿真时间不前进，时长看门狗不会触发，vvp 一直空转。
TB_HANG = """
`timescale 1ns/1ps
module tb;
  initial begin
    $display("ASSERT PASS a");
    $display("COVER c");
    forever begin end
  end
endmodule
"""


@pytest.fixture
def files(tmp_path):
    src = tmp_path / "d.v"
    src.write_text(DESIGN)
    tb = tmp_path / "tb.v"
    tb.write_text(TB_PASS)
    return tmp_path, src, tb


def _run_args(files, tb_text=None, extra=()):
    tmp_path, src, tb = files
    if tb_text is not None:
        tb.write_text(tb_text)
    return [
        "run", str(src), "--tb", str(tb), "--top", "tb",
        "--duration", "100ns", "--workdir", str(tmp_path / "w"),
        "--report", str(tmp_path / "r.json"),
    ] + list(extra)


def _regress_args(files, seeds, tb_text=None, extra=()):
    tmp_path, src, tb = files
    if tb_text is not None:
        tb.write_text(tb_text)
    return [
        "regress", str(src), "--tb", str(tb), "--top", "tb",
        "--duration", "100ns", "--seeds", seeds,
        "--workdir", str(tmp_path / "w"),
        "--report", str(tmp_path / "r.json"),
    ] + list(extra)


def _verify_args(files, tbs, extra=()):
    tmp_path, src, _tb = files
    argv = [
        "verify", str(src), "--run-id", "tid", "--duration", "100ns",
        "--workdir", str(tmp_path / "w"),
        "--report", str(tmp_path / "r.json"),
    ]
    for tb in tbs:
        argv += ["--tb", str(tb)]
    return argv + list(extra)


# ---------------- 超时值解析 ----------------

@pytest.mark.parametrize("value,expected", [
    (None, None), ("1", 1), ("007", 7), (" 3 ", 3), (1, 1), (3600, 3600),
])
def test_parse_timeout_valid(value, expected):
    assert parse_timeout(value) == expected


@pytest.mark.parametrize("value", [
    0, -1, -3600, "0", "-3", "1.5", "1e3", "+3", "abc", "", "   ",
    1.5, True, False,
])
def test_parse_timeout_invalid(value):
    with pytest.raises(InputError):
        parse_timeout(value)


# ---------------- 非法超时的命令行行为（退出 2，不生成/覆盖报告） ----------------

@pytest.mark.parametrize("bad", ["0", "-1", "1.5", "abc", ""])
def test_cli_run_invalid_timeout_exit_2(files, bad):
    tmp_path = files[0]
    assert main(_run_args(files, extra=["--timeout", bad])) == 2
    assert not (tmp_path / "r.json").exists()


def test_cli_regress_invalid_timeout_exit_2(files):
    tmp_path = files[0]
    argv = _regress_args(files, "1,2", extra=["--timeout", "0"])
    assert main(argv) == 2
    assert not (tmp_path / "r.json").exists()


def test_cli_verify_invalid_timeout_exit_2(files):
    tmp_path = files[0]
    argv = _verify_args(files, [files[2]], extra=["--timeout", "x"])
    assert main(argv) == 2
    assert not (tmp_path / "r.json").exists()


def test_cli_invalid_timeout_keeps_existing_report(files):
    tmp_path = files[0]
    report = tmp_path / "r.json"
    report.write_text('{"keep": true}\n')
    assert main(_run_args(files, extra=["--timeout", "0"])) == 2
    assert report.read_text() == '{"keep": true}\n'


# ---------------- 工具层超时行为 ----------------

def _make_script(tmp_path, name, body):
    script = tmp_path / name
    script.write_text(body)
    script.chmod(0o755)
    return script


def _patch_tool(monkeypatch, tool_name, script):
    """把指定工具的查找结果替换为脚本路径。"""
    from rtl_lab import tools

    real_find = tools.find_executable

    def fake_find(name):
        if name == tool_name:
            return str(script)
        return real_find(name)

    monkeypatch.setattr(tools, "find_executable", fake_find)


def test_compile_timeout_marker_and_nonzero_rc(tmp_path, monkeypatch):
    sleeper = _make_script(tmp_path, "fake_iverilog", "#!/bin/sh\nsleep 60\n")
    _patch_tool(monkeypatch, "iverilog", sleeper)
    rc, _out, err, _cmd = compile_sources(
        source_files=["a.v"], top="tb", output_path="o.vvp", timeout=1,
    )
    assert rc != 0
    assert err == PROCESS_TIMEOUT_MARKER + "\n"


def test_compile_timeout_marker_appended_after_partial_stderr(
    tmp_path, monkeypatch
):
    script = _make_script(
        tmp_path, "fake_iverilog",
        "#!/bin/sh\nprintf partial >&2\nsleep 60\n",
    )
    _patch_tool(monkeypatch, "iverilog", script)
    rc, _out, err, _cmd = compile_sources(
        source_files=["a.v"], top="tb", output_path="o.vvp", timeout=1,
    )
    assert rc != 0
    assert err == "partial\n" + PROCESS_TIMEOUT_MARKER + "\n"


def _pid_alive(pid):
    """/proc 下判断进程是否存活（僵尸/已回收均视为不存活）。"""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            rest = f.read().rsplit(")", 1)[1]
        return rest.split()[0] not in ("Z", "X")
    except OSError:
        return False


@pytest.mark.skipif(not os.path.exists("/proc"), reason="需要 /proc")
def test_timeout_kills_residual_child_processes(tmp_path, monkeypatch):
    pid_file = tmp_path / "child.pid"
    script = _make_script(
        tmp_path, "fake_vvp",
        "#!/bin/sh\n"
        "sleep 300 &\n"
        f"echo $! > {pid_file}\n"
        "exec sleep 300\n",
    )
    _patch_tool(monkeypatch, "vvp", script)
    rc, _out, err, _cmd = run_simulation(
        vvp_path="x.vvp", seed=0, cwd=str(tmp_path), timeout=1,
    )
    assert rc != 0
    assert PROCESS_TIMEOUT_MARKER in err

    deadline = time.time() + 5
    child_pid = None
    while time.time() < deadline:
        if pid_file.exists() and pid_file.read_text().strip():
            child_pid = int(pid_file.read_text().strip())
            if not _pid_alive(child_pid):
                break
        time.sleep(0.05)
    assert child_pid is not None
    assert not _pid_alive(child_pid)


# ---------------- run：编译/仿真超时归入既有阶段失败 ----------------

@needs_icarus
def test_run_pass_with_timeout_unaffected(files):
    tmp_path = files[0]
    assert main(_run_args(files, extra=["--timeout", "30"])) == 0
    data = json.load(open(tmp_path / "r.json"))
    assert data["status"] == "passed"
    assert all(
        PROCESS_TIMEOUT_MARKER not in d for d in data["diagnostics"]
    )


@needs_icarus
def test_run_simulation_timeout_exit_5(files):
    tmp_path = files[0]
    assert main(_run_args(files, TB_HANG, ["--timeout", "1"])) == 5
    data = json.load(open(tmp_path / "r.json"))
    assert data["status"] == "simulation_failed"
    assert data["schema_version"] == 1
    assert any(PROCESS_TIMEOUT_MARKER in d for d in data["diagnostics"])


@needs_icarus
def test_run_compile_timeout_exit_3(files, monkeypatch):
    sleeper = _make_script(files[0], "fake_iverilog", "#!/bin/sh\nsleep 60\n")
    _patch_tool(monkeypatch, "iverilog", sleeper)
    tmp_path = files[0]
    assert main(_run_args(files, extra=["--timeout", "1"])) == 3
    data = json.load(open(tmp_path / "r.json"))
    assert data["status"] == "compile_failed"
    assert any(PROCESS_TIMEOUT_MARKER in d for d in data["diagnostics"])


# ---------------- regress：按种子分别计时，超时停止后续种子 ----------------

@needs_icarus
def test_regress_simulation_timeout_stops_seeds(files):
    tmp_path = files[0]
    argv = _regress_args(files, "1,2,3", TB_HANG, ["--timeout", "1"])
    assert main(argv) == 5
    data = json.load(open(tmp_path / "r.json"))
    assert data["status"] == "simulation_failed"
    assert data["schema_version"] == 2
    assert len(data["runs"]) == 1
    assert data["runs"][0]["seed"] == 1
    assert data["runs"][0]["status"] == "simulation_failed"
    assert any(
        PROCESS_TIMEOUT_MARKER in d for d in data["runs"][0]["diagnostics"]
    )


@needs_icarus
def test_regress_compile_timeout_exit_3(files, monkeypatch):
    sleeper = _make_script(files[0], "fake_iverilog", "#!/bin/sh\nsleep 60\n")
    _patch_tool(monkeypatch, "iverilog", sleeper)
    tmp_path = files[0]
    argv = _regress_args(files, "1,2", extra=["--timeout", "1"])
    assert main(argv) == 3
    data = json.load(open(tmp_path / "r.json"))
    assert data["status"] == "compile_failed"
    assert data["runs"] == []
    assert any(PROCESS_TIMEOUT_MARKER in d for d in data["diagnostics"])


# ---------------- verify：逐台/逐种子独立计时 ----------------

@needs_icarus
def test_verify_simulation_timeout(files):
    tmp_path = files[0]
    argv = _verify_args(files, [files[2]], ["--timeout", "1"])
    files[2].write_text(TB_HANG)
    assert main(argv) == 7
    data = json.load(open(tmp_path / "r.json"))
    assert data["result"] == "failed"
    tb0 = data["testbenches"][0]
    assert tb0["status"] == "failed"
    assert tb0["reason"] == "simulation_failed"
    assert any(PROCESS_TIMEOUT_MARKER in d for d in tb0["diagnostics"])


@needs_icarus
def test_verify_multiseed_simulation_timeout_stops_tb(files):
    tmp_path = files[0]
    files[2].write_text(TB_HANG)
    argv = _verify_args(
        files, [files[2]], ["--seeds", "1,2,3", "--timeout", "1"]
    )
    assert main(argv) == 7
    data = json.load(open(tmp_path / "r.json"))
    assert data["result"] == "failed"
    assert data["schema_version"] == 5
    tb0 = data["testbenches"][0]
    assert tb0["reason"] == "simulation_failed"
    assert len(tb0["runs"]) == 1
    assert tb0["runs"][0]["seed"] == 1
    assert tb0["runs"][0]["reason"] == "simulation_failed"
    assert any(
        PROCESS_TIMEOUT_MARKER in d for d in tb0["runs"][0]["diagnostics"]
    )


@needs_icarus
def test_verify_multiseed_compile_timeout(files, monkeypatch):
    sleeper = _make_script(files[0], "fake_iverilog", "#!/bin/sh\nsleep 60\n")
    _patch_tool(monkeypatch, "iverilog", sleeper)
    tmp_path = files[0]
    argv = _verify_args(
        files, [files[2]], ["--seeds", "1,2", "--timeout", "1"]
    )
    assert main(argv) == 7
    data = json.load(open(tmp_path / "r.json"))
    tb0 = data["testbenches"][0]
    assert tb0["reason"] == "compilation_failed"
    assert tb0["runs"] == []
    assert any(PROCESS_TIMEOUT_MARKER in d for d in tb0["diagnostics"])


@needs_icarus
def test_verify_jobs_per_call_timeout_budget(files):
    """--jobs 并行时各测试台独立计时：挂起的台超时失败，正常的台通过。"""
    tmp_path, src, tb = files
    tb.write_text(TB_HANG)
    tb2 = tmp_path / "tb2.v"
    tb2.write_text(TB_PASS2)
    argv = _verify_args(
        files, [tb, tb2], ["--jobs", "2", "--timeout", "1"]
    )
    assert main(argv) == 7
    data = json.load(open(tmp_path / "r.json"))
    by_name = {t["name"]: t for t in data["testbenches"]}
    assert by_name["tb"]["reason"] == "simulation_failed"
    assert by_name["tb2"]["status"] == "passed"
