"""统一验证执行与报告（schema v3）。

在 ``run``（schema v1）/ ``regress``（schema v2）能力基线上，提供一次
校验、编译、仿真、收集并生成**统一验证报告**的公开入口 :func:`verify`：

- 支持多个测试台（:class:`TestSpec`），每个独立编译、独立仿真；
- 支持测试级跳过（``skipped``）与必测项（required）标记；
- 支持覆盖率阈值与必须命中的覆盖点检查；
- 报告对同一输入与同一 ``run_id`` 可复现：无机器路径（沿用 v1/v2 的
  脱敏规则）、键序固定、文件/测试/结果项稳定排序，不受临时文件顺序
  与完成先后影响；唯一允许在重复生成间变化的是各测试台的
  ``started_at`` / ``ended_at`` 时间戳；
- 测试台失败、断言失败或覆盖率低于阈值时**仍生成完整报告**，整体结论
  记为 ``failed``；只有全部检查通过且没有跳过必测项才记为 ``passed``；
- 校验与执行期错误按需求区分为内置 :class:`ValueError`（运行标识为空、
  配置语义非法）、:class:`OSError`（输出位置不可写）、
  :class:`RuntimeError`（结果不属于本次运行、命名冲突、重复记录、
  无可执行测试台、无任何覆盖率结果——后两者保留已有报告）。

schema v3 顶层键固定为（顺序即输出顺序）：

``schema_version``、``run_id``、``tool``、``config_summary``、
``compiled_files``、``testbenches``、``totals``、``assertions``、
``coverage_summary``、``conclusion``。
"""

import json
import os
import tempfile
from datetime import datetime, timezone

from .duration import normalize_duration
from .errors import InputError
from .parser import ResultCollector
from .report import (
    Report,
    sanitize_argv,
    sanitize_path,
    sanitize_text,
)
from .tools import (
    WATCHDOG_MODULE,
    compile_sources,
    make_watchdog_source,
    run_simulation,
)

#: 统一验证报告结构版本（区别于单次 v1 与回归 v2）。
VERIFY_SCHEMA_VERSION = 3

#: 允许的源文件后缀。
SOURCE_SUFFIXES = (".v", ".sv")

#: 报告使用的 tool 标识（与既有入口一致）。
TOOL_NAME = "icarus-verilog"

#: 测试台对外的三态结论枚举。
TB_PASSED = "passed"
TB_FAILED = "failed"
TB_SKIPPED = "skipped"

#: 断言状态枚举。
STATUS_PASSED = "passed"
STATUS_FAILED = "failed"

#: 测试台未通过时的结束原因枚举（不依赖平台文本）。
REASON_COMPILE_FAILED = "compile_failed"
REASON_SIMULATION_FAILED = "simulation_failed"
REASON_ASSERTION_FAILED = "assertion_failed"
REASON_MISSING_STATS = "missing_stats"

#: 执行结果内部状态（仿真/编译层），到对外三态的映射依据。
_INTERNAL_STATUSES = (
    TB_PASSED, REASON_COMPILE_FAILED,
    REASON_SIMULATION_FAILED, REASON_ASSERTION_FAILED,
)


def _utc_now_iso():
    """当前 UTC 时间的 ISO 8601 字符串（报告中唯一允许变化的信息）。"""
    return datetime.now(timezone.utc).isoformat()


class CoverageConfig:
    """覆盖率配置。

    :param threshold: 覆盖率阈值，按 ``命中覆盖点数 / 覆盖点总数``
        计算；比值低于阈值记为检查失败。None 表示不做阈值检查。覆盖点
        总数取“实际观测到的点”与 :attr:`points` 声明全集的并集，因此
        声明了却未被任何测试台打印 ``COVER`` 的点按 0 命中计入分母。
    :param points: 期望覆盖点名称全集（有序、不可重复）；未实际观测到
        的名称以 0 命中出现在覆盖率结果中，并计入 ``missing_points``。
    :param required_points: 必须命中的覆盖点名称；实际结果中存在且
        ``hits>0`` 才算满足，否则计入 ``missing_required_points``。
    """

    def __init__(self, *, threshold=None, points=None, required_points=None):
        self.threshold = threshold
        self.points = list(points) if points else []
        self.required_points = list(required_points) if required_points else []


class TestSpec:
    """一个测试台的选择与运行配置。

    :param testbench: 测试台源文件路径。
    :param top: 顶层模块名；缺省取配置级 ``default_top``。
    :param name: 测试的稳定唯一标识；缺省用测试台文件名（去后缀）。
        报告顺序与结果归属校验均以它为准。
    :param seed: 仿真随机种子（非负整数，通过 ``+SEED=<seed>`` 传入）。
    :param skip: 标记该测试台本次不执行（报告记 ``skipped``）。
    :param skip_reason: 跳过原因（可空，原样写入报告）。
    :param required: 是否为必测项；必测项被跳过则整体不能 ``passed``。
    """

    #: 防止 pytest 等测试框架把本配置类误判为测试类。
    __test__ = False

    def __init__(self, testbench, *, top=None, name=None, seed=0,
                 skip=False, skip_reason="", required=True):
        self.testbench = testbench
        self.top = top
        self.name = name
        self.seed = seed
        self.skip = skip
        self.skip_reason = skip_reason
        self.required = required

    @property
    def effective_name(self):
        """报告中使用的稳定名称：显式 name 优先，否则取文件 stem。"""
        if self.name:
            return self.name
        return os.path.splitext(os.path.basename(self.testbench))[0]


class VerifyConfig:
    """统一验证执行的输入配置。

    :param sources: 设计源文件路径列表（按编译顺序）。
    :param testbenches: :class:`TestSpec` 列表（也接受等价 dict 列表）。
    :param default_top: 测试台未指定 ``top`` 时使用的默认顶层模块名。
    :param duration: 仿真时长字符串，如 ``"100ns"``。
    :param run_id: 运行标识，非空字符串；报告可复现性与归属锚点。
    :param coverage: :class:`CoverageConfig`，缺省表示不做阈值检查。
    :param report_path: JSON 报告输出路径，可为 None（只返回不写盘）。
    :param workdir: 工作目录（编译产物与临时看门狗置于其中）。
    """

    def __init__(self, *, sources, testbenches, default_top, duration,
                 run_id, coverage=None, report_path=None, workdir="."):
        self.sources = list(sources)
        self.testbenches = [
            t if isinstance(t, TestSpec) else TestSpec(**t)
            for t in testbenches
        ]
        self.default_top = default_top
        self.duration = duration
        self.run_id = run_id
        self.coverage = coverage if coverage is not None else CoverageConfig()
        self.report_path = report_path
        self.workdir = workdir


# ---------------------------------------------------------------------------
# 前置校验
# ---------------------------------------------------------------------------

def _require_run_id(run_id):
    """运行标识必须是非空字符串（去空白后非空），否则 ValueError。"""
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("运行标识（run_id）不能为空")
    return run_id


def _check_output_writable(report_path):
    """落盘前确认输出位置可写，否则抛 OSError 且不触碰任何文件。"""
    if report_path is None:
        return
    if not isinstance(report_path, str) or not report_path.strip():
        raise ValueError("报告输出路径不能为空")

    parent = os.path.dirname(os.path.abspath(report_path))
    if not os.path.isdir(parent):
        raise OSError(f"报告输出目录不存在：{parent}")
    if not os.access(parent, os.W_OK):
        raise OSError(f"报告输出目录不可写：{parent}")
    if os.path.lexists(report_path):
        if os.path.isdir(report_path):
            raise OSError(f"报告输出路径是目录：{report_path}")
        if not os.access(report_path, os.W_OK):
            raise OSError(f"报告文件不可写：{report_path}")


def _validate_config(config):
    """校验配置语义，返回规范化 ``(amount, unit)``。

    测试台名称重复属配置级命名冲突，抛 :class:`ValueError`，不落盘；
    结果级的同名/重复/归属冲突在 :func:`build_verify_report` 中拦截。
    """
    if not isinstance(config.default_top, str) \
            or not config.default_top.strip():
        raise ValueError("默认顶层模块名（default_top）不能为空")

    for spec in config.testbenches:
        if spec.top is not None and (
            not isinstance(spec.top, str) or not spec.top.strip()
        ):
            raise ValueError(
                f"测试台 {spec.testbench!r} 的顶层模块名不能为空"
            )
        if isinstance(spec.seed, bool) or not isinstance(spec.seed, int) \
                or spec.seed < 0:
            raise ValueError(
                f"测试台 {spec.testbench!r} 的种子必须为非负整数，"
                f"收到 {spec.seed!r}"
            )
        if not spec.effective_name.strip():
            raise ValueError(f"测试台 {spec.testbench!r} 的名称不能为空")

    try:
        amount, unit, _fs = normalize_duration(config.duration)
    except InputError as exc:
        # 统一验证链路的配置错误统一为 ValueError。
        raise ValueError(str(exc)) from exc

    def check_file(path, label):
        if not isinstance(path, str) or not path.strip():
            raise ValueError(f"{label}路径不能为空")
        if not path.lower().endswith(SOURCE_SUFFIXES):
            raise ValueError(
                f"{label} {path!r} 后缀不受支持：仅支持 .v 与 .sv"
            )
        if not os.path.isfile(path):
            raise ValueError(f"{label}不存在：{path}")

    if not config.sources:
        raise ValueError("至少需要一个设计源文件")
    for source in config.sources:
        check_file(source, "设计源文件")
    if not config.testbenches:
        raise ValueError("至少需要一个测试台")
    for spec in config.testbenches:
        check_file(spec.testbench, "测试台文件")

    seen_names = set()
    for spec in config.testbenches:
        name = spec.effective_name
        if name in seen_names:
            raise ValueError(f"测试台名称冲突：{name!r}")
        seen_names.add(name)

    cov = config.coverage
    if cov.threshold is not None:
        if isinstance(cov.threshold, bool) or not isinstance(
            cov.threshold, (int, float)
        ):
            raise ValueError("覆盖率阈值必须是数值或 None")
        if not 0 <= float(cov.threshold) <= 1:
            raise ValueError("覆盖率阈值必须落在 [0, 1] 区间")
    for point in cov.required_points:
        if not isinstance(point, str) or not point.strip():
            raise ValueError("必须命中的覆盖点名称必须为非空字符串")
    if len(set(cov.required_points)) != len(cov.required_points):
        raise ValueError("必须命中的覆盖点存在重复项")
    for point in cov.points:
        if not isinstance(point, str) or not point.strip():
            raise ValueError("覆盖点全集名称必须为非空字符串")
    if len(set(cov.points)) != len(cov.points):
        raise ValueError("覆盖点全集存在重复项")

    return amount, unit


# ---------------------------------------------------------------------------
# 纯构建层：执行结果 -> 统一报告
# ---------------------------------------------------------------------------

def _assertion_entries(assertions):
    return [
        {
            "name": name,
            "status": assertions[name]["status"],
            "fail_count": assertions[name]["fail_count"],
        }
        for name in assertions
    ]


def build_verify_report(*, run_id, sources, testbenches, default_top,
                        duration, coverage_config, results, workdir,
                        known_paths=()):
    """把每个测试台的执行结果汇总为统一报告 dict（schema v3）。

    结果收集链路的纯构建层：不执行编译/仿真、不写盘。集中负责结果归属、
    命名冲突、重复记录、统计缺失与空结果校验，非法时抛
    :class:`RuntimeError`，不产生报告。

    :param results: 测试结果的归属集合，接受两种形式：

      - 列表：元素为结果 dict（顺序即收集顺序，函数内部改按
        ``testbenches`` 的稳定顺序输出）；同一测试台名称出现两次即
        “同一测试重复记录”，抛 :class:`RuntimeError`；
      - 字典：键为测试台名称，值为结果 dict；键与结果内嵌的 ``name``
        不一致即“结果命名冲突”，抛 :class:`RuntimeError`。

      被跳过的测试台登记为 ``{"name", "run_id", "status": "skipped"}``；
      其余执行结果 dict 为：

        .. code-block:: python

            {"run_id": str, "name": str,
             "status": "passed"/"compile_failed"/
                       "simulation_failed"/"assertion_failed",
             "returncode": int,
             "started_at": str|None, "ended_at": str|None,
             "diagnostics": [str, ...],
             "assertions": {name: {"status", "fail_count"}},
             "coverage": {name: hits},
             "compile_command": [argv], "simulate_command": [argv],
             "compiled_files": [path, ...]}

      结果名称不属于本次运行、或 ``run_id`` 不符，均抛
      :class:`RuntimeError`。
    """
    _require_run_id(run_id)

    expected = [spec.effective_name for spec in testbenches]
    expected_set = set(expected)

    # ---- 归属 + 命名冲突 / 重复记录 ----
    if isinstance(results, dict):
        records = []
        for key, value in results.items():
            if not isinstance(value, dict):
                raise RuntimeError(f"测试 {key!r} 的结果必须是 dict")
            embedded = value.get("name")
            if embedded is not None and embedded != key:
                raise RuntimeError(
                    f"测试结果命名冲突：归属键 {key!r} 与结果名称 "
                    f"{embedded!r} 不一致"
                )
            record = dict(value)
            record.setdefault("name", key)
            records.append(record)
    elif isinstance(results, (list, tuple)):
        records = list(results)
    else:
        raise RuntimeError("results 必须是列表或字典")

    by_name = {}
    for result in records:
        if not isinstance(result, dict):
            raise RuntimeError("测试结果必须是 dict")
        name = result.get("name")
        if not isinstance(name, str) or not name.strip():
            raise RuntimeError("存在缺少名称的测试结果")
        if name not in expected_set:
            raise RuntimeError(
                f"测试结果 {name!r} 不属于本次运行（run_id={run_id!r}）"
            )
        if result.get("run_id") != run_id:
            raise RuntimeError(
                f"测试 {name!r} 的结果不属于本次运行"
                f"（run_id={result.get('run_id')!r}，期望 {run_id!r}）"
            )
        if name in by_name:
            raise RuntimeError(f"测试 {name!r} 存在重复结果记录")
        by_name[name] = result

    missing = [n for n in expected if n not in by_name]
    if missing:
        raise RuntimeError(
            "缺少属于本次运行的测试结果：" + ", ".join(missing)
        )

    text_paths = tuple(list(sources)
                       + [s.testbench for s in testbenches]
                       + list(known_paths))

    tb_entries = []
    executed = []          # (spec, result, tb_status, reason)
    agg_assertions = {}
    agg_coverage = {}

    for spec in testbenches:
        name = spec.effective_name
        result = by_name[name]

        if result["status"] == TB_SKIPPED:
            # 执行层把跳过也登记为带 run_id 的结果；仅接受与配置一致者。
            if not spec.skip:
                raise RuntimeError(
                    f"测试 {name!r} 未配置为跳过却上报了 skipped 结果"
                )
            tb_entries.append(_skipped_entry(spec, workdir))
            continue
        if spec.skip:
            raise RuntimeError(
                f"测试 {name!r} 已配置为跳过却上报了执行结果"
            )

        _validate_result_payload(name, result)
        _merge_assertions(name, result["assertions"], agg_assertions)
        _merge_coverage(name, result["coverage"], agg_coverage)

        if result["status"] == TB_PASSED:
            if result["assertions"]:
                tb_status, reason = TB_PASSED, None
            else:
                # 正常结束却没有任何断言统计：统计缺失记为 failed。
                tb_status, reason = TB_FAILED, REASON_MISSING_STATS
        elif result["status"] == REASON_ASSERTION_FAILED:
            tb_status, reason = TB_FAILED, REASON_ASSERTION_FAILED
        else:
            tb_status = TB_FAILED
            reason = result["status"]  # compile_failed / simulation_failed

        tb_entries.append({
            "name": name,
            "testbench": sanitize_path(spec.testbench, workdir),
            "status": tb_status,
            "end_reason": reason,
            "required": bool(spec.required),
            "top": (spec.top or default_top).strip(),
            "seed": spec.seed,
            "skip_reason": sanitize_text(spec.skip_reason, workdir, text_paths),
            "started_at": result.get("started_at"),
            "ended_at": result.get("ended_at"),
            "assertions": _assertion_entries(result["assertions"]),
            "coverage": [
                {"name": cov_name, "hits": result["coverage"][cov_name]}
                for cov_name in result["coverage"]
            ],
            "diagnostics": [
                sanitize_text(d, workdir, text_paths)
                for d in result["diagnostics"]
            ],
        })
        executed.append((spec, result, tb_status))

    # ---- 空结果：无可执行测试台 / 无任何覆盖率结果（保留已有报告）----
    # 已有测试台失败时必须产出完整报告（含失败结论），故“无覆盖率结果”
    # 的 RuntimeError 仅在所有已执行测试台均未失败、却收集不到任何覆盖
    # 率点时触发。
    if not executed:
        raise RuntimeError("无可执行测试台（全部被跳过）")
    executed_failed = any(
        tb_status == TB_FAILED for (_spec, _result, tb_status) in executed
    )
    # 既未观测到任何覆盖点、也未声明覆盖点全集，且测试台本身都没失败：
    # 属于覆盖率收集缺失，报错并保留已有报告。声明了全集时即便 0 命中
    # 也能构成正常的低覆盖率结论，不在此列。
    if not agg_coverage and not coverage_config.points \
            and not executed_failed:
        raise RuntimeError("没有任何覆盖率结果")

    # ---- 计数与整体失败条件 ----
    counts = {TB_PASSED: 0, TB_FAILED: 0, TB_SKIPPED: 0}
    skipped_required = []
    for spec, entry in zip(testbenches, tb_entries):
        counts[entry["status"]] += 1
        if entry["status"] == TB_SKIPPED and spec.required:
            skipped_required.append(entry["name"])

    assertion_total = len(agg_assertions)
    assertion_failed_names = [
        n for n, a in agg_assertions.items() if a["status"] == STATUS_FAILED
    ]

    # 覆盖点全集：配置声明的点优先（按声明顺序），其后追加实际观测到但
    # 未声明的点（按首次出现顺序）；未观测到的声明点按 0 命中计入。
    declared = list(dict.fromkeys(coverage_config.points))
    ordered_points = declared + [
        n for n in agg_coverage if n not in set(declared)
    ]
    point_hits = {n: agg_coverage.get(n, 0) for n in ordered_points}
    hit_points = {n for n, h in point_hits.items() if h > 0}
    unobserved = [n for n in ordered_points if n not in agg_coverage]
    ratio = len(hit_points) / len(point_hits) if point_hits else 0.0
    required_points = list(dict.fromkeys(coverage_config.required_points))
    missing_required_cov = [
        p for p in required_points if point_hits.get(p, 0) <= 0
    ]
    threshold = coverage_config.threshold
    threshold_value = float(threshold) if threshold is not None else None
    threshold_failed = (
        threshold_value is not None and ratio < threshold_value
    )

    any_failed = counts[TB_FAILED] > 0
    overall_failed = (
        any_failed
        or bool(assertion_failed_names)
        or threshold_failed
        or bool(missing_required_cov)
        or bool(skipped_required)
    )

    return {
        "schema_version": VERIFY_SCHEMA_VERSION,
        "run_id": run_id,
        "tool": TOOL_NAME,
        "config_summary": _config_summary_entry(
            testbenches, default_top, duration, by_name, workdir
        ),
        "compiled_files": _compiled_files_entry(
            sources, testbenches, by_name, workdir
        ),
        "testbenches": tb_entries,
        "totals": {
            "total": len(testbenches),
            "passed": counts[TB_PASSED],
            "failed": counts[TB_FAILED],
            "skipped": counts[TB_SKIPPED],
        },
        "assertions": {
            "total": assertion_total,
            "passed": assertion_total - len(assertion_failed_names),
            "failed": len(assertion_failed_names),
            "results": _assertion_entries(agg_assertions),
        },
        "coverage_summary": {
            "points_total": len(point_hits),
            "points_hit": len(hit_points),
            "hit_ratio": round(ratio, 6),
            "threshold": threshold_value,
            "threshold_met": not threshold_failed,
            "required_points": required_points,
            "missing_required_points": missing_required_cov,
            "missing_points": unobserved,
            "points": [
                {"name": name, "hits": point_hits[name]}
                for name in ordered_points
            ],
        },
        "conclusion": TB_FAILED if overall_failed else TB_PASSED,
    }


def _validate_result_payload(name, result):
    """校验单条执行结果的字段类型与枚举。"""
    status = result.get("status")
    if status not in _INTERNAL_STATUSES:
        raise RuntimeError(f"测试 {name!r} 的结果状态非法：{status!r}")
    rc = result.get("returncode")
    if isinstance(rc, bool) or not isinstance(rc, int):
        raise RuntimeError(f"测试 {name!r} 缺少合法的 returncode")
    for key in ("started_at", "ended_at"):
        value = result.get(key)
        if value is not None and not isinstance(value, str):
            raise RuntimeError(f"测试 {name!r} 的 {key} 必须为字符串或 null")
    if not isinstance(result.get("diagnostics"), list) or not all(
        isinstance(d, str) for d in result["diagnostics"]
    ):
        raise RuntimeError(f"测试 {name!r} 缺少合法的 diagnostics")
    _validate_assertions(name, result.get("assertions"))
    _validate_coverage(name, result.get("coverage"))
    for key in ("compile_command", "simulate_command", "compiled_files"):
        value = result.get(key)
        if not isinstance(value, list) or not all(
            isinstance(t, str) for t in value
        ):
            raise RuntimeError(f"测试 {name!r} 缺少合法的 {key}")


def _validate_assertions(tb_name, assertions):
    if not isinstance(assertions, dict):
        raise RuntimeError(f"测试 {tb_name!r} 的断言结果必须是 dict")
    for aname, entry in assertions.items():
        if not isinstance(aname, str) or not aname.strip():
            raise RuntimeError(f"测试 {tb_name!r} 存在非法断言名称")
        if not isinstance(entry, dict):
            raise RuntimeError(
                f"测试 {tb_name!r} 的断言 {aname!r} 结果必须是 dict"
            )
        if entry.get("status") not in (STATUS_PASSED, STATUS_FAILED):
            raise RuntimeError(
                f"测试 {tb_name!r} 的断言 {aname!r} 状态非法"
            )
        fc = entry.get("fail_count")
        if isinstance(fc, bool) or not isinstance(fc, int) or fc < 0:
            raise RuntimeError(
                f"测试 {tb_name!r} 的断言 {aname!r} fail_count 非法"
            )


def _validate_coverage(tb_name, coverage):
    if not isinstance(coverage, dict):
        raise RuntimeError(f"测试 {tb_name!r} 的覆盖率结果必须是 dict")
    for cname, hits in coverage.items():
        if not isinstance(cname, str) or not cname.strip():
            raise RuntimeError(f"测试 {tb_name!r} 存在非法覆盖点名称")
        if isinstance(hits, bool) or not isinstance(hits, int) or hits < 0:
            raise RuntimeError(
                f"测试 {tb_name!r} 的覆盖点 {cname!r} 命中数非法"
            )


def _merge_assertions(tb_name, assertions, agg):
    """跨测试台合并同名断言（聚合规则与 schema v2 一致）。

    任一测试台中最终 failed 即 failed，``fail_count`` 为各失败测试台的
    失败次数之和；全部 passed 才 passed。同一测试台内部的重复 ASSERT
    记录已由 :class:`ResultCollector` 归并为同一项，不会在此重复计数
    语义之外产生第二条结果。
    """
    for aname, entry in assertions.items():
        prior = agg.get(aname)
        if prior is None:
            agg[aname] = {
                "status": entry["status"],
                "fail_count": entry["fail_count"],
            }
            continue
        if entry["status"] == STATUS_FAILED:
            prior["status"] = STATUS_FAILED
            prior["fail_count"] += entry["fail_count"]


def _merge_coverage(tb_name, coverage, agg):
    """跨测试台累加同名覆盖点命中数。"""
    for cname, hits in coverage.items():
        agg[cname] = agg.get(cname, 0) + hits


def _skipped_entry(spec, workdir):
    return {
        "name": spec.effective_name,
        "testbench": sanitize_path(spec.testbench, workdir),
        "status": TB_SKIPPED,
        "end_reason": None,
        "required": bool(spec.required),
        "top": None,
        "seed": spec.seed,
        "skip_reason": spec.skip_reason,
        "started_at": None,
        "ended_at": None,
        "assertions": [],
        "coverage": [],
        "diagnostics": [],
    }


def _compiled_files_entry(sources, testbenches, by_name, workdir):
    """实际编译文件：设计源文件有序保留，并集脱敏后稳定排序。

    ``per_testbench`` 按测试台配置顺序输出，与结果收集先后无关。
    """
    per_tb = {}
    union = set()
    for spec in testbenches:
        result = by_name[spec.effective_name]
        if result["status"] == TB_SKIPPED:
            continue
        files = [sanitize_path(p, workdir) for p in result["compiled_files"]]
        per_tb[spec.effective_name] = files
        union.update(files)
    return {
        "design_sources": [sanitize_path(p, workdir) for p in sources],
        "files": sorted(union),
        "per_testbench": per_tb,
    }


def _config_summary_entry(testbenches, default_top, duration, by_name,
                          workdir):
    """编译与仿真配置摘要：时长、默认/每台顶层、种子、脱敏命令。"""
    tops = {}
    seeds = {}
    compile_commands = {}
    simulate_commands = {}
    for spec in testbenches:
        name = spec.effective_name
        tops[name] = (spec.top or default_top).strip()
        seeds[name] = spec.seed
        result = by_name.get(name)
        if result is not None and result["status"] != TB_SKIPPED:
            compile_commands[name] = sanitize_argv(
                result["compile_command"], workdir
            )
            if result["simulate_command"]:
                simulate_commands[name] = sanitize_argv(
                    result["simulate_command"], workdir
                )
    return {
        "duration": duration,
        "default_top": default_top.strip(),
        "tops": tops,
        "seeds": seeds,
        "compile_commands": compile_commands,
        "simulate_commands": simulate_commands,
    }


# ---------------------------------------------------------------------------
# 执行编排
# ---------------------------------------------------------------------------

def _safe_file_stem(name):
    """把测试名映射为工作目录产物使用的安全文件名字段。"""
    stem = "".join(
        ch if (ch.isalnum() or ch in ("_", "-")) else "_" for ch in name
    )
    return stem or "test"


def _compile_one(spec, sources, default_top, amount, unit, workdir):
    """编译单个测试台，返回编译相关产物与结果。

    测试台按配置顺序处理（非并发），故看门狗沿用既有固定命名
    ``rtl_lab_watchdog.v``（属于既有配套产物）；各测试台的 vvp 产物以
    测试名区分。
    """
    top = (spec.top or default_top).strip()
    stem = _safe_file_stem(spec.effective_name)
    vvp_output = os.path.join(workdir, f"rtl_lab_verify_{stem}.vvp")
    watchdog_path = os.path.join(workdir, "rtl_lab_watchdog.v")
    make_watchdog_source(watchdog_path, amount, unit)
    compiled_files = list(sources) + [spec.testbench, watchdog_path]
    rc, cout, cerr, command = compile_sources(
        source_files=compiled_files,
        top=top,
        output_path=vvp_output,
        extra_roots=[WATCHDOG_MODULE],
    )
    return {
        "vvp": vvp_output,
        "watchdog": watchdog_path,
        "returncode": rc,
        "stdout": cout,
        "stderr": cerr,
        "command": command,
        "compiled_files": compiled_files,
    }


def verify(config=None, **kwargs):
    """执行统一验证流程并返回 schema v3 报告。

    可传 :class:`VerifyConfig`，也可用关键字参数直接构造。

    步骤：

    1. 前置校验（顺序固定）：``run_id`` 非空（:class:`ValueError`）、
       配置语义与文件、输出位置可写（:class:`OSError`，不触碰任何
       文件）；
    2. 按配置顺序逐个处理未跳过的测试台：独立编译、独立仿真、解析
       ASSERT/COVER，并登记带 ``run_id`` 的结果；单个测试台失败不影响
       其他测试台继续执行；
    3. 经 :func:`build_verify_report` 汇总：归属/冲突/重复/缺失/空结果
       校验集中于此，非法抛 :class:`RuntimeError`；“无可执行测试台”
       与“无任何覆盖率结果”时不会进入写盘步骤，**已有报告得以保留**；
    4. 通过同目录临时文件 + ``os.replace`` 原子写盘；无论整体结论
       passed/failed 都生成完整报告。

    :returns: :class:`~rtl_lab.report.Report`（dict 子类，携带原始流）。
    :raises ValueError: run_id 为空或配置非法（不落盘）。
    :raises OSError: report_path 不可写（不落盘、不动已有报告）。
    :raises RuntimeError: 结果归属/命名冲突/重复记录/空结果。
    """
    if config is None:
        config = VerifyConfig(**kwargs)
    elif kwargs:
        raise TypeError("verify() 不能同时传入 VerifyConfig 与关键字参数")

    # 1) 前置校验。
    run_id = _require_run_id(config.run_id)
    amount, unit = _validate_config(config)
    _check_output_writable(config.report_path)

    duration_str = f"{amount}{unit}"
    workdir = config.workdir
    os.makedirs(workdir, exist_ok=True)

    # 2) 顺序编译 + 仿真（结果按收集顺序登记；输出顺序与完成先后解耦）。
    results = []
    raw_blocks = []
    known_paths = []

    for spec in config.testbenches:
        name = spec.effective_name
        if spec.skip:
            results.append({"name": name, "run_id": run_id,
                            "status": TB_SKIPPED})
            continue

        started_at = _utc_now_iso()
        compiled = _compile_one(
            spec, config.sources, config.default_top,
            amount, unit, workdir,
        )
        known_paths.extend([compiled["vvp"], compiled["watchdog"]])

        if compiled["returncode"] != 0:
            ended_at = _utc_now_iso()
            diagnostics = []
            if compiled["stdout"]:
                diagnostics.append(compiled["stdout"])
            if compiled["stderr"]:
                diagnostics.append(compiled["stderr"])
            results.append({
                "run_id": run_id,
                "name": name,
                "status": REASON_COMPILE_FAILED,
                "returncode": compiled["returncode"],
                "started_at": started_at,
                "ended_at": ended_at,
                "diagnostics": diagnostics,
                "assertions": {},
                "coverage": {},
                "compile_command": compiled["command"],
                "simulate_command": [],
                "compiled_files": compiled["compiled_files"],
            })
            raw_blocks.append((name, compiled["stdout"], compiled["stderr"]))
            continue

        rc, sout, serr, simulate_command = run_simulation(
            vvp_path=os.path.basename(compiled["vvp"]),
            seed=spec.seed, cwd=workdir,
        )
        ended_at = _utc_now_iso()

        collector = ResultCollector()
        collector.feed_text(sout)

        diagnostics = []
        if sout:
            diagnostics.append(sout)
        if serr:
            diagnostics.append(serr)

        if rc != 0:
            status = REASON_SIMULATION_FAILED
        elif collector.has_failure:
            status = REASON_ASSERTION_FAILED
        else:
            status = TB_PASSED

        results.append({
            "run_id": run_id,
            "name": name,
            "status": status,
            "returncode": rc,
            "started_at": started_at,
            "ended_at": ended_at,
            "diagnostics": diagnostics,
            "assertions": dict(collector.assertions),
            "coverage": dict(collector.coverage),
            "compile_command": compiled["command"],
            "simulate_command": simulate_command,
            "compiled_files": compiled["compiled_files"],
        })
        raw_blocks.append((name, sout, serr))

    # 3) 构建报告（空结果/归属错误在此抛出，不会覆盖已有报告）。
    data = build_verify_report(
        run_id=run_id,
        sources=config.sources,
        testbenches=config.testbenches,
        default_top=config.default_top,
        duration=duration_str,
        coverage_config=config.coverage,
        results=results,
        workdir=workdir,
        known_paths=tuple(known_paths),
    )
    report = Report(
        data,
        raw_stdout=_join_stream(raw_blocks, 1),
        raw_stderr=_join_stream(raw_blocks, 2),
    )

    # 4) 原子落盘。
    if config.report_path:
        _atomic_write_report(report, config.report_path)
    return report


def _join_stream(blocks, index):
    """按测试台稳定顺序拼接某一流（index 1=stdout，2=stderr）。"""
    joined = ""
    for block in blocks:
        text = block[index]
        if text:
            joined += text
            if not text.endswith("\n"):
                joined += "\n"
    return joined


def _atomic_write_report(report, report_path):
    """同目录临时文件 + os.replace 原子写报告，失败时保留已有报告。"""
    parent = os.path.dirname(os.path.abspath(report_path))
    fd, tmp_path = tempfile.mkstemp(
        prefix=".rtl_lab_report_", suffix=".tmp", dir=parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, report_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
