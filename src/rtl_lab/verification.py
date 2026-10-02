"""统一验证执行：多测试台编译/仿真编排与可复现报告（schema v3）。

在 ``run`` / ``regress`` 之上提供新的公开入口 :func:`verify`：

- 输入：设计编译范围（源文件）、测试台或测试选择、覆盖率配置、报告输出
  位置与运行标识 ``run_id``；
- 每个测试台独立编译、独立仿真（各自产物置于工作目录下的独立子目录），
  可选择并发执行，但结果一律按测试选择顺序收集；
- 产出 schema v3 统一验证报告，字段与顺序稳定，可复现、可追溯。

校验规则（生成报告之前）：

- ``run_id`` 为空（含纯空白）抛 :class:`ValueError`；
- 报告输出位置不可写抛 :class:`OSError`；
- 结果不属于本次运行、测试台命名冲突、同一测试台重复记录抛
  :class:`RuntimeError`；
- 没有可执行测试台或没有任何覆盖率结果抛 :class:`RuntimeError`，
  且保留已有报告不被覆盖。

测试台失败、断言失败或覆盖率低于阈值时仍生成完整报告，总体 ``result``
记为 ``failed``；只有全部检查通过且没有跳过必测项才记为 ``passed``。
"""

import hashlib
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .duration import normalize_duration
from .errors import InputError
from .parser import ResultCollector
from .report import (
    Report,
    build_verification_report,
    ensure_report_writable,
    write_report,
)
from .runner import SOURCE_SUFFIXES, _prepare_and_compile  # noqa: PLC2701
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
    :param jobs: 并发编译/仿真的测试台数，默认 1（顺序执行）。
    """

    sources: list
    testbenches: list
    run_id: str
    duration: str
    coverage: CoverageConfig = field(default_factory=CoverageConfig)
    workdir: str = "."
    report_path: str = None
    jobs: int = 1

    def __post_init__(self):
        self.sources = list(self.sources)
        self.testbenches = [
            t if isinstance(t, TestSpec) else TestSpec(**t)
            for t in self.testbenches
        ]
        if self.coverage is None:
            self.coverage = CoverageConfig()
        elif not isinstance(self.coverage, CoverageConfig):
            self.coverage = CoverageConfig(**self.coverage)
        if isinstance(self.jobs, bool) or not isinstance(self.jobs, int):
            raise ValueError("jobs 必须是正整数")
        if self.jobs < 1:
            raise ValueError("jobs 必须是正整数")


def _validate(config):
    """生成前校验；规范化时长，返回 ``(amount, unit)``。"""
    if not isinstance(config.run_id, str) or not config.run_id.strip():
        raise ValueError("运行标识 run_id 不能为空")

    amount, unit, _fs = normalize_duration(config.duration)

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

    if config.report_path:
        ensure_report_writable(config.report_path)

    return amount, unit


class _TbConfig:
    """复用 runner 编译逻辑所需的最小配置视图。"""

    def __init__(self, sources, testbench, top, workdir):
        self.sources = sources
        self.testbench = testbench
        self.top = top
        self.workdir = workdir


def _execute_testbench(spec, config, amount, unit, run_token, tb_dir):
    """编译并仿真单个测试台，返回归属到本次运行的结果 dict。"""
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
        config.sources, spec.testbench, spec.top, tb_workdir
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
        }

    rc, sout, serr, run_command = run_simulation(
        vvp_path=os.path.basename(vvp_output),
        seed=spec.seed, cwd=tb_workdir,
    )

    collector = ResultCollector()
    collector.feed_text(sout)
    if sout:
        diagnostics.append(sout)
    if serr:
        diagnostics.append(serr)

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


def verify(config=None, **kwargs):
    """执行统一验证并生成 schema v3 报告。

    可传 :class:`VerifyConfig`，也可用关键字参数直接构造。

    :returns: 始终返回 :class:`Report`（含总体 ``result``）；测试台失败、
        断言失败或覆盖率不达标时不抛异常，报告 ``result`` 为 ``failed``。
    :raises ValueError: ``run_id`` 为空或覆盖率/jobs 配置非法。
    :raises OSError: 报告输出位置不可写。
    :raises InputError: 源文件/测试台/时长等输入非法。
    :raises RuntimeError: 结果归属错误、命名冲突、无可执行测试台或无任何
        覆盖率结果；这几种情况不覆盖已有报告。
    """
    if config is None:
        config = VerifyConfig(**kwargs)
    elif kwargs:
        raise TypeError("verify() 不能同时传入 VerifyConfig 与关键字参数")

    amount, unit = _validate(config)
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
    )
    report = Report(data)
    if config.report_path:
        write_report(report, config.report_path)
    return report
