"""统一验证执行：多测试台编译/仿真编排与可复现报告（schema v3/v5）。

在 ``run`` / ``regress`` 之上提供新的公开入口 :func:`verify`：

- 输入：设计编译范围（源文件）、测试台或测试选择、覆盖率配置、报告输出
  位置与运行标识 ``run_id``；
- 每个测试台独立编译、独立仿真（各自产物置于工作目录下的独立子目录），
  可选择并发执行，但结果一律按测试选择顺序收集；
- 默认产出 schema v3 统一验证报告，字段与顺序稳定，可复现、可追溯。

多种子矩阵（``VerifyConfig.seeds`` 非空，schema v5）：

- ``--jobs`` 的并发只发生在测试台之间；每个未跳过测试台只编译一次，
  同一测试台的种子按列表顺序串行以 ``+SEED=<seed>`` 仿真；
- 结果按测试选择顺序、同台按种子顺序排列：测试台条目新增 ``seeds`` 与
  ``runs``（每个 run 含 seed/status/reason/diagnostics/assertions/
  coverage/simulate）；
- 全部种子通过该台才 passed；某种子进程非零退出记 simulation_failed 并
  停止该台后续种子；断言失败记 assertion_failed 但继续后续种子；正常
  结束却缺断言或覆盖率统计记 incomplete_statistics；编译失败时 runs
  为空、reason 为 compilation_failed；
- 断言按名取跨种子终态并累加 fail_count；coverage 按名跨种子累加 hits
  后再跨台合并（hit_testbenches 去重）。种子沿用 regress 的解析与校验，
  且不可与逐测试台种子（``--tb-seed``）并用。

校验规则（生成报告之前）：

- ``run_id`` 为空（含纯空白）抛 :class:`ValueError`；
- 报告输出位置不可写抛 :class:`OSError`；
- 结果不属于本次运行、测试台命名冲突、同一测试台重复记录抛
  :class:`RuntimeError`；
- 没有可执行测试台或没有任何覆盖率结果抛 :class:`RuntimeError`，
  且保留已有报告不被覆盖。

测试台失败、断言失败或覆盖率低于阈值时仍生成完整报告，总体 ``result``
记为 ``failed``；只有全部检查通过且没有跳过必测项才记为 ``passed``。

可选 ``timeout``（正整数秒）对每次独立启动的 iverilog/vvp 进程分别
计时：超时即终止该进程并在该阶段 stderr 追加稳定标记
``RTL_LAB_PROCESS_TIMEOUT``；编译超时等同编译失败（多种子时 runs 为
空），仿真超时等同仿真非零退出（多种子时停止该台后续种子）。

基线对比（``VerifyConfig.baseline_path``）：先按原流程生成当前结果，再把
当前报告与上次 ``verify`` 生成的基线报告按 name 对齐对比
（testbenches、assertions、coverage），在既有字段之后追加 ``comparison``；
有差异时总体 ``result`` 记为 ``failed``。单种子报告基线版为 schema v4；
多种子报告保持 schema v5（同样追加 ``comparison``）。基线不存在、不可读、
非合法 JSON、不是 rtl-lab 验证报告或版本不受支持时抛 :class:`InputError`，
不执行仿真、不生成或覆盖报告。

JUnit XML 输出（``VerifyConfig.junit_path``）：报告生成后，``junit_path``
非空时在 JSON 报告之外额外写出一份 UTF-8 JUnit XML（结构见
:mod:`rtl_lab.junit`），内容完全由报告决定、不含时间戳，相同报告生成相同
XML。``junit_path`` 为空或与 ``report_path`` / ``baseline_path`` 指向同一
文件时抛 :class:`InputError`；输出位置不可写或写入失败按前置条件失败处理
（抛 :class:`OSError`，退出 8），JSON 与 XML 均不生成或覆盖。本就不生成
报告的输入错误、工具缺失等路径同样不生成 XML。
"""

import hashlib
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .baseline import compare_verification_reports, load_baseline_report
from .duration import normalize_duration
from .errors import InputError
from .junit import write_junit
from .parser import ResultCollector
from .report import (
    Report,
    VERIFICATION_COMPARISON_SCHEMA_VERSION,
    build_verification_report,
    ensure_report_writable,
    write_report,
)
from .runner import (  # noqa: PLC2701
    SOURCE_SUFFIXES,
    _prepare_and_compile,
    parse_seeds,
    parse_timeout,
    validate_compile_options,
)
from .tools import run_simulation

#: 测试台原因枚举（稳定取值，不依赖平台文本）。
REASONS = (
    "passed",
    "compilation_failed",
    "simulation_failed",
    "assertion_failed",
    "incomplete_statistics",
    "skipped",
)


def _now_iso():
    """UTC ISO-8601 时间戳；这是报告中唯一允许跨复现变化的信息。"""
    return datetime.now(timezone.utc).isoformat()


def _safe_dir_name(name):
    """把测试台名转为文件系统安全的子目录名。"""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._-")
    return cleaned or "tb"


def _plan_tb_dirs(specs):
    """为各测试台规划唯一且稳定的子目录名（按逻辑名索引）。

    常规情况直接使用清洗后的逻辑名；仅当不同逻辑名清洗后撞名时，给撞名组
    中的每个名字追加由逻辑名导出的稳定短哈希后缀。同名输入在任何机器上都
    得到同一结果。
    """
    base = {s.name: _safe_dir_name(s.name) for s in specs}
    counts = {}
    for cleaned in base.values():
        counts[cleaned] = counts.get(cleaned, 0) + 1
    dirs = {}
    for name, cleaned in base.items():
        if counts[cleaned] > 1:
            suffix = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
            dirs[name] = f"{cleaned}_{suffix}"
        else:
            dirs[name] = cleaned
    return dirs


@dataclass
class TestSpec:
    """一个测试台的选择与执行属性。

    :param testbench: 测试台源文件路径。
    :param top: 顶层模块名；缺省取去掉后缀的文件 basename。
    :param name: 结果中使用的稳定测试台名（须在本次选择中唯一）；
        缺省取去掉后缀的文件 basename。
    :param required: 是否为必测项；跳过必测项会使总体结论 failed。
    :param skip: 是否跳过该测试台；跳过不编译、不仿真。
    :param seed: 该测试台使用的随机种子（``+SEED=<seed>``）。
    """

    testbench: str
    top: str = None
    name: str = None
    required: bool = True
    skip: bool = False
    seed: int = 0

    #: 告知 pytest 不要把本数据类当作测试类收集。
    __test__ = False

    def __post_init__(self):
        if self.name is None:
            self.name = os.path.splitext(os.path.basename(self.testbench))[0]
        if self.top is None:
            self.top = os.path.splitext(os.path.basename(self.testbench))[0]


@dataclass
class CoverageConfig:
    """覆盖率配置。

    :param threshold: 覆盖率达标阈值（0~1 的比率，命中点/总点）。
    :param points: 显式要求的覆盖率点名集合；缺省取实际统计点并集。
        点名但无任何统计的点记为 unavailable 并计入分母。
    """

    threshold: float = 1.0
    points: list = field(default_factory=list)

    def __post_init__(self):
        if isinstance(self.threshold, bool) or not isinstance(
            self.threshold, (int, float)
        ):
            raise ValueError("覆盖率阈值必须是数值")
        self.threshold = float(self.threshold)
        if not 0.0 <= self.threshold <= 1.0:
            raise ValueError("覆盖率阈值必须位于 0~1 之间")
        if self.points is None:
            self.points = []
        self.points = list(self.points)
        if any(not isinstance(p, str) or not p.strip() for p in self.points):
            raise ValueError("覆盖率点名必须是非空字符串")
        if len(set(self.points)) != len(self.points):
            raise ValueError("覆盖率点名不能重复")


@dataclass
class VerifyConfig:
    """一次统一验证的输入配置。

    :param sources: 设计源文件路径列表（所有测试台共享的编译范围，按序）。
    :param testbenches: :class:`TestSpec` 列表（测试选择与稳定顺序）。
    :param run_id: 运行标识，不能为空；同一标识用于结果归属与追溯。
    :param duration: 仿真时长字符串，如 ``"100ns"``。
    :param coverage: :class:`CoverageConfig`，缺省阈值 1.0。
    :param workdir: 工作目录（各测试台产物置于其下独立子目录）。
    :param report_path: JSON 报告输出路径，可为 None。
    :param junit_path: JUnit XML 输出路径，可为 None；非空时在 JSON 报告
        之外额外生成一份可复现的 JUnit XML（见 :mod:`rtl_lab.junit`）。
        不能为空字符串，也不得与 ``report_path`` / ``baseline_path``
        指向同一文件。
    :param jobs: 并发编译/仿真的测试台数，默认 1（顺序执行）；并发只发生
        在测试台之间，同一测试台的各种子始终串行。
    :param baseline_path: 上次 verify 生成的 JSON 报告路径，可为 None；
        指定时生成带 ``comparison`` 的对比报告。
    :param seeds: 多种子矩阵的非负整数种子列表（也接受逗号分隔字符串，
        由 :func:`rtl_lab.runner.parse_seeds` 解析）；非空时每个未跳过
        测试台编译一次、按列表顺序逐种子以 ``+SEED=<seed>`` 仿真，产出
        schema v5 报告。与 :attr:`TestSpec.seed`（``--tb-seed``）语义
        互斥：矩阵种子非空时各测试台不得另行指定种子。
    :param timeout: 每次 iverilog/vvp 进程调用的墙钟超时（正整数秒，
        也接受十进制字符串，由 :func:`rtl_lab.runner.parse_timeout`
        解析）；None 表示不限制。每个测试台独立编译、每个种子独立仿真，
        各次进程调用分别计时，不合并或平均预算。
    :param include_dirs: include 文件搜索目录列表（按给出顺序传给
        iverilog ``-I``，所有测试台共享）；省略时不传任何 include 参数。
    :param defines: 预处理宏定义列表（``NAME`` 或 ``NAME=VALUE``
        字符串，按给出顺序传给 iverilog ``-D``）；省略时不定义任何宏。
    :param parameters: 设计参数覆盖列表（``PATH=VALUE`` 字符串，PATH 为
        从所选顶层开始的参数层级名，按给出顺序传给 iverilog ``-P``，
        所有测试台共享）；省略时不覆盖任何参数。
    """

    sources: list
    testbenches: list
    run_id: str
    duration: str
    coverage: CoverageConfig = field(default_factory=CoverageConfig)
    workdir: str = "."
    report_path: str = None
    junit_path: str = None
    jobs: int = 1
    baseline_path: str = None
    seeds: object = None
    timeout: object = None
    include_dirs: list = field(default_factory=list)
    defines: list = field(default_factory=list)
    parameters: list = field(default_factory=list)

    def __post_init__(self):
        self.sources = list(self.sources)
        self.testbenches = [
            t if isinstance(t, TestSpec) else TestSpec(**t)
            for t in self.testbenches
        ]
        self.include_dirs = list(self.include_dirs or [])
        self.defines = list(self.defines or [])
        self.parameters = list(self.parameters or [])
        if self.coverage is None:
            self.coverage = CoverageConfig()
        elif not isinstance(self.coverage, CoverageConfig):
            self.coverage = CoverageConfig(**self.coverage)
        if isinstance(self.jobs, bool) or not isinstance(self.jobs, int):
            raise ValueError("jobs 必须是正整数")
        if self.jobs < 1:
            raise ValueError("jobs 必须是正整数")
        # 多种子矩阵沿用 regress 的种子解析与校验（空值/负数/非整数/
        # 重复均为 InputError）；None 表示传统单种子 verify。
        if self.seeds is None:
            self.seeds = []
        else:
            self.seeds = parse_seeds(self.seeds)
        # 超时沿用统一的正整数秒解析（零/负数/小数/非数字/空值均为
        # InputError）；None 表示不限制。
        self.timeout = parse_timeout(self.timeout)


def _validate(config):
    """生成前校验；规范化时长，返回 ``(amount, unit)``。"""
    if not isinstance(config.run_id, str) or not config.run_id.strip():
        raise ValueError("运行标识 run_id 不能为空")

    amount, unit, _fs = normalize_duration(config.duration)

    # include 目录、宏定义与参数覆盖与 run/regress 共用同一套校验规则。
    validate_compile_options(config)

    if not config.sources:
        raise InputError("至少需要一个设计源文件")

    def check_file(path, label):
        if not isinstance(path, str) or not path.strip():
            raise InputError(f"{label}路径不能为空")
        if not path.lower().endswith(SOURCE_SUFFIXES):
            raise InputError(f"{label} {path!r} 后缀不受支持：仅支持 .v/.sv")
        if not os.path.isfile(path):
            raise InputError(f"{label}不存在：{path}")

    for source in config.sources:
        check_file(source, "设计源文件")

    if not config.testbenches:
        raise InputError("至少需要一个测试台")

    names = set()
    for spec in config.testbenches:
        check_file(spec.testbench, "测试台文件")
        if not isinstance(spec.top, str) or not spec.top.strip():
            raise InputError("测试台顶层模块名不能为空")
        if not isinstance(spec.name, str) or not spec.name.strip():
            raise InputError("测试台名不能为空")
        if isinstance(spec.seed, bool) or not isinstance(spec.seed, int) \
                or spec.seed < 0:
            raise InputError(f"测试台 {spec.name!r} 的种子必须是非负整数")
        if spec.name in names:
            # 命名冲突：无法保证唯一可观察结果，直接拒绝。
            raise RuntimeError(f"测试台命名冲突：{spec.name!r}")
        names.add(spec.name)

    if not any(not t.skip for t in config.testbenches):
        # 没有可执行测试台：不生成新报告（已有报告保留）。
        raise RuntimeError("没有可执行的测试台（所有测试台均被跳过）")

    # 多种子矩阵与逐测试台种子（--tb-seed）互斥；CLI 在标志同时出现时
    # 先行拒绝，这里再兜底拒绝非零的逐测试台种子，避免被静默忽略。
    if config.seeds and any(
        isinstance(t.seed, bool) or not isinstance(t.seed, int)
        or t.seed != 0
        for t in config.testbenches
    ):
        raise InputError("多种子矩阵（--seeds）不可与 --tb-seed 并用")

    if config.report_path:
        ensure_report_writable(config.report_path)

    if config.junit_path is not None:
        if not isinstance(config.junit_path, str) \
                or not config.junit_path.strip():
            raise InputError("JUnit XML 输出路径不能为空")
        junit_abs = os.path.normpath(os.path.abspath(config.junit_path))
        for label, other in (
            ("报告", config.report_path),
            ("基线", config.baseline_path),
        ):
            if other and os.path.normpath(os.path.abspath(other)) == junit_abs:
                raise InputError(
                    f"JUnit XML 输出路径不能与{label}路径相同："
                    f"{config.junit_path}"
                )
        ensure_report_writable(config.junit_path)

    return amount, unit


class _TbConfig:
    """复用 runner 编译逻辑所需的最小配置视图。"""

    def __init__(self, sources, testbench, top, workdir, timeout=None,
                 include_dirs=(), defines=(), parameters=()):
        self.sources = sources
        self.testbench = testbench
        self.top = top
        self.workdir = workdir
        self.timeout = timeout
        self.include_dirs = include_dirs
        self.defines = defines
        self.parameters = parameters


def _simulate_once(vvp_output, tb_workdir, seed, timeout=None):
    """执行一次仿真并解析结果，返回 (rc, collector, diagnostics, command)。"""
    rc, sout, serr, run_command = run_simulation(
        vvp_path=os.path.basename(vvp_output),
        seed=seed, cwd=tb_workdir, timeout=timeout,
    )
    collector = ResultCollector()
    collector.feed_text(sout)
    diagnostics = []
    if sout:
        diagnostics.append(sout)
    if serr:
        diagnostics.append(serr)
    return rc, collector, diagnostics, run_command


def _run_single_seed(spec, config, amount, unit, run_token, tb_dir):
    """传统单种子（schema v3）路径：编译一次、仿真一次。"""
    start_time = _now_iso()

    if spec.skip:
        return {
            "run_token": run_token,
            "name": spec.name,
            "top": spec.top.strip(),
            "testbench_file": spec.testbench,
            "status": "skipped",
            "reason": "skipped",
            "required": spec.required,
            "start_time": start_time,
            "end_time": _now_iso(),
            "diagnostics": [],
            "assertions": {},
            "coverage": {},
            "commands": {"compile": [], "simulate": []},
        }

    tb_workdir = os.path.abspath(os.path.join(config.workdir, tb_dir))
    os.makedirs(tb_workdir, exist_ok=True)
    tb_config = _TbConfig(
        config.sources, spec.testbench, spec.top, tb_workdir,
        timeout=config.timeout,
        include_dirs=config.include_dirs, defines=config.defines,
        parameters=config.parameters,
    )

    (vvp_output, watchdog_path,
     rc, cout, cerr, compile_command) = _prepare_and_compile(
        tb_config, amount, unit
    )

    diagnostics = []
    if cout:
        diagnostics.append(cout)
    if cerr:
        diagnostics.append(cerr)

    if rc != 0:
        return {
            "run_token": run_token,
            "name": spec.name,
            "top": spec.top.strip(),
            "testbench_file": spec.testbench,
            "status": "failed",
            "reason": "compilation_failed",
            "required": spec.required,
            "start_time": start_time,
            "end_time": _now_iso(),
            "diagnostics": diagnostics,
            "assertions": {},
            "coverage": {},
            "commands": {"compile": compile_command, "simulate": []},
            "_artifacts": (vvp_output, watchdog_path),
        }

    rc, collector, sim_diagnostics, run_command = _simulate_once(
        vvp_output, tb_workdir, spec.seed, timeout=config.timeout
    )
    diagnostics.extend(sim_diagnostics)

    if rc != 0:
        status, reason = "failed", "simulation_failed"
    elif collector.has_failure:
        status, reason = "failed", "assertion_failed"
    elif not collector.assertions:
        # 正常结束但缺少断言统计：该测试台记为 failed。
        # 覆盖率整体缺失属于运行级判定（见 verify），不在此逐台判失败。
        status, reason = "failed", "incomplete_statistics"
    else:
        status, reason = "passed", "passed"

    return {
        "run_token": run_token,
        "name": spec.name,
        "top": spec.top.strip(),
        "testbench_file": spec.testbench,
        "status": status,
        "reason": reason,
        "required": spec.required,
        "start_time": start_time,
        "end_time": _now_iso(),
        "diagnostics": diagnostics,
        "assertions": collector.assertions,
        "coverage": collector.coverage,
        "commands": {"compile": compile_command, "simulate": run_command},
        "_artifacts": (vvp_output, watchdog_path),
    }


def _classify_seed_run(rc, collector):
    """执行阶段单子种子结果的 (status, reason)；status ∈ passed/failed。

    与 v3 逐台执行阶段保持同一裁决：仅“缺断言”在此判失败；“缺覆盖率”
    属运行级两级裁决（见 :func:`verify`），此处先按 passed 保留。
    """
    if rc != 0:
        return "failed", "simulation_failed"
    if collector.has_failure:
        return "failed", "assertion_failed"
    if not collector.assertions:
        return "failed", "incomplete_statistics"
    return "passed", "passed"


def _aggregate_matrix_tb(runs, *, hard_stop):
    """由各 run 结果推导测试台级 (status, reason)。

    仿真硬停止（某种子进程非零退出）固定 simulation_failed；否则
    assertion_failed 优先于 incomplete_statistics；全部通过才 passed。
    """
    if hard_stop:
        return "failed", "simulation_failed"
    reasons = {r["reason"] for r in runs if r["status"] == "failed"}
    if "assertion_failed" in reasons:
        return "failed", "assertion_failed"
    if "incomplete_statistics" in reasons:
        return "failed", "incomplete_statistics"
    return "passed", "passed"


def _run_seed_matrix(spec, config, amount, unit, run_token, tb_dir):
    """多种子矩阵（schema v5）路径：编译一次，同台种子按序串行仿真。

    - 仿真进程非零退出：该 run 记 simulation_failed，停止该台后续种子；
    - 断言失败：记 assertion_failed，但继续后续种子；
    - 正常结束却缺少断言或覆盖率统计：记 incomplete_statistics，继续后续
      种子（与断言失败同为可恢复的结果级失败）；
    - 全部种子均通过，该台才为 passed。
    """
    start_time = _now_iso()
    seeds = list(config.seeds)

    if spec.skip:
        return {
            "run_token": run_token,
            "name": spec.name,
            "top": spec.top.strip(),
            "testbench_file": spec.testbench,
            "status": "skipped",
            "reason": "skipped",
            "required": spec.required,
            "start_time": start_time,
            "end_time": _now_iso(),
            "diagnostics": [],
            "commands": {"compile": [], "simulate": []},
            "seeds": seeds,
            "runs": [],
        }

    tb_workdir = os.path.abspath(os.path.join(config.workdir, tb_dir))
    os.makedirs(tb_workdir, exist_ok=True)
    tb_config = _TbConfig(
        config.sources, spec.testbench, spec.top, tb_workdir,
        timeout=config.timeout,
        include_dirs=config.include_dirs, defines=config.defines,
        parameters=config.parameters,
    )

    (vvp_output, watchdog_path,
     rc, cout, cerr, compile_command) = _prepare_and_compile(
        tb_config, amount, unit
    )

    tb_diagnostics = []
    if cout:
        tb_diagnostics.append(cout)
    if cerr:
        tb_diagnostics.append(cerr)

    if rc != 0:
        # 编译失败：不仿真，runs 为空。
        return {
            "run_token": run_token,
            "name": spec.name,
            "top": spec.top.strip(),
            "testbench_file": spec.testbench,
            "status": "failed",
            "reason": "compilation_failed",
            "required": spec.required,
            "start_time": start_time,
            "end_time": _now_iso(),
            "diagnostics": tb_diagnostics,
            "commands": {"compile": compile_command, "simulate": []},
            "seeds": seeds,
            "runs": [],
            "_artifacts": (vvp_output, watchdog_path),
        }

    runs = []
    hard_stop = False
    for seed in seeds:
        rc, collector, sim_diagnostics, run_command = _simulate_once(
            vvp_output, tb_workdir, seed, timeout=config.timeout
        )
        run_status, run_reason = _classify_seed_run(rc, collector)
        runs.append({
            "seed": seed,
            "status": run_status,
            "reason": run_reason,
            "diagnostics": sim_diagnostics,
            "assertions": collector.assertions,
            "coverage": collector.coverage,
            "command": {"simulate": run_command},
        })
        # 仿真进程非零退出：停止该台后续种子（断言/统计类失败继续）。
        if run_reason == "simulation_failed":
            hard_stop = True
            break

    tb_status, tb_reason = _aggregate_matrix_tb(runs, hard_stop=hard_stop)

    # 条目级 diagnostics 保留编译输出（各 run 的仿真输出在其 runs 内）；
    # 条目级 simulate 命令取首个实际执行种子，作为该台代表 argv。
    return {
        "run_token": run_token,
        "name": spec.name,
        "top": spec.top.strip(),
        "testbench_file": spec.testbench,
        "status": tb_status,
        "reason": tb_reason,
        "required": spec.required,
        "start_time": start_time,
        "end_time": _now_iso(),
        "diagnostics": tb_diagnostics,
        "commands": {
            "compile": compile_command,
            "simulate": runs[0]["command"]["simulate"] if runs else [],
        },
        "seeds": seeds,
        "runs": runs,
        "_artifacts": (vvp_output, watchdog_path),
    }


def _execute_testbench(spec, config, amount, unit, run_token, tb_dir):
    """编译并仿真单个测试台；按配置分派单种子或多种子矩阵路径。"""
    if config.seeds:
        return _run_seed_matrix(
            spec, config, amount, unit, run_token, tb_dir
        )
    return _run_single_seed(spec, config, amount, unit, run_token, tb_dir)


def verify(config=None, **kwargs):
    """执行统一验证并生成报告（默认 schema v3；多种子矩阵为 schema v5）。

    可传 :class:`VerifyConfig`，也可用关键字参数直接构造。

    :returns: 始终返回 :class:`Report`（含总体 ``result``）；测试台失败、
        断言失败或覆盖率不达标时不抛异常，报告 ``result`` 为 ``failed``。
    :raises ValueError: ``run_id`` 为空或覆盖率/jobs 配置非法。
    :raises OSError: 报告输出位置不可写。
    :raises InputError: 源文件/测试台/时长/种子矩阵等输入非法，或
        ``--seeds`` 与 ``--tb-seed`` 并用，或基线报告不存在、不可读、
        非合法 JSON、非 rtl-lab 验证报告、版本不受支持，或
        ``junit_path`` 为空或与报告/基线路径指向同一文件。
    :raises RuntimeError: 结果归属错误、命名冲突、无可执行测试台或无任何
        覆盖率结果；这几种情况不覆盖已有报告。
    """
    if config is None:
        config = VerifyConfig(**kwargs)
    elif kwargs:
        raise TypeError("verify() 不能同时传入 VerifyConfig 与关键字参数")

    amount, unit = _validate(config)

    # 基线属于输入校验：非法基线抛 InputError，不执行仿真、不生成报告。
    baseline = None
    if config.baseline_path:
        baseline = load_baseline_report(config.baseline_path)
    duration_str = f"{amount}{unit}"
    run_token = f"{config.run_id.strip()}"
    tb_dirs = _plan_tb_dirs(config.testbenches)

    # 并发执行（jobs=1 即顺序）；无论完成顺序如何，结果按键回填为选择顺序。
    indexed = list(enumerate(config.testbenches))
    outcomes_by_index = {}
    if config.jobs == 1:
        for index, spec in indexed:
            outcomes_by_index[index] = _execute_testbench(
                spec, config, amount, unit, run_token, tb_dirs[spec.name]
            )
    else:
        with ThreadPoolExecutor(max_workers=config.jobs) as pool:
            futures = {
                pool.submit(
                    _execute_testbench, spec, config, amount, unit,
                    run_token, tb_dirs[spec.name],
                ): index
                for index, spec in indexed
            }
            for future, index in futures.items():
                outcomes_by_index[index] = future.result()

    outcomes = [outcomes_by_index[i] for i, _ in indexed]

    # 结果归属：每条结果必须属于本次运行。
    for outcome in outcomes:
        if outcome.get("run_token") != run_token:
            raise RuntimeError(
                f"测试结果不属于本次运行 {config.run_id!r}：{outcome['name']!r}"
            )
    names = [o["name"] for o in outcomes]
    if len(set(names)) != len(names):
        raise RuntimeError("同一测试台被重复记录")

    executed = [o for o in outcomes if o["status"] != "skipped"]
    if not executed:
        raise RuntimeError("没有可执行的测试台结果")

    if config.seeds:
        # 多种子矩阵：两级裁决下沉到 run 粒度。
        run_has_coverage = any(
            r["coverage"] for o in executed for r in o["runs"]
        )
        if run_has_coverage:
            for o in executed:
                changed = False
                for r in o["runs"]:
                    if r["status"] == "passed" and not r["coverage"]:
                        # 运行在别处收集到了覆盖率，而该种子自身一个覆盖点
                        # 都没有：正常结束却统计缺失，记为 failed。
                        r["status"] = "failed"
                        r["reason"] = "incomplete_statistics"
                        changed = True
                if changed:
                    hard_stop = any(
                        r["reason"] == "simulation_failed"
                        for r in o["runs"]
                    )
                    o["status"], o["reason"] = _aggregate_matrix_tb(
                        o["runs"], hard_stop=hard_stop
                    )
        elif not any(o["status"] == "failed" for o in executed):
            raise RuntimeError("没有任何覆盖率结果")
    else:
        # 覆盖率缺失的两级判定，各自有唯一可观察结果：
        # - 运行级：所有测试台都正常结束且有断言，但整个运行没有任何覆盖率结果，
        #   无法评估覆盖率 => RuntimeError，保留已有报告；
        # - 测试台级：运行在别处确实收集到了覆盖率，而某个正常结束的测试台自身
        #   没有任何覆盖率点 => 该台统计缺失，记为 failed。
        # 编译/仿真/断言硬失败优先：即使全局无覆盖率也照常生成 failed 报告。
        run_has_coverage = any(o["coverage"] for o in executed)
        if run_has_coverage:
            for o in executed:
                if o["status"] == "passed" and not o["coverage"]:
                    o["status"] = "failed"
                    o["reason"] = "incomplete_statistics"
        elif not any(o["status"] == "failed" for o in executed):
            raise RuntimeError("没有任何覆盖率结果")

    artifacts = []
    for o in outcomes:
        artifacts.extend(o.pop("_artifacts", ()))

    known_paths = tuple(
        [config.report_path] if config.report_path else []
    ) + tuple(artifacts)

    data, _result = build_verification_report(
        run_id=config.run_id.strip(),
        sources=config.sources,
        testbench_files=[s.testbench for s in config.testbenches],
        coverage_config={
            "threshold": config.coverage.threshold,
            "points": config.coverage.points,
        },
        duration=duration_str,
        # 顶层命令给出代表性编译/仿真 argv（取自第一个实际执行的测试台），
        # 每个测试台各自的命令在其条目内完整记录。
        compile_command=executed[0]["commands"]["compile"],
        simulate_command=executed[0]["commands"]["simulate"],
        outcomes=outcomes,
        generated_at=_now_iso(),
        workdir=config.workdir,
        known_paths=known_paths,
        seeds=config.seeds,
    )

    if baseline is not None:
        # 基线对比：既有字段及顺序保持不变，comparison 追加在最后；
        # 有差异时总体结论记为 failed（无论当前检查是否通过）。
        # schema 版本沿用当前报告（v3 单种子 -> v4；v5 多种子仍为 v5）。
        if not config.seeds:
            data["schema_version"] = VERIFICATION_COMPARISON_SCHEMA_VERSION
        comparison = compare_verification_reports(
            data, baseline,
            baseline_path=config.baseline_path, workdir=config.workdir,
        )
        data["comparison"] = comparison
        if not comparison["passed"]:
            data["result"] = "failed"

    report = Report(data)
    # JUnit XML 与 JSON 报告同源同内容；先写 XML，若其写入失败（退出 8）
    # 则 JSON 也不生成或覆盖，两种输出保持一致。
    if config.junit_path:
        write_junit(report, config.junit_path)
    if config.report_path:
        write_report(report, config.report_path)
    return report
