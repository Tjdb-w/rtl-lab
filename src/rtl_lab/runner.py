"""端到端的编译-仿真-报告流程。

入口：

- :func:`run`：单种子单次运行（报告 schema v1）；
- :func:`regress`：多种子回归，编译一次、依次以 ``+SEED=<seed>`` 仿真
  （报告 schema v2）。

单次流程：

1. 校验输入（源文件存在性与后缀、顶层名、时长、种子）；
2. 按给定顺序编译设计源文件与测试台，以顶层模块为根，并注入仿真时长看门狗；
3. 执行仿真，随机种子通过 ``+SEED=<seed>`` plusarg 传入测试台；
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
from .report import (
    Report,
    build_regress_report,
    build_report,
    write_report,
)
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


class RegressConfig:
    """多随机种子回归的输入配置。

    除 ``seeds`` 外字段含义与 :class:`RunConfig` 相同。

    :param seeds: 非负整数种子列表（按顺序依次执行，不可为空或含重复项）；
        也接受逗号分隔字符串，由 :func:`parse_seeds` 统一解析。
    """

    def __init__(self, *, sources, testbench, top, duration, seeds,
                 workdir=".", report_path=None):
        self.sources = list(sources)
        self.testbench = testbench
        self.top = top
        self.duration = duration
        self.seeds = list(seeds) if not isinstance(seeds, str) else seeds
        self.workdir = workdir
        self.report_path = report_path


def parse_seeds(value):
    """解析 ``--seeds`` 的逗号分隔非负整数列表。

    空值、负数、非整数或重复项均抛 :class:`InputError`。
    接受列表（逐项校验，bool 不视为整数）或字符串。
    """
    if isinstance(value, str):
        tokens = value.split(",")
    elif isinstance(value, (list, tuple)):
        tokens = list(value)
    else:
        raise InputError(f"种子列表必须是字符串或列表，收到 {type(value).__name__}")

    if not tokens:
        raise InputError("种子列表不能为空")

    seeds = []
    for token in tokens:
        text = token.strip() if isinstance(token, str) else token
        if isinstance(text, str):
            if not text:
                raise InputError("种子不能为空")
            # 仅接受纯十进制无符号整数（允许前导零），拒绝 +3、3.0、0x1 等。
            if not text.isdigit():
                raise InputError(f"种子必须为非负整数，收到 {token!r}")
            seed = int(text)
        else:
            if isinstance(text, bool) or not isinstance(text, int):
                raise InputError(f"种子必须为非负整数，收到 {token!r}")
            seed = text
        if seed < 0:
            raise InputError(f"种子不能为负数，收到 {seed}")
        if seed in seeds:
            raise InputError(f"种子不能重复：{seed}")
        seeds.append(seed)
    return seeds


def _validate_common(*, sources, testbench, top, duration):
    """校验源文件、测试台、顶层名与时长，返回规范化 ``(amount, unit)``。"""
    if not isinstance(top, str) or not top.strip():
        raise InputError("顶层模块名不能为空")

    amount, unit, _fs = normalize_duration(duration)

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

    return amount, unit


def _validate(config):
    """校验单次运行的全部输入，非法时抛 :class:`InputError`。"""
    if isinstance(config.seed, bool) or not isinstance(config.seed, int):
        raise InputError(f"随机种子必须为非负整数，收到 {config.seed!r}")
    if config.seed < 0:
        raise InputError(f"随机种子不能为负数，收到 {config.seed}")

    return _validate_common(
        sources=config.sources, testbench=config.testbench,
        top=config.top, duration=config.duration,
    )


def _validate_regress(config):
    """校验回归配置：通用字段 + 种子列表。"""
    amount, unit = _validate_common(
        sources=config.sources, testbench=config.testbench,
        top=config.top, duration=config.duration,
    )
    config.seeds = parse_seeds(config.seeds)
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


def _prepare_and_compile(config, amount, unit):
    """创建工作目录、生成看门狗并编译，返回编译相关产物。

    :returns: ``(vvp_output, watchdog_path, returncode, stdout, stderr,
        compile_command)``；编译是否成功由调用方按 returncode 判断。
    """
    os.makedirs(config.workdir, exist_ok=True)
    vvp_output = os.path.join(config.workdir, "rtl_lab_sim.vvp")
    watchdog_path = os.path.join(config.workdir, "rtl_lab_watchdog.v")

    # 测试台排在设计源文件之后，按给定顺序一起编译。
    all_sources = list(config.sources) + [config.testbench]

    make_watchdog_source(watchdog_path, amount, unit)
    compile_files = all_sources + [watchdog_path]

    rc, cout, cerr, compile_command = compile_sources(
        source_files=compile_files,
        top=config.top.strip(),
        output_path=vvp_output,
        extra_roots=[WATCHDOG_MODULE],
    )
    return vvp_output, watchdog_path, rc, cout, cerr, compile_command


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

    # ---- 编译 ----
    (vvp_output, watchdog_path,
     rc, cout, cerr, compile_command) = _prepare_and_compile(
        config, amount, unit
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


def _join_streams(parts):
    """拼接多个种子的同一流输出，段间保证换行分隔。"""
    joined = ""
    for part in parts:
        if not part:
            continue
        joined += part
        if not part.endswith("\n"):
            joined += "\n"
    return joined


def _emit_regress_report(config, duration_str, status, diagnostics, runs,
                         vvp_output, watchdog_path, compile_command,
                         raw_stdout="", raw_stderr=""):
    """组装并落盘回归报告，返回带原始流的 :class:`Report`。"""
    data = build_regress_report(
        tool=TOOL_NAME,
        compile_command=compile_command,
        sources=config.sources,
        testbench=config.testbench,
        top=config.top.strip(),
        duration=duration_str,
        seeds=config.seeds,
        status=status,
        diagnostics=diagnostics,
        runs=runs,
        workdir=config.workdir,
        known_paths=(vvp_output, watchdog_path, config.report_path),
    )
    report = Report(data, raw_stdout=raw_stdout, raw_stderr=raw_stderr)
    if config.report_path:
        write_report(report, config.report_path)
    return report


def regress(config=None, **kwargs):
    """多随机种子回归：校验一次、编译一次，按种子顺序依次仿真。

    可传 :class:`RegressConfig`，也可用关键字参数直接构造。

    行为：

    - 编译失败：生成无 ``runs`` 的 compile_failed 报告，抛
      :class:`CompilationError`；
    - 某一种子仿真非零退出：保留已有结果与诊断，停止后续种子，生成
      simulation_failed 报告，抛 :class:`SimulationError`；
    - 断言失败：记录后继续执行剩余种子，全部结束后生成 assertion_failed
      报告，抛带 ``assertion_failed=True`` 的 :class:`SimulationError`；
    - 全部通过：返回 passed 报告。

    :raises InputError: 输入或种子列表非法（退出码 2，不生成报告）。
    :raises ToolError: iverilog/vvp 无法启动（退出码 4，不生成报告）。
    """
    if config is None:
        config = RegressConfig(**kwargs)
    elif kwargs:
        raise TypeError("regress() 不能同时传入 RegressConfig 与关键字参数")

    amount, unit = _validate_regress(config)
    duration_str = f"{amount}{unit}"

    # ---- 编译（仅一次）----
    (vvp_output, watchdog_path,
     rc, cout, cerr, compile_command) = _prepare_and_compile(
        config, amount, unit
    )
    if rc != 0:
        diagnostics = []
        if cout:
            diagnostics.append(cout)
        if cerr:
            diagnostics.append(cerr)
        report = _emit_regress_report(
            config, duration_str, "compile_failed", diagnostics, [],
            vvp_output, watchdog_path, compile_command,
            raw_stdout=cout, raw_stderr=cerr,
        )
        raise CompilationError(
            "编译失败（iverilog 返回非零状态）", report=report,
            raw_stdout=cout, raw_stderr=cerr,
        )

    # ---- 各种子依次仿真 ----
    runs = []
    stdout_parts = []
    stderr_parts = []
    final_status = "passed"
    assertion_failed_names = set()

    for seed in config.seeds:
        rc, sout, serr, run_command = run_simulation(
            vvp_path=os.path.basename(vvp_output),
            seed=seed, cwd=config.workdir,
        )
        stdout_parts.append(sout)
        stderr_parts.append(serr)

        collector = ResultCollector()
        collector.feed_text(sout)

        diagnostics = []
        if sout:
            diagnostics.append(sout)
        if serr:
            diagnostics.append(serr)

        if rc != 0:
            run_status = "simulation_failed"
            final_status = "simulation_failed"
        elif collector.has_failure:
            run_status = "assertion_failed"
            final_status = "assertion_failed"
            assertion_failed_names.update(
                n for n, a in collector.assertions.items()
                if a["status"] == "failed"
            )
        else:
            run_status = "passed"

        runs.append({
            "seed": seed,
            "status": run_status,
            "diagnostics": diagnostics,
            "assertions": collector.assertions,
            "coverage": collector.coverage,
            "command": {
                "compile": compile_command,
                "simulate": run_command,
            },
        })

        # 仿真异常退出：保留已有结果与诊断，停止后续种子。
        if rc != 0:
            break

    raw_stdout = _join_streams(stdout_parts)
    raw_stderr = _join_streams(stderr_parts)

    # 顶层诊断仅放 run 之外的额外信息；逐种子诊断已在 runs 中。
    report = _emit_regress_report(
        config, duration_str, final_status, [], runs,
        vvp_output, watchdog_path, compile_command,
        raw_stdout=raw_stdout, raw_stderr=raw_stderr,
    )

    if final_status == "simulation_failed":
        raise SimulationError(
            f"种子 {runs[-1]['seed']} 的仿真进程以非零状态退出"
            f"（退出码 {rc}）",
            report=report, raw_stdout=raw_stdout, raw_stderr=raw_stderr,
        )
    if final_status == "assertion_failed":
        raise SimulationError(
            "断言失败：" + ", ".join(sorted(assertion_failed_names)),
            report=report, assertion_failed=True,
            raw_stdout=raw_stdout, raw_stderr=raw_stderr,
        )
    return report
