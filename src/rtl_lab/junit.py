"""verify 报告的 JUnit XML 输出（与 JSON 报告同源、同脱敏）。

在 verify JSON 报告生成之后，:func:`build_junit_xml` 依据最终报告
（含基线对比结果）产出确定性的 JUnit XML：

- 根元素 ``<testsuites>`` 的 ``name`` 为 ``run_id``；``tests``/``failures``/
  ``skipped`` 全部由 testcase 汇总，``errors`` 恒为 0；不含时间戳、耗时、
  主机名等任何跨复现变化的信息，相同报告生成相同 XML；
- 每个测试台按输入顺序对应一个 ``<testsuite>``（名为测试台名）：
  单种子、编译失败或跳过且无 runs 明细的测试台生成同名 testcase；
  多种子按执行顺序为每个实际执行的种子生成
  ``<测试台名>[seed=<种子值>]`` 的 testcase；
- 通过的 testcase 无子元素；可选跳过项为 ``<skipped/>``；必测项被跳过
  记为 ``RequiredTestSkipped`` 失败（必测跳过会使总体结论 failed，
  故计入 failures 而非 skipped）；
- 失败原因到 JUnit failure type 的映射：
  ``simulation_failed`` → ``SimulationFailure``、
  ``assertion_failed`` → ``AssertionFailure``、
  ``incomplete_statistics`` → ``IncompleteStatistics``、
  ``compilation_failed`` → ``CompilationFailure``；
  failure 内容为脱敏后的 reason 与 diagnostics；
- 覆盖率不达标时追加名为 ``coverage`` 的 suite，内含名为 ``threshold``
  的 testcase，以 ``CoverageFailure`` 记录命中数、总数、比率与阈值；
- 基线对比存在差异时追加名为 ``baseline`` 的 suite，内含名为
  ``comparison`` 的 testcase，以 ``BaselineMismatch`` 记录差异条数。
"""

from xml.sax.saxutils import escape

#: 测试台失败原因 → JUnit failure type。
_FAILURE_TYPES = {
    "simulation_failed": "SimulationFailure",
    "assertion_failed": "AssertionFailure",
    "incomplete_statistics": "IncompleteStatistics",
    "compilation_failed": "CompilationFailure",
}

#: 必测项被跳过时的 failure type。
REQUIRED_SKIPPED_TYPE = "RequiredTestSkipped"

#: 覆盖率不达标时的 failure type。
COVERAGE_FAILURE_TYPE = "CoverageFailure"

#: 基线对比存在差异时的 failure type。
BASELINE_MISMATCH_TYPE = "BaselineMismatch"


def _clean_text(text):
    """剥离 XML 1.0 禁止的控制字符（保留制表符与换行）。"""
    if not text:
        return ""
    return "".join(
        ch for ch in text
        if ch in ("\t", "\n", "\r") or ord(ch) >= 0x20
    )


def _attr(value):
    """转义属性值并包裹双引号（空白控制字符以实体形式保留）。"""
    cleaned = _clean_text(str(value))
    cleaned = (
        cleaned.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("\t", "&#9;")
        .replace("\n", "&#10;")
        .replace("\r", "&#13;")
    )
    return '"' + cleaned + '"'


def _text(value):
    """转义元素文本。"""
    return escape(_clean_text(str(value)))


def _failure_body(reason, diagnostics):
    """failure 元素内容：脱敏 reason 在前，diagnostics 按序追加。"""
    parts = [reason]
    for chunk in diagnostics or ():
        if chunk:
            parts.append(chunk)
    return _clean_text("\n".join(parts))


def _failure_line(indent, failure_type, message, body):
    """生成单个 failure 元素行（文本内联，换行保持原样）。"""
    begin = (
        f"{' ' * indent}<failure type={_attr(failure_type)} "
        f"message={_attr(message)}>"
    )
    return begin + _text(body) + "</failure>"


class _Suite:
    """累计一个 testsuite 的 testcase 行与统计。"""

    def __init__(self, name):
        self.name = name
        self.lines = []
        self.tests = 0
        self.failures = 0
        self.skipped = 0

    def _testcase_open(self, indent, name, classname):
        return (
            f"{' ' * indent}<testcase name={_attr(name)} "
            f"classname={_attr(classname)}>"
        )

    def add_passing(self, name, classname):
        self.tests += 1
        self.lines.append(
            f"{' ' * 4}<testcase name={_attr(name)} "
            f"classname={_attr(classname)}/>"
        )

    def add_skipped(self, name, classname):
        self.tests += 1
        self.skipped += 1
        self.lines.append(self._testcase_open(4, name, classname))
        self.lines.append(" " * 6 + "<skipped/>")
        self.lines.append(" " * 4 + "</testcase>")

    def add_failure(self, name, classname, failure_type, message,
                    reason, diagnostics):
        self.tests += 1
        self.failures += 1
        self.lines.append(self._testcase_open(4, name, classname))
        self.lines.append(
            _failure_line(
                6, failure_type, message,
                _failure_body(reason, diagnostics),
            )
        )
        self.lines.append(" " * 4 + "</testcase>")


def _add_tb_outcome(suite, tb):
    """把单个测试台条目（已脱敏）展开为 suite 内 testcase。"""
    name = tb["name"]
    runs = tb.get("runs")
    if runs:
        # 多种子且存在 runs 明细：按执行顺序逐种子生成 testcase。
        for run in runs:
            case_name = f"{name}[seed={run['seed']}]"
            if run["status"] == "passed":
                suite.add_passing(case_name, name)
            else:
                reason = run["reason"]
                suite.add_failure(
                    case_name, name, _FAILURE_TYPES[reason], reason,
                    reason, run.get("diagnostics", ()),
                )
        return

    # 单种子，或多种子下编译失败/跳过（runs 为空）：同名 testcase。
    if tb["status"] == "passed":
        suite.add_passing(name, name)
    elif tb["status"] == "skipped":
        if tb.get("required", True):
            # 必测项被跳过：按失败记录（总体结论因此 failed）。
            suite.add_failure(
                name, name, REQUIRED_SKIPPED_TYPE, tb["reason"],
                tb["reason"], tb.get("diagnostics", ()),
            )
        else:
            suite.add_skipped(name, name)
    else:
        reason = tb["reason"]
        suite.add_failure(
            name, name, _FAILURE_TYPES[reason], reason,
            reason, tb.get("diagnostics", ()),
        )


def _coverage_suite(report):
    """覆盖率不达标时返回 coverage suite，否则返回 None。"""
    summary = report.get("coverage_summary")
    if not summary or summary.get("met"):
        return None
    suite = _Suite("coverage")
    ratio = summary["ratio"]
    ratio_text = "None" if ratio is None else str(ratio)
    body = (
        f"coverage {summary['hit_points']}/{summary['total_points']} "
        f"points hit, ratio={ratio_text}, "
        f"threshold={summary['threshold']}"
    )
    suite.add_failure(
        "threshold", "coverage", COVERAGE_FAILURE_TYPE,
        "coverage below threshold", body, (),
    )
    return suite


def _baseline_suite(report):
    """基线对比存在差异时返回 baseline suite，否则返回 None。"""
    comparison = report.get("comparison")
    if not comparison or comparison.get("passed"):
        return None
    suite = _Suite("baseline")
    count = len(comparison.get("mismatches", ()))
    body = f"baseline comparison failed: {count} mismatches"
    suite.add_failure(
        "comparison", "baseline", BASELINE_MISMATCH_TYPE,
        "baseline mismatch", body, (),
    )
    return suite


def build_junit_xml(report):
    """由最终 verify 报告 dict（已脱敏）构造确定性 JUnit XML 字符串。"""
    suites = []
    for tb in report["testbenches"]:
        suite = _Suite(tb["name"])
        _add_tb_outcome(suite, tb)
        suites.append(suite)

    coverage = _coverage_suite(report)
    if coverage is not None:
        suites.append(coverage)
    baseline = _baseline_suite(report)
    if baseline is not None:
        suites.append(baseline)

    total_tests = sum(s.tests for s in suites)
    total_failures = sum(s.failures for s in suites)
    total_skipped = sum(s.skipped for s in suites)

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<testsuites "
        f"name={_attr(report['run_id'])} "
        f'tests="{total_tests}" failures="{total_failures}" '
        f'errors="0" skipped="{total_skipped}">',
    ]
    for suite in suites:
        lines.append(
            "  <testsuite "
            f"name={_attr(suite.name)} "
            f'tests="{suite.tests}" failures="{suite.failures}" '
            f'errors="0" skipped="{suite.skipped}">'
        )
        lines.extend(suite.lines)
        lines.append("  </testsuite>")
    lines.append("</testsuites>")
    return "\n".join(lines) + "\n"
