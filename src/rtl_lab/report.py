"""JSON 报告的构建与路径脱敏。

单次报告（``run``，schema v1）顶层固定包含：
``schema_version``、``tool``、``command``、``sources``、``testbench``、
``top``、``duration``、``seed``、``status``、``diagnostics``、
``assertions``、``coverage``。

回归报告（``regress``，schema v2）将 ``seed`` 扩展为 ``seeds``，并新增
``runs`` 与 ``failed_seeds``；顶层断言/覆盖率为跨种子汇总，结构同 v1。

统一验证报告（``verify``，schema v3）在 v1/v2 之上新增 ``run_id``、
``format``、``result``、``config``、``compilation``、``testbenches``、
``assertion_summary``、``coverage_summary``、``skipped_required``、
``generated_at``；旧字段语义与脱敏规则保持不变，旧读取逻辑凭
``schema_version`` 与 ``format`` 即可识别为新格式，不会误判为 v1/v2。

带基线对比的统一验证报告（``verify --baseline``，schema v4）在 v3
字段及顺序之后追加 ``comparison``（基线路径、无差异标记与结构化差异）；
对比逻辑见 :mod:`rtl_lab.baseline`。
"""

import json
import os
import re

#: 单次报告结构版本。
SCHEMA_VERSION = 1

#: 多随机种子回归报告结构版本。
REGRESS_SCHEMA_VERSION = 2

#: 统一验证报告结构版本。
VERIFICATION_SCHEMA_VERSION = 3

#: 带基线对比的统一验证报告结构版本（v3 字段及顺序之后追加
#: ``comparison``；仅 ``verify --baseline`` 产出）。
VERIFICATION_COMPARISON_SCHEMA_VERSION = 4

#: 统一验证报告的格式标识（旧读取逻辑据此与 v1/v2 区分）。
VERIFICATION_FORMAT = "rtl-lab-verification"

#: 统一验证报告使用的工具标识（与 v1/v2 字面值保持一致）。
TOOL_NAME_VERIFICATION = "icarus-verilog"

#: 测试台终态枚举（顺序即报告中的稳定排列依据之外的取值域）。
TB_STATUSES = ("passed", "failed", "skipped")

#: 覆盖率点状态枚举：hit=至少命中一次；missed=已统计但零命中；
#: unavailable=配置点名但无任何统计。
COVERAGE_POINT_STATUSES = ("hit", "missed", "unavailable")

#: 统一报告总体结论枚举。
RESULTS = ("passed", "failed")


class Report(dict):
    """报告 dict；额外携带不参与 JSON 序列化的原始仿真流。

    控制台展示使用 :attr:`raw_stdout` / :attr:`raw_stderr`，
    JSON 报告仅包含脱敏后的 ``diagnostics``。
    """

    def __init__(self, data, raw_stdout="", raw_stderr=""):
        super().__init__(data)
        self.raw_stdout = raw_stdout
        self.raw_stderr = raw_stderr

#: 兜底匹配 POSIX 风格绝对路径（至少两级，避免误伤 "/" 本身）。
_ABS_PATH_RE = re.compile(r"(?<![\w.-])/[A-Za-z0-9_][\w.\-]*(?:/[\w.\-]+)+")

#: 兜底匹配 Windows 风格绝对路径（如 ``C:\\dir\\file.v``）。
_WIN_ABS_PATH_RE = re.compile(
    r"[A-Za-z]:[\\/][\w.\-]+(?:[\\/][\w.\-]+)+"
)


def sanitize_path(path, workdir):
    """将路径脱敏为不泄露工作目录之外绝对路径的形式。

    - 绝对路径且位于工作目录之内：转为相对工作目录的 POSIX 风格路径；
    - 绝对路径但位于工作目录之外：仅保留文件名（basename）；
    - 输入即为相对路径：规范化后原样保留（相对路径不泄露绝对位置）。
    """
    if not os.path.isabs(path):
        return os.path.normpath(path).replace(os.sep, "/")

    workdir_abs = os.path.abspath(os.path.normpath(workdir))
    abs_path = os.path.abspath(path)
    try:
        rel = os.path.relpath(abs_path, workdir_abs)
    except ValueError:
        # Windows 下不同盘符会抛 ValueError，退化为 basename。
        return os.path.basename(path)

    rel_posix = rel.replace(os.sep, "/")
    if rel_posix == ".." or rel_posix.startswith("../") or os.path.isabs(rel_posix):
        return os.path.basename(path)
    return rel_posix


def sanitize_text(text, workdir, known_paths=()):
    """脱敏诊断文本中出现的绝对路径。

    依次处理：

    1. 已知输入/产物路径（源文件、测试台、编译产物等）整体替换；
    2. 工作目录绝对前缀替换为 ``<workdir>``；
    3. 残留的绝对路径兜底替换为其文件名，确保不泄露工作目录之外的位置。

    文本的其余内容（断言记录、仿真消息等）原样保留。
    """
    if not text:
        return text
    workdir_abs = os.path.abspath(os.path.normpath(workdir))

    # 1) 已知绝对路径：长路径优先，避免前缀部分替换。
    replacements = []
    for raw in known_paths:
        if raw and os.path.isabs(raw):
            abs_raw = os.path.abspath(raw)
            replacements.append((abs_raw, sanitize_path(raw, workdir)))
    # 2) 工作目录前缀。
    replacements.append((workdir_abs.rstrip(os.sep) + os.sep, "<workdir>/"))
    replacements.append((workdir_abs, "<workdir>"))
    replacements.sort(key=lambda pair: len(pair[0]), reverse=True)

    result = text
    for old, new in replacements:
        result = result.replace(old, new)

    # 3) 兜底：清洗任何残留的绝对路径（POSIX 或 Windows 风格）。
    def _scrub(match):
        return re.split(r"[\\/]", match.group(0))[-1]

    result = _ABS_PATH_RE.sub(_scrub, result)
    result = _WIN_ABS_PATH_RE.sub(_scrub, result)
    return result


def sanitize_arg(token, workdir):
    """脱敏命令 argv 中的单个 token：仅处理绝对路径。

    工作目录之内的绝对路径转相对路径；之外的绝对路径仅保留文件名
    （如 ``/usr/bin/iverilog`` -> ``iverilog``）；其余 token 原样保留。
    """
    if not isinstance(token, str) or not os.path.isabs(token):
        return token
    return sanitize_path(token, workdir)


def sanitize_argv(argv, workdir):
    """脱敏整条命令的 argv 列表。"""
    return [sanitize_arg(t, workdir) for t in argv]


def _assertion_entries(assertions):
    return [
        {
            "name": name,
            "status": assertions[name]["status"],
            "fail_count": assertions[name]["fail_count"],
        }
        for name in assertions
    ]


def build_report(*, tool, command, sources, testbench, top, duration, seed,
                 status, diagnostics, assertions, coverage, workdir,
                 known_paths=()):
    """组装单次报告 dict（schema v1）；路径与诊断文本均做脱敏处理。

    数组顺序与输入顺序一致：``sources`` 保持传入顺序；
    ``assertions`` / ``coverage`` 按首次出现顺序排列。

    :param duration: 规范化时长字符串，如 ``"100ns"``。
    :param command: ``{"compile": argv, "simulate": argv}``。
    :param assertions: ``{name: {"status": ..., "fail_count": int}}``
    :param coverage: ``{name: hit_count}``
    :param diagnostics: 字符串列表（编译/仿真诊断等）
    :param known_paths: 需要从诊断文本中脱敏的额外已知路径
        （编译产物、看门狗文件等）。
    """
    safe_command = {
        stage: sanitize_argv(argv, workdir)
        for stage, argv in command.items()
    }
    text_paths = tuple(list(sources) + [testbench] + list(known_paths))
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": tool,
        "command": safe_command,
        "sources": [sanitize_path(p, workdir) for p in sources],
        "testbench": sanitize_path(testbench, workdir),
        "top": top,
        "duration": duration,
        "seed": seed,
        "status": status,
        "diagnostics": [
            sanitize_text(d, workdir, text_paths) for d in diagnostics
        ],
        "assertions": _assertion_entries(assertions),
        "coverage": [
            {"name": name, "hits": coverage[name]}
            for name in coverage
        ],
    }


def build_regress_report(*, tool, compile_command, sources, testbench, top,
                         duration, seeds, status, diagnostics, runs, workdir,
                         known_paths=()):
    """组装多随机种子回归报告 dict（schema v2）。

    复用单次报告的字段与脱敏规则；``seed`` 扩展为 ``seeds``，新增 ``runs``
    与 ``failed_seeds``。顶层 ``command`` 保存编译 argv 与首个种子的仿真
    argv（编译失败、尚无 run 时 simulate 为 ``[]``）；每个 run 条目恰含
    seed、status、diagnostics、assertions、coverage 五个字段。

    跨种子汇总：

    - 断言与覆盖率点均按名称首次出现顺序排列（按种子执行顺序）；
    - 断言状态：任一种子中该断言最终为 failed 则 failed，否则 passed；
      ``fail_count`` 为各种子失败次数之和；
    - 覆盖率 ``hits`` 为各种子命中次数之和，``hit_runs`` 为命中种子数；
    - ``failed_seeds`` 列出最终失败（断言失败或仿真异常退出）的种子。

    :param compile_command: 编译阶段的原始 argv。
    :param runs: 原始（未脱敏）每种子结果列表，每项为
        ``{"seed", "status", "diagnostics", "assertions", "coverage",
        "command"}``；其中 ``command["simulate"]`` 仅用于提取顶层仿真
        argv，不会写入 run 条目；顺序须与 ``seeds`` 一致。
    :param diagnostics: 顶层诊断（编译失败等 run 之外的额外诊断）。
    """
    simulate_argv = runs[0]["command"]["simulate"] if runs else []
    safe_command = {
        "compile": sanitize_argv(compile_command, workdir),
        "simulate": sanitize_argv(simulate_argv, workdir),
    }
    text_paths = tuple(list(sources) + [testbench] + list(known_paths))

    safe_runs = []
    agg_assertions = {}
    agg_coverage = {}
    failed_seeds = []

    for run in runs:
        safe_runs.append({
            "seed": run["seed"],
            "status": run["status"],
            "diagnostics": [
                sanitize_text(d, workdir, text_paths)
                for d in run["diagnostics"]
            ],
            "assertions": _assertion_entries(run["assertions"]),
            "coverage": [
                {"name": name, "hits": run["coverage"][name]}
                for name in run["coverage"]
            ],
        })

        for name, entry in run["assertions"].items():
            agg = agg_assertions.setdefault(
                name, {"status": "passed", "fail_count": 0}
            )
            agg["fail_count"] += entry["fail_count"]
            if entry["status"] == "failed":
                agg["status"] = "failed"

        for name, hits in run["coverage"].items():
            cov = agg_coverage.setdefault(name, {"hits": 0, "hit_runs": 0})
            cov["hits"] += hits
            if hits:
                cov["hit_runs"] += 1

        if run["status"] != "passed":
            failed_seeds.append(run["seed"])

    return {
        "schema_version": REGRESS_SCHEMA_VERSION,
        "tool": tool,
        "command": safe_command,
        "sources": [sanitize_path(p, workdir) for p in sources],
        "testbench": sanitize_path(testbench, workdir),
        "top": top,
        "duration": duration,
        "seeds": list(seeds),
        "status": status,
        "diagnostics": [
            sanitize_text(d, workdir, text_paths) for d in diagnostics
        ],
        "runs": safe_runs,
        "assertions": _assertion_entries(agg_assertions),
        "coverage": [
            {"name": name,
             "hits": agg_coverage[name]["hits"],
             "hit_runs": agg_coverage[name]["hit_runs"]}
            for name in agg_coverage
        ],
        "failed_seeds": failed_seeds,
    }


def write_report(report, report_path):
    """将报告写入 JSON 文件（UTF-8、两空格缩进、末尾换行）。"""
    parent = os.path.dirname(os.path.abspath(report_path))
    os.makedirs(parent, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write("\n")


def ensure_report_writable(report_path):
    """生成前校验报告输出位置可写。

    - 已存在的普通文件：要求可写；
    - 已存在但不是普通文件（目录等）：不可写；
    - 不存在：沿父目录向上找到最近的已存在目录，要求其可写
      （尚不存在的目录留给写入阶段创建）。

    不可写时抛 :class:`OSError`；本函数不创建任何目录或文件。
    """
    if os.path.exists(report_path):
        if not os.path.isfile(report_path) or not os.access(report_path, os.W_OK):
            raise OSError(f"报告输出位置不可写：{report_path}")
        return

    parent = os.path.dirname(os.path.abspath(report_path))
    probe = parent
    while probe and not os.path.isdir(probe):
        probe = os.path.dirname(probe)
    if not probe or not os.access(probe, os.W_OK):
        raise OSError(f"报告输出目录不可写：{parent}")


def _merge_coverage(coverage_by_tb):
    """跨测试台合并覆盖率命中，返回 name -> {hits, hit_testbenches}。

    顺序为各测试台覆盖率点首次出现顺序（调用方已按稳定顺序排列测试台）。
    ``hits`` 为各台命中次数之和，``hit_testbenches`` 为命中过的测试台数。
    """
    merged = {}
    for _tb_name, coverage in coverage_by_tb:
        for name, hits in coverage.items():
            agg = merged.setdefault(name, {"hits": 0, "hit_testbenches": 0})
            agg["hits"] += hits
            if hits:
                agg["hit_testbenches"] += 1
    return merged


def build_verification_report(*, run_id, sources, testbench_files,
                              coverage_config, duration,
                              compile_command, simulate_command,
                              outcomes, generated_at, workdir, known_paths=()):
    """组装统一验证报告 dict（schema v3）。

    纯函数：不做落盘、不做归属校验之外的副作用。数组顺序稳定——
    ``sources`` 按编译范围输入顺序；``testbenches`` 按 ``outcomes`` 给定
    顺序（调用方保证为测试选择顺序）；断言与覆盖率项按名称首次出现顺序；
    路径与诊断沿用 v1/v2 脱敏规则。

    覆盖率语义：覆盖率 = 至少被命中一次的覆盖率点 / 覆盖率点总数。
    覆盖率点集合取配置显式点名与实际统计点的并集（配置不点名时即实际
    并集）；配置要求但无任何统计的点记为 ``unavailable``，计入分母并拉低
    覆盖率。是否达标由聚合比率与阈值比较。

    :param testbench_files: 选择的测试台源文件路径列表（按选择顺序）。
    :param coverage_config: ``{"threshold": float, "points": [名称...]}``。
    :param outcomes: 按选择顺序的结果 dict 列表，每项含
        name/top/status/required/start_time/end_time/assertions/coverage/
        diagnostics/reason/commands；status ∈ passed/failed/skipped，reason
        为稳定枚举（passed/compilation_failed/simulation_failed/
        assertion_failed/incomplete_statistics/skipped），commands 为
        ``{"compile": argv, "simulate": argv}``。
    :param generated_at: 调用方提供的报告生成时间字符串（允许跨复现变化）。
    :returns: ``(data, result)``；result 为总体结论 passed/failed。
    """
    threshold = float(coverage_config["threshold"])
    configured_points = list(dict.fromkeys(coverage_config.get("points") or []))
    text_paths = tuple(
        list(sources) + list(testbench_files) + list(known_paths)
    )

    if not outcomes:
        raise RuntimeError("结果归属错误：没有任何可执行测试台结果")
    if len({o["name"] for o in outcomes}) != len(outcomes):
        raise RuntimeError("测试结果归属错误：存在重复的测试台记录")

    safe_outcomes = []
    tb_counts = {"passed": 0, "failed": 0, "skipped": 0}
    skipped_required = []
    agg_assertions = {}
    coverage_by_tb = []

    for o in outcomes:
        if o["status"] not in TB_STATUSES:
            raise RuntimeError(f"测试台状态非法：{o['status']!r}")
        safe_outcomes.append({
            "name": o["name"],
            "top": o["top"],
            "testbench": sanitize_path(o["testbench_file"], workdir),
            "status": o["status"],
            "reason": o["reason"],
            "required": bool(o["required"]),
            "start_time": o["start_time"],
            "end_time": o["end_time"],
            "command": {
                "compile": sanitize_argv(o["commands"]["compile"], workdir),
                "simulate": sanitize_argv(o["commands"]["simulate"], workdir),
            },
            "diagnostics": [
                sanitize_text(d, workdir, text_paths) for d in o["diagnostics"]
            ],
            "assertions": _assertion_entries(o["assertions"]),
            "coverage": [
                {"name": name, "hits": o["coverage"][name]}
                for name in o["coverage"]
            ],
        })
        tb_counts[o["status"]] += 1
        if o["status"] == "skipped" and o["required"]:
            skipped_required.append(o["name"])

        for name, entry in o["assertions"].items():
            agg = agg_assertions.setdefault(
                name, {"status": "passed", "fail_count": 0}
            )
            agg["fail_count"] += entry["fail_count"]
            if entry["status"] == "failed":
                agg["status"] = "failed"

        coverage_by_tb.append((o["name"], o["coverage"]))

    merged = _merge_coverage(coverage_by_tb)

    # 覆盖率点集合：实际统计点按首次出现顺序排列，配置点名但从未出现的
    # 点追加在末尾并记为 unavailable。是否允许零覆盖率点由调用方在生成前
    # 按运行级规则裁决（例如编译硬失败仍需出报告）。
    point_names = list(merged) + [
        name for name in configured_points if name not in merged
    ]
    coverage_entries = []
    for name in point_names:
        agg = merged.get(name)
        if agg is None:
            coverage_entries.append({
                "name": name, "status": "unavailable",
                "hits": None, "hit_testbenches": None,
            })
        else:
            coverage_entries.append({
                "name": name,
                "status": "hit" if agg["hits"] > 0 else "missed",
                "hits": agg["hits"],
                "hit_testbenches": agg["hit_testbenches"],
            })

    total_points = len(coverage_entries)
    hit_points = sum(1 for c in coverage_entries if c["status"] == "hit")
    unavailable_points = [
        c["name"] for c in coverage_entries if c["status"] == "unavailable"
    ]
    ratio = round(hit_points / total_points, 6) if total_points else None
    coverage_met = (
        total_points > 0 and ratio is not None and ratio + 1e-12 >= threshold
    )

    assertion_entries = _assertion_entries(agg_assertions)
    assertion_summary = {
        "total": len(assertion_entries),
        "passed": sum(1 for a in assertion_entries if a["status"] == "passed"),
        "failed": sum(1 for a in assertion_entries if a["status"] == "failed"),
        "fail_count": sum(a["fail_count"] for a in assertion_entries),
    }
    coverage_summary = {
        "threshold": threshold,
        "total_points": total_points,
        "hit_points": hit_points,
        "ratio": ratio,
        "met": coverage_met,
        "unavailable_points": unavailable_points,
    }

    # 总体结论：任一测试台失败、任一断言失败、覆盖率未达标（含配置点名
    # 但无统计），或跳过了必测项，均 failed；否则 passed。
    result = "passed"
    if (tb_counts["failed"] or assertion_summary["failed"]
            or not coverage_met or skipped_required):
        result = "failed"

    data = {
        "schema_version": VERIFICATION_SCHEMA_VERSION,
        "format": VERIFICATION_FORMAT,
        "run_id": run_id,
        "result": result,
        "generated_at": generated_at,
        "tool": TOOL_NAME_VERIFICATION,
        "config": {
            "duration": duration,
            "coverage": {
                "threshold": threshold,
                "points": list(dict.fromkeys(configured_points)),
            },
            "compile": sanitize_argv(compile_command, workdir),
            "simulate": sanitize_argv(simulate_command, workdir),
        },
        "compilation": {
            "sources": [sanitize_path(p, workdir) for p in sources],
        },
        "testbenches": safe_outcomes,
        "testbench_summary": dict(tb_counts),
        "assertion_summary": assertion_summary,
        "assertions": assertion_entries,
        "coverage_summary": coverage_summary,
        "coverage": coverage_entries,
        "skipped_required": skipped_required,
    }
    return data, result
