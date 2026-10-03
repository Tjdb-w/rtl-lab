"""--timeout 功能测试：输入校验、进程超时终止与既有失败流程的映射。"""

import json
import os
import shutil
import stat
import time

import pytest

from rtl_lab.cli import main
from rtl_lab.errors import (
    CompilationError, InputError, SimulationError,
)
from rtl_lab.runner import (
    RegressConfig, RunConfig, normalize_timeout, regress, run,
)
from rtl_lab.tools import TIMEOUT_MARKER, compile_sources
from rtl_lab.verification import TestSpec, VerifyConfig, verify

pytestmark = pytest.mark.skipif(
    shutil.which("iverilog") is None or shutil.which("vvp") is None,
    reason="需要安装 Icarus Verilog",
)

DESIGN = "module d; endmodule\n"

TB_PASS = """
`timescale 1ns/1ps
module tb;
  initial begin
    $display("ASSERT PASS ok");
    $display("COVER c");
    #5 $finish;
  end
endmodule
"""

# 零延迟死循环：仿真时间永不前进，看门狗无法触发，只能由墙钟超时终止。
TB_HANG = """
`timescale 1ns/1ps
module tb;
  initial begin
    $display("BEFORE_HANG");
    while (1) begin end
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


def _run_cfg(files, tb_text=TB_PASS, **kw):
    tmp_path, src, tb = files
    tb.write_text(tb_text)
    report = tmp_path / "r.json"
    return RunConfig(
        sources=[str(src)], testbench=str(tb), top="tb",
        duration="100ns", workdir=str(tmp_path / "w"),
        report_path=str(report), **kw
    ), report


def _verify_cfg(files, tb_text=TB_PASS, **kw):
    tmp_path, src, tb = files
    tb.write_text(tb_text)
    report = tmp_path / "r.json"
    return VerifyConfig(
        sources=[str(src)],
        testbenches=[TestSpec(testbench=str(tb), top="tb", name="tb")],
        run_id="tmo", duration="100ns",
        workdir=str(tmp_path / "w"), report_path=str(report), **kw
    ), report


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---- normalize_timeout 单元测试 ----


@pytest.mark.parametrize("value,expected", [
    (None, None), (1, 1), (30, 30), ("1", 1), ("30", 30), ("007", 7),
    (" 5 ", 5),
])
def test_normalize_timeout_valid(value, expected):
    assert normalize_timeout(value) == expected


@pytest.mark.parametrize("value", [
    0, -1, "0", "-1", "1.5", "2.0", "abc", "", "   ", "+3", "0x10",
    1.5, 2.0, True, False, [1],
])
def test_normalize_timeout_invalid(value):
    with pytest.raises(InputError):
        normalize_timeout(value)


# ---- CLI 输入校验：退出 2，不生成也不覆盖报告 ----


@pytest.mark.parametrize("bad", ["0", "-1", "1.5", "abc", ""])
def test_cli_run_bad_timeout_exit_2(files, bad):
    tmp_path, src, tb = files
    report = tmp_path / "r.json"
    report.write_text('{"sentinel": true}\n', encoding="utf-8")
    argv = [
        "run", str(src), "--tb", str(tb), "--top", "tb",
        "--duration", "100ns", "--workdir", str(tmp_path / "w"),
        "--report", str(report), "--timeout", bad,
    ]
    assert main(argv) == 2
    # 已有报告不被覆盖。
    assert json.loads(report.read_text(encoding="utf-8")) == {
        "sentinel": True
    }


def test_cli_regress_bad_timeout_exit_2_no_report(files):
    tmp_path, src, tb = files
    report = tmp_path / "r.json"
    argv = [
        "regress", str(src), "--tb", str(tb), "--top", "tb",
        "--duration", "100ns", "--seeds", "1,2",
        "--workdir", str(tmp_path / "w"),
        "--report", str(report), "--timeout", "0",
    ]
    assert main(argv) == 2
    assert not report.exists()


def test_cli_verify_bad_timeout_exit_2_no_report(files):
    tmp_path, src, tb = files
    report = tmp_path / "r.json"
    argv = [
        "verify", str(src), "--run-id", "x", "--tb", str(tb),
        "--duration", "100ns", "--workdir", str(tmp_path / "w"),
        "--report", str(report), "--timeout", "1.5",
    ]
    assert main(argv) == 2
    assert not report.exists()


def test_python_config_bad_timeout_raises_input_error(files):
    cfg, report = _run_cfg(files, timeout=0)
    with pytest.raises(InputError):
        run(cfg)
    assert not report.exists()

    # VerifyConfig 在构造时即完成超时校验。
    with pytest.raises(InputError):
        _verify_cfg(files, timeout="abc")
    assert not report.exists()


# ---- 未超时：行为与省略时一致 ----


def test_run_with_timeout_passes_normally(files):
    cfg, report_path = _run_cfg(files, timeout=30)
    report = run(cfg)
    assert report["status"] == "passed"
    assert _load(report_path)["status"] == "passed"


def test_cli_run_valid_timeout_exit_0(files):
    tmp_path, src, tb = files
    argv = [
        "run", str(src), "--tb", str(tb), "--top", "tb",
        "--duration", "100ns", "--workdir", str(tmp_path / "w"),
        "--report", str(tmp_path / "r.json"), "--timeout", "30",
    ]
    assert main(argv) == 0


# ---- vvp 仿真超时：simulation_failed ----


def test_run_simulation_timeout(files):
    cfg, report_path = _run_cfg(files, TB_HANG, timeout=1)
    start = time.monotonic()
    with pytest.raises(SimulationError) as excinfo:
        run(cfg)
    elapsed = time.monotonic() - start
    assert not excinfo.value.assertion_failed
    assert elapsed < 20  # 被超时终止而非挂死

    report = _load(report_path)
    assert report["status"] == "simulation_failed"
    # 稳定标记进入诊断，且不依赖平台超时提示文本。
    assert any(TIMEOUT_MARKER in d for d in report["diagnostics"])
    assert TIMEOUT_MARKER in excinfo.value.raw_stderr


def test_cli_run_simulation_timeout_exit_5(files):
    tmp_path, src, tb = files
    tb.write_text(TB_HANG)
    argv = [
        "run", str(src), "--tb", str(tb), "--top", "tb",
        "--duration", "100ns", "--workdir", str(tmp_path / "w"),
        "--report", str(tmp_path / "r.json"), "--timeout", "1",
    ]
    assert main(argv) == 5
    report = _load(tmp_path / "r.json")
    assert report["status"] == "simulation_failed"
    assert any(TIMEOUT_MARKER in d for d in report["diagnostics"])


def test_regress_simulation_timeout_stops_seeds(files):
    tmp_path, src, tb = files
    tb.write_text(TB_HANG)
    report_path = tmp_path / "r.json"
    cfg = RegressConfig(
        sources=[str(src)], testbench=str(tb), top="tb",
        duration="100ns", seeds=[1, 2, 3],
        workdir=str(tmp_path / "w"), report_path=str(report_path),
        timeout=1,
    )
    with pytest.raises(SimulationError) as excinfo:
        regress(cfg)
    assert not excinfo.value.assertion_failed
    report = _load(report_path)
    assert report["status"] == "simulation_failed"
    # 第一个种子超时后即停止，后续种子不再执行。
    assert len(report["runs"]) == 1
    assert report["runs"][0]["seed"] == 1
    assert report["runs"][0]["status"] == "simulation_failed"
    assert any(
        TIMEOUT_MARKER in d for d in report["runs"][0]["diagnostics"]
    )


def test_verify_simulation_timeout_single_seed(files):
    cfg, report_path = _verify_cfg(files, TB_HANG, timeout=1)
    report = verify(cfg)
    assert report["result"] == "failed"
    entry = report["testbenches"][0]
    assert entry["status"] == "failed"
    assert entry["reason"] == "simulation_failed"
    assert any(TIMEOUT_MARKER in d for d in entry["diagnostics"])
    assert _load(report_path)["result"] == "failed"


def test_cli_verify_simulation_timeout_exit_7(files):
    tmp_path, src, tb = files
    tb.write_text(TB_HANG)
    argv = [
        "verify", str(src), "--run-id", "x", "--tb", str(tb),
        "--duration", "100ns", "--workdir", str(tmp_path / "w"),
        "--report", str(tmp_path / "r.json"), "--timeout", "1",
    ]
    assert main(argv) == 7
    report = _load(tmp_path / "r.json")
    assert report["result"] == "failed"
    assert report["testbenches"][0]["reason"] == "simulation_failed"


def test_verify_simulation_timeout_multi_seed_stops_tb(files):
    cfg, report_path = _verify_cfg(files, TB_HANG, seeds=[1, 2, 3],
                                   timeout=1)
    report = verify(cfg)
    assert report["result"] == "failed"
    entry = report["testbenches"][0]
    assert entry["reason"] == "simulation_failed"
    # 当前种子记 simulation_failed 并停止该台后续种子。
    assert len(entry["runs"]) == 1
    assert entry["runs"][0]["seed"] == 1
    assert entry["runs"][0]["reason"] == "simulation_failed"
    assert any(
        TIMEOUT_MARKER in d for d in entry["runs"][0]["diagnostics"]
    )


def test_verify_timeout_jobs_parallel_per_tb_budget(files):
    """--jobs 只并行测试台：各台预算独立，互不合并或平均。"""
    tmp_path, src, tb = files
    tb2 = tmp_path / "tb2.v"
    tb2.write_text(TB_HANG.replace("module tb;", "module tb2;"))
    tb.write_text(TB_HANG)
    cfg = VerifyConfig(
        sources=[str(src)],
        testbenches=[
            TestSpec(testbench=str(tb), top="tb", name="tb"),
            TestSpec(testbench=str(tb2), top="tb2", name="tb2"),
        ],
        run_id="tmo", duration="100ns",
        workdir=str(tmp_path / "w"),
        report_path=str(tmp_path / "r.json"),
        jobs=2, timeout=2,
    )
    start = time.monotonic()
    report = verify(cfg)
    elapsed = time.monotonic() - start
    assert report["result"] == "failed"
    reasons = [e["reason"] for e in report["testbenches"]]
    assert reasons == ["simulation_failed", "simulation_failed"]
    # 两台并行各自计满预算，而非串行相加。
    assert elapsed < 8


# ---- iverilog 编译超时：compile_failed / compilation_failed ----


@pytest.fixture
def fake_iverilog(tmp_path, monkeypatch):
    """把 iverilog 替换为一个永不返回的脚本，vvp 保持真实。"""
    script = tmp_path / "fake_iverilog.sh"
    script.write_text("#!/bin/sh\nsleep 60\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    real_which = shutil.which

    def fake_which(name):
        if name == "iverilog":
            return str(script)
        return real_which(name)

    monkeypatch.setattr("rtl_lab.tools.shutil.which", fake_which)
    return script


def test_compile_sources_timeout_unit(tmp_path, fake_iverilog):
    start = time.monotonic()
    rc, stdout, stderr, _cmd = compile_sources(
        source_files=["x.v"], top="tb",
        output_path=str(tmp_path / "out.vvp"), timeout=1,
    )
    assert time.monotonic() - start < 20
    assert rc != 0
    assert TIMEOUT_MARKER in stderr


def test_run_compile_timeout(files, fake_iverilog):
    cfg, report_path = _run_cfg(files, timeout=1)
    with pytest.raises(CompilationError):
        run(cfg)
    report = _load(report_path)
    assert report["status"] == "compile_failed"
    assert any(TIMEOUT_MARKER in d for d in report["diagnostics"])


def test_cli_run_compile_timeout_exit_3(files, fake_iverilog):
    tmp_path, src, tb = files
    argv = [
        "run", str(src), "--tb", str(tb), "--top", "tb",
        "--duration", "100ns", "--workdir", str(tmp_path / "w"),
        "--report", str(tmp_path / "r.json"), "--timeout", "1",
    ]
    assert main(argv) == 3
    assert _load(tmp_path / "r.json")["status"] == "compile_failed"


def test_regress_compile_timeout(files, fake_iverilog):
    tmp_path, src, tb = files
    report_path = tmp_path / "r.json"
    cfg = RegressConfig(
        sources=[str(src)], testbench=str(tb), top="tb",
        duration="100ns", seeds=[1, 2],
        workdir=str(tmp_path / "w"), report_path=str(report_path),
        timeout=1,
    )
    with pytest.raises(CompilationError):
        regress(cfg)
    report = _load(report_path)
    assert report["status"] == "compile_failed"
    assert report["runs"] == []
    assert any(TIMEOUT_MARKER in d for d in report["diagnostics"])


def test_verify_compile_timeout_multi_seed(files, fake_iverilog):
    cfg, report_path = _verify_cfg(files, seeds=[1, 2], timeout=1)
    report = verify(cfg)
    assert report["result"] == "failed"
    entry = report["testbenches"][0]
    assert entry["reason"] == "compilation_failed"
    # 编译失败：多种子模式下 runs 为空。
    assert entry["runs"] == []
    assert any(TIMEOUT_MARKER in d for d in entry["diagnostics"])


# ---- 残留子进程清理 ----


def test_timeout_preserves_captured_stdout(tmp_path):
    """超时前已捕获的 stdout 保留，stderr 末尾追加稳定标记。"""
    from rtl_lab.tools import _execute  # noqa: PLC2701

    script = tmp_path / "echo_then_hang.sh"
    script.write_text(
        "#!/bin/sh\necho CAPTURED_LINE\nsleep 60\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    rc, stdout, stderr = _execute([str(script)], tool="sh", timeout=1)
    assert rc != 0
    assert "CAPTURED_LINE" in stdout
    assert stderr.endswith(TIMEOUT_MARKER + "\n")


def test_timeout_kills_leftover_children(tmp_path):
    """父进程被终止后，其派生的子进程不应残留。"""
    from rtl_lab.tools import _execute  # noqa: PLC2701

    script = tmp_path / "spawn.sh"
    marker = tmp_path / "child_started"
    script.write_text(
        "#!/bin/sh\n"
        f"sleep 60 &\n"
        f"echo $! > {marker}\n"
        "wait\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    rc, _out, stderr = _execute([str(script)], tool="sh", timeout=1)
    assert rc != 0
    assert TIMEOUT_MARKER in stderr

    child_pid = int(marker.read_text(encoding="utf-8").strip())
    # 子进程已被进程组终止（pid 可能已被回收复用前的短暂窗口内检查）。
    assert not os.path.exists(f"/proc/{child_pid}") or \
        "zombie" in open(f"/proc/{child_pid}/stat").read().lower()
