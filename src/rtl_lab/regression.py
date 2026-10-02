"""多随机种子回归流程。

入口：:func:`regress`。流程：

1. 校验输入（源文件存在性与后缀、顶层名、时长、种子列表）；
2. 按给定顺序编译设计源文件与测试台，以顶层模块为根，并注入仿真时长看门狗
   （所有种子共用一次编译产物）；
3. 按种子顺序依次仿真，随机种子通过 ``+SEED=<seed>`` plusarg 传入测试台；
4. 汇总各种子的 ASSERT/COVER 记录，生成 schema_version 2 的 JSON 报告。

任一运行仿真进程非零退出时保留已有结果并停止后续种子；断言最终失败时
仍执行剩余种子，最后统一报告。
"""

import os

from .duration import normalize_duration
from .errors import (
    InputError,
    CompilationError,
    SimulationError,
)
from .parser import ResultCollector
from .report import Report, build_regression_report, write_report
from .runner import TOOL_NAME, _check_sources, _check_top
from .tools import (
    WATCHDOG_MODULE,
    compile_sources,
    make_watchdog_source,
    run_simulation,
)


class RegressConfig:
    """一次多随机种子回归的输入配置。

    :param sources: 设计源文件路径列表（按编译顺序）。
    :param testbench: 测试台源文件路径。
    :param top: 顶层模块名。
    :param duration: 仿真时长字符串，如 ``"100ns"``。
    :param seeds: 随机种子列表（非负整数，非空且无重复），按给定顺序依次仿真。
    :param workdir: 工作目录（编译产物与临时文件置于其中）。
    :param report_path: JSON 报告输出路径，可为 None。
    """

    def __init__(self, *, sources, testbench, top, duration, seeds,
                 workdir=".", report_path=None):
        self.sources = list(sources)
        self.testbench = testbench
        self.top = top
        self.duration = duration
        self.seeds = list(seeds)
        self.workdir = workdir
        self.report_path = report_path


def _validate_seeds(seeds):
    """校验种子列表：非空、非负整数且无重复。"""
    if not seeds:
        raise InputError("至少需要一个随机种子")
    seen = set()
    for seed in seeds:
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise InputError(f"随机种子必须为非负整数，收到 {seed!r}")
        if seed < 0:
            raise InputError(f"随机种子不能为负数，收到 {seed}")
        if seed in seen:
            raise InputError(f"随机种子重复：{seed}")
        seen.add(seed)


def parse_seeds(text):
    """解析逗号分隔的种子列表（CLI ``--seeds`` 的参数形式）。

    空值、负数、非整数或重复项均抛 :class:`InputError`。
    """
    if not isinstance(text, str) or not text.strip():
        raise InputError(f"种子列表不能为空，收到 {text!r}")
    seeds = []
    for token in text.split(","):
        token = token.strip()
        if not token:
            raise InputError(f"种子列表包含空项：{text!r}")
        try:
            seeds.append(int(token))
        except ValueError:
            raise InputError(f"随机种子必须为非负整数，收到 {token!r}")
    _validate_seeds(seeds)
    return seeds


def _validate(config):
    """校验全部输入，非法时抛 :class:`InputError`。"""
    _check_top(config.top)
    _validate_seeds(config.seeds)

    # 时长（正整数 + 受支持单位）。
    amount, unit, _fs = normalize_duration(config.duration)

    _check_sources(config.sources, config.testbench)

    return amount, unit


def _aggregate(runs):
    """跨种子汇总断言与覆盖率，均按首次出现顺序排列。

    断言：任一种子最终失败则汇总状态为 failed，``fail_count`` 为各种子之和；
    覆盖率：``hits`` 为各种子命中之和，``hit_runs`` 为命中该点的种子数。
    """
    assertions = {}
    coverage = {}
    for run in runs:
        for name, a in run["assertions"].items():
            entry = assertions.setdefault(
                name, {"status": "passed", "fail_count": 0}
            )
            entry["fail_count"] += a["fail_count"]
            if a["status"] == "failed":
                entry["status"] = "failed"
        for name, hits in run["coverage"].items():
            entry = coverage.setdefault(name, {"hits": 0, "hit_runs": 0})
            entry["hits"] += hits
            if hits:
                entry["hit_runs"] += 1
    return assertions, coverage


def _emit_report(config, duration_str, status, diagnostics, runs, raw_runs,
                 failed_seeds, assertions, coverage,
                 vvp_output, watchdog_path,
                 compile_command=None, run_command=None,
                 raw_stdout="", raw_stderr=""):
    """组装并落盘回归报告，返回带原始流的 :class:`Report`。"""
    data = build_regression_report(
        tool=TOOL_NAME,
        command={
            "compile": compile_command or [],
            "simulate": run_command or [],
        },
        sources=config.sources,
        testbench=config.testbench,
        top=config.top.strip(),
        duration=duration_str,
        seeds=config.seeds,
        status=status,
        diagnostics=diagnostics,
        assertions=assertions,
        coverage=coverage,
        runs=runs,
        failed_seeds=failed_seeds,
        workdir=config.workdir,
        known_paths=(vvp_output, watchdog_path, config.report_path),
    )
    report = Report(data, raw_stdout=raw_stdout, raw_stderr=raw_stderr)
    #: 每个已执行种子的原始 (stdout, stderr)，仅供控制台回放，不入 JSON。
    report.raw_runs = raw_runs
    if config.report_path:
        write_report(report, config.report_path)
    return report


def regress(config=None, **kwargs):
    """执行一次多随机种子回归。

    可传 :class:`RegressConfig`，也可用关键字参数直接构造。

    :returns: 全部种子通过时返回报告 dict。
    :raises InputError: 输入校验失败（退出码 2，不生成报告）。
    :raises CompilationError: 编译失败（退出码 3，生成无 runs 的
        compile_failed 报告）。
    :raises ToolError: iverilog/vvp 无法启动（退出码 4，发生在报告生成之前）。
    :raises SimulationError: 任一仿真进程非零退出（5，保留已有结果并停止
        后续种子，生成 simulation_failed 报告）或最终断言失败（6，执行
        剩余种子后生成 assertion_failed 报告）。
    """
    if config is None:
        config = RegressConfig(**kwargs)
    elif kwargs:
        raise TypeError("regress() 不能同时传入 RegressConfig 与关键字参数")

    amount, unit = _validate(config)

    os.makedirs(config.workdir, exist_ok=True)
    vvp_output = os.path.join(config.workdir, "rtl_lab_sim.vvp")
    watchdog_path = os.path.join(config.workdir, "rtl_lab_watchdog.v")

    # 测试台排在设计源文件之后，按给定顺序一起编译。
    all_sources = list(config.sources) + [config.testbench]

    make_watchdog_source(watchdog_path, amount, unit)
    compile_files = all_sources + [watchdog_path]

    # ---- 编译（所有种子共用一次编译产物）----
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
        report = _emit_report(
            config, f"{amount}{unit}", "compile_failed", diagnostics,
            runs=[], raw_runs=[], failed_seeds=[],
            assertions={}, coverage={},
            vvp_output=vvp_output, watchdog_path=watchdog_path,
            compile_command=compile_command,
            raw_stdout=cout, raw_stderr=cerr,
        )
        raise CompilationError(
            "编译失败（iverilog 返回非零状态）", report=report,
            raw_stdout=cout, raw_stderr=cerr,
        )

    # ---- 按种子顺序依次仿真 ----
    # vvp 以工作目录为 cwd 运行，故产物使用 basename 定位。
    runs = []
    raw_runs = []
    simulate_command = []
    sim_failed_rc = None
    for seed in config.seeds:
        rc, sout, serr, run_command = run_simulation(
            vvp_path=os.path.basename(vvp_output),
            seed=seed, cwd=config.workdir,
        )
        if not simulate_command:
            simulate_command = run_command

        collector = ResultCollector()
        collector.feed_text(sout)

        diagnostics = []
        if sout:
            diagnostics.append(sout)
        if serr:
            diagnostics.append(serr)

        raw_runs.append((sout, serr))
        if rc != 0:
            # 保留该次已有结果与诊断，并停止后续种子。
            runs.append({
                "seed": seed,
                "status": "simulation_failed",
                "diagnostics": diagnostics,
                "assertions": collector.assertions,
                "coverage": collector.coverage,
            })
            sim_failed_rc = rc
            break
        runs.append({
            "seed": seed,
            "status": "assertion_failed" if collector.has_failure else "passed",
            "diagnostics": diagnostics,
            "assertions": collector.assertions,
            "coverage": collector.coverage,
        })

    assertions, coverage = _aggregate(runs)
    failed_seeds = [r["seed"] for r in runs if r["status"] != "passed"]

    if sim_failed_rc is not None:
        status = "simulation_failed"
    elif failed_seeds:
        status = "assertion_failed"
    else:
        status = "passed"

    report = _emit_report(
        config, f"{amount}{unit}", status, [], runs, raw_runs, failed_seeds,
        assertions, coverage,
        vvp_output=vvp_output, watchdog_path=watchdog_path,
        compile_command=compile_command, run_command=simulate_command,
    )

    if sim_failed_rc is not None:
        raise SimulationError(
            f"仿真进程以非零状态退出（退出码 {sim_failed_rc}）",
            report=report,
            raw_stdout=raw_runs[-1][0], raw_stderr=raw_runs[-1][1],
        )
    if status == "assertion_failed":
        failed_names = [
            n for n, a in assertions.items() if a["status"] == "failed"
        ]
        raise SimulationError(
            "断言失败：" + ", ".join(failed_names),
            report=report, assertion_failed=True,
        )
    return report
