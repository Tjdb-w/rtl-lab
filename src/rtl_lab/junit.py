"""verify 报告的 JUnit XML 输出（``verify --junit PATH``）。

在既有 JSON 报告（schema v3/v4/v5）之外，``VerifyConfig.junit_path``
非空时额外产出一份 UTF-8 JUnit XML，供 CI 系统消费：

- 根元素 ``testsuites`` 以 ``run_id`` 为 ``name``，``tests`` /
  ``failures`` / ``skipped`` 从全部 testcase 汇总，``errors`` 恒为 0；
- 每个测试台一个 ``testsuite``（按报告中的输入顺序）；单种子、编译失败
  或跳过（无 runs 明细）的测试台生成与测试台同名的单个 testcase，
  多种子矩阵按执行顺序为每个种子生成 ``测试台名[seed=种子值]`` 的
  testcase；
- 通过的 testcase 无子元素；可选跳过用 ``<skipped/>``，必测跳过用
  ``<skipped message="RequiredTestSkipped"/>``；失败按 reason 映射为
  ``<failure type="...">``：simulation_failed→SimulationFailure、
  assertion_failed→AssertionFailure、
  incomplete_statistics→IncompleteStatistics、
  compilation_failed→CompilationFailure，内容为脱敏后的 reason 与
  diagnostics；
- 覆盖率不达标时在 ``coverage`` suite 生成 ``threshold`` testcase
  （``CoverageFailure``，记命中数、总数、比率与阈值）；基线对比有差异时
  在 ``baseline`` suite 生成 ``comparison`` testcase
  （``BaselineMismatch``，记差异条数）；
- 输出完全由报告内容决定：不含时间戳与耗时，相同报告生成相同 XML。
"""

import os
from xml.sax.saxutils import escape, quoteattr

#: 测试台/种子运行 reason 到 JUnit failure type 的稳定映射。
REASON_FAILURE_TYPES = {
    "simulation_failed": "SimulationFailure",
    "assertion_failed": "AssertionFailure",
    "incomplete_statistics": "IncompleteStatistics",
    "compilation_failed": "CompilationFailure",
}

#: 必测跳过项的 skipped message。
REQUIRED_SKIP_MESSAGE = "RequiredTestSkipped"

#: 覆盖率不达标 testcase 的 failure type。
COVERAGE_FAILURE_TYPE = "CoverageFailure"

#: 基线对比差异 testcase 的 failure type。
BASELINE_MISMATCH_TYPE = "BaselineMismatch"


def _failure_xml(reason, diagnostics, indent):
    """渲染一个 ``<failure>`` 元素；内容为脱敏 reason 与 diagnostics。"""
    failure_type = REASON_FAILURE_TYPES[reason]
    text = "\n".join([reason] + list(diagnostics))
    return (
        f"{indent}<failure type={quoteattr(failure_type)}>"
        f"{escape(text)}</failure>"
    )


def _testcase_xml(name, inner, indent):
    """渲染一个 ``<testcase>``；``inner`` 为空时无子元素（自闭合）。"""
    if not inner:
        return f"{indent}<testcase name={quoteattr(name)}/>"
    return (
        f"{indent}<testcase name={quoteattr(name)}>\n"
        f"{inner}\n"
        f"{indent}</testcase>"
    )


def _tb_testcases(tb):
    """一个测试台条目对应的 testcase 列表。

    每项为 ``(name, kind, inner_xml)``：kind ∈ passed/failure/skipped，
    ``inner_xml`` 为已渲染的子元素（passed 时为空字符串）。多种子矩阵按
    runs 执行顺序逐种子生成；单种子、编译失败或跳过生成同名 testcase。
    """
    name = tb["name"]
    if tb["status"] == "skipped":
        if tb["required"]:
            inner = (
                f"      <skipped message={quoteattr(REQUIRED_SKIP_MESSAGE)}/>"
            )
        else:
            inner = "      <skipped/>"
        return [(name, "skipped", inner)]

    runs = tb.get("runs") or []
    if not runs:
        # 单种子（v3/v4 无 runs 字段）或编译失败（runs 为空）：同名 testcase。
        if tb["status"] == "failed":
            inner = _failure_xml(tb["reason"], tb["diagnostics"], " " * 6)
            return [(name, "failure", inner)]
        return [(name, "passed", "")]

    cases = []
    for run in runs:
        case_name = f"{name}[seed={run['seed']}]"
        if run["status"] == "failed":
            inner = _failure_xml(run["reason"], run["diagnostics"], " " * 6)
            cases.append((case_name, "failure", inner))
        else:
            cases.append((case_name, "passed", ""))
    return cases


def _coverage_testcase(coverage_summary):
    """覆盖率未达标时的 ``threshold`` testcase（CoverageFailure）。"""
    ratio = coverage_summary["ratio"]
    ratio_text = "-" if ratio is None else str(ratio)
    text = (
        f"hit_points={coverage_summary['hit_points']}"
        f" total_points={coverage_summary['total_points']}"
        f" ratio={ratio_text}"
        f" threshold={coverage_summary['threshold']:g}"
    )
    inner = (
        f'      <failure type={quoteattr(COVERAGE_FAILURE_TYPE)}>'
        f"{escape(text)}</failure>"
    )
    return ("threshold", "failure", inner)


def _baseline_testcase(comparison):
    """基线对比存在差异时的 ``comparison`` testcase（BaselineMismatch）。"""
    text = f"mismatches={len(comparison['mismatches'])}"
    inner = (
        f'      <failure type={quoteattr(BASELINE_MISMATCH_TYPE)}>'
        f"{escape(text)}</failure>"
    )
    return ("comparison", "failure", inner)


def _suite_xml(name, cases, indent):
    """渲染一个 ``<testsuite>``；统计字段从 testcase 汇总，errors 为 0。"""
    tests = len(cases)
    failures = sum(1 for _n, kind, _i in cases if kind == "failure")
    skipped = sum(1 for _n, kind, _i in cases if kind == "skipped")
    header = (
        f"{indent}<testsuite name={quoteattr(name)}"
        f' tests="{tests}" failures="{failures}"'
        f' skipped="{skipped}" errors="0">'
    )
    lines = [header]
    for case_name, _kind, inner in cases:
        lines.append(_testcase_xml(case_name, inner, indent + "  "))
    lines.append(f"{indent}</testsuite>")
    return "\n".join(lines), tests, failures, skipped


def build_junit_xml(report):
    """由 verify 报告 dict 构建 JUnit XML 文本（UTF-8，末尾换行）。

    纯函数：输出完全由报告内容决定，不含时间戳或耗时，相同报告生成
    相同 XML。测试台 suite 按报告 ``testbenches`` 顺序；覆盖率 suite 与
    基线 suite 分别在不达标 / 有差异时追加在最后。
    """
    suites = []
    total_tests = total_failures = total_skipped = 0

    def _add_suite(name, cases):
        nonlocal total_tests, total_failures, total_skipped
        xml, tests, failures, skipped = _suite_xml(name, cases, "  ")
        suites.append(xml)
        total_tests += tests
        total_failures += failures
        total_skipped += skipped

    for tb in report["testbenches"]:
        _add_suite(tb["name"], _tb_testcases(tb))

    coverage_summary = report["coverage_summary"]
    if not coverage_summary["met"]:
        _add_suite("coverage", [_coverage_testcase(coverage_summary)])

    comparison = report.get("comparison")
    if comparison is not None and not comparison["passed"]:
        _add_suite("baseline", [_baseline_testcase(comparison)])

    header = (
        f"<testsuites name={quoteattr(report['run_id'])}"
        f' tests="{total_tests}" failures="{total_failures}"'
        f' skipped="{total_skipped}" errors="0">'
    )
    return "\n".join(
        ['<?xml version="1.0" encoding="UTF-8"?>', header]
        + suites
        + ["</testsuites>", ""]
    )


def write_junit(report, junit_path):
    """将 JUnit XML 写入文件（UTF-8）；父目录不存在时先行创建。"""
    parent = os.path.dirname(os.path.abspath(junit_path))
    os.makedirs(parent, exist_ok=True)
    with open(junit_path, "w", encoding="utf-8") as f:
        f.write(build_junit_xml(report))
