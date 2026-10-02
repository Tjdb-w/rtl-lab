"""端到端的编译-仿真-报告流程。

入口：:func:`run`。流程：

1. 校验输入（源文件存在性与后缀、顶层名、时长、种子）；
2. 按给定顺序编译设计源文件与测试台，以顶层模块为根，并注入仿真时长看门狗；
3. 执行一次仿真，随机种子通过 ``+SEED=<seed>`` plusarg 传入测试台；
4. 完整保留仿真 stdout/stderr，解析 ASSERT/COVER 记录；
5. 生成 JSON 报告并在需要时抛出对应异常。
"""

import os

from .duration import normalize_duration
from .errors import (
    InputError,
    CompilationError,
    SimulationError,
)
from .parser import ResultCollector
from .report import Report, build_report, write_report
from .tools import (
    WATCHDOG_MODULE,
    compile_sources,
    make_watchdog_source,
    run_simulation,
)

#: 允许的源文件后缀。
SOURCE_SUFFIXES = (".v", ".sv")

#: 各阶段报告使用的 tool 标识。
TOOL_NAME = "icarus-verilog"


class RunConfig:
    """一次仿真运行的输入配置。

    :param sources: 设计源文件路径列表（按编译顺序）。
    :param testbench: 测试台源文件路径。
    :param top: 顶层模块名。
    :param duration: 仿真时长字符串，如 ``"100ns"``。
    :param workdir: 工作目录（编译产物与临时文件置于其中）。
    :param seed: 随机种子（非负整数）。
    :param report_path: JSON 报告输出路径，可为 None。
    """

    def __init__(self, *, sources, testbench, top, duration,
                 workdir=".", seed=0, report_path=None):
        self.sources = list(sources)
        self.testbench = testbench
        self.top = top
        self.duration = duration
        self.workdir = workdir
        self.seed = seed
        self.report_path = report_path


def _check_top(top):
    """校验顶层模块名。"""
    if not isinstance(top, str) or not top.strip():
        raise InputError("顶层模块名不能为空")


def _check_seed(seed):
    """校验随机种子：整数且非负。"""
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise InputError(f"随机种子必须为非负整数，收到 {seed!r}")
    if seed < 0:
        raise InputError(f"随机种子不能为负数，收到 {seed}")


def _check_sources(sources, testbench):
    """校验源文件与测试台：后缀合法且文件存在。"""
    def check_file(path, label):
        if not isinstance(path, str) or not path.strip():
            raise InputError(f"{label}路径不能为空")
        if not path.lower().endswith(SOURCE_SUFFIXES):
            raise InputError(
                f"{label} {path!r} 后缀不受支持：仅支持 .v 与 .sv"
            )
        if not os.path.isfile(path):
            raise InputError(f"{label}不存在：{path}")

    if not sources:
        raise InputError("至少需要一个设计源文件")
    for source in sources:
        check_file(source, "设计源文件")
    check_file(testbench, "测试台文件")


def _validate(config):
    """校验全部输入，非法时抛 :class:`InputError`。"""
    _check_top(config.top)
    _check_seed(config.seed)

    # 时长（正整数 + 受支持单位）。
    amount, unit, _fs = normalize_duration(config.duration)

    _check_sources(config.sources, config.testbench)

    return amount, unit


def _emit_report(config, duration_str, status, diagnostics, collector,
                 vvp_output, watchdog_path, compile_command=None,
                 run_command=None, raw_stdout="", raw_stderr=""):
    """组装并落盘报告，返回带原始流的 :class:`Report`。"""
    data = build_report(
        tool=TOOL_NAME,
        command={
            "compile": compile_command or [],
            "simulate": run_command or [],
        },
        sources=config.sources,
        testbench=config.testbench,
        top=config.top.strip(),
        duration=duration_str,
        seed=config.seed,
        status=status,
        diagnostics=diagnostics,
        assertions=collector.assertions,
        coverage=collector.coverage,
        workdir=config.workdir,
        known_paths=(vvp_output, watchdog_path, config.report_path),
    )
    report = Report(data, raw_stdout=raw_stdout, raw_stderr=raw_stderr)
    if config.report_path:
        write_report(report, config.report_path)
    return report


def run(config=None, **kwargs):
    """执行一次完整流程。

    可传 :class:`RunConfig`，也可用关键字参数直接构造。

    :returns: 成功时返回报告 dict。
    :raises InputError: 输入校验失败（退出码 2，不生成报告）。
    :raises CompilationError: 编译失败（退出码 3，生成 compile_failed 报告）。
    :raises ToolError: iverilog/vvp 无法启动（退出码 4，发生在报告生成之前）。
    :raises SimulationError: 仿真非零退出（5）或断言失败（6），均生成报告。
    """
    if config is None:
        config = RunConfig(**kwargs)
    elif kwargs:
        raise TypeError("run() 不能同时传入 RunConfig 与关键字参数")

    amount, unit = _validate(config)

    os.makedirs(config.workdir, exist_ok=True)
    vvp_output = os.path.join(config.workdir, "rtl_lab_sim.vvp")
    watchdog_path = os.path.join(config.workdir, "rtl_lab_watchdog.v")

    # 测试台排在设计源文件之后，按给定顺序一起编译。
    all_sources = list(config.sources) + [config.testbench]

    make_watchdog_source(watchdog_path, amount, unit)
    compile_files = all_sources + [watchdog_path]

    # ---- 编译 ----
    rc, cout, cerr, compile_command = compile_sources(
        source_files=compile_files,
        top=config.top.strip(),
        output_path=vvp_output,
        extra_roots=[WATCHDOG_MODULE],
    )
    if rc != 0:
        diagnostics = []
        if cout:
            diagnostics.append(cout)
        if cerr:
            diagnostics.append(cerr)
        collector = ResultCollector()
        report = _emit_report(
            config, f"{amount}{unit}", "compile_failed", diagnostics,
            collector, vvp_output, watchdog_path,
            compile_command=compile_command,
            raw_stdout=cout, raw_stderr=cerr,
        )
        raise CompilationError(
            "编译失败（iverilog 返回非零状态）", report=report,
            raw_stdout=cout, raw_stderr=cerr,
        )

    # ---- 仿真 ----
    # vvp 以工作目录为 cwd 运行，故产物使用 basename 定位。
    rc, sout, serr, run_command = run_simulation(
        vvp_path=os.path.basename(vvp_output),
        seed=config.seed, cwd=config.workdir
    )

    collector = ResultCollector()
    collector.feed_text(sout)

    diagnostics = []
    if sout:
        diagnostics.append(sout)
    if serr:
        diagnostics.append(serr)

    if rc != 0:
        report = _emit_report(
            config, f"{amount}{unit}", "simulation_failed", diagnostics,
            collector, vvp_output, watchdog_path,
            compile_command=compile_command,
            run_command=run_command,
            raw_stdout=sout, raw_stderr=serr,
        )
        raise SimulationError(
            f"仿真进程以非零状态退出（退出码 {rc}）", report=report,
            raw_stdout=sout, raw_stderr=serr,
        )

    if collector.has_failure:
        report = _emit_report(
            config, f"{amount}{unit}", "assertion_failed", diagnostics,
            collector, vvp_output, watchdog_path,
            compile_command=compile_command,
            run_command=run_command,
            raw_stdout=sout, raw_stderr=serr,
        )
        failed_names = [
            n for n, a in collector.assertions.items()
            if a["status"] == "failed"
        ]
        raise SimulationError(
            "断言失败：" + ", ".join(failed_names),
            report=report, assertion_failed=True,
            raw_stdout=sout, raw_stderr=serr,
        )

    return _emit_report(
        config, f"{amount}{unit}", "passed", diagnostics, collector,
        vvp_output, watchdog_path,
        compile_command=compile_command, run_command=run_command,
        raw_stdout=sout, raw_stderr=serr,
    )
