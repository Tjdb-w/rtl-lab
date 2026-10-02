"""验证报告的基线加载与结构化对比（schema v4）。

``verify --baseline PATH`` 把上次 ``verify`` 生成的 JSON 报告作为基线：
先按原流程执行并生成当前结果（执行顺序、种子、产物目录与字段语义均不
受影响），再把当前报告与基线按 name 对齐逐类对比——测试台比
``status``/``reason``，断言比终态 ``status``/``fail_count``，覆盖率比
``status``/``hits``/``hit_testbenches``。

每条差异固定为 ``kind``、``name``、``expected``、``actual`` 四个字段：

- 基线缺失（基线有、当前无）：``expected`` 为基线完整记录，
  ``actual`` 为 ``None``；
- 当前新增（基线无、当前有）：``expected`` 为 ``None``，``actual``
  为当前完整记录；
- 字段差异：``expected``/``actual`` 为对应标量。

差异按类别（testbench、assertion、coverage）顺序排列，类别内按 name
的 Unicode 码点排序；同名的多个字段差异分别保留，不合并。
"""

import json
import os

from .errors import InputError
from .report import (
    VERIFICATION_COMPARISON_SCHEMA_VERSION,
    VERIFICATION_FORMAT,
    VERIFICATION_SCHEMA_VERSION,
    sanitize_path,
)

#: 可作为基线读取的报告版本（v3 原始报告与 v4 带对比报告均可）。
SUPPORTED_BASELINE_SCHEMA_VERSIONS = (
    VERIFICATION_SCHEMA_VERSION,
    VERIFICATION_COMPARISON_SCHEMA_VERSION,
)

#: 各类别参与对比的字段；顺序即同名多个字段差异的稳定排列顺序。
_COMPARE_FIELDS = {
    "testbench": ("status", "reason"),
    "assertion": ("status", "fail_count"),
    "coverage": ("status", "hits", "hit_testbenches"),
}

#: 对比类别在 ``mismatches`` 中的排列顺序（映射到报告数组字段名）。
_CATEGORIES = (
    ("testbench", "testbenches"),
    ("assertion", "assertions"),
    ("coverage", "coverage"),
)


def load_baseline_report(path):
    """读取并校验基线报告，返回报告 dict。

    :raises InputError: 基线不存在、不可读、非合法 JSON、不是 rtl-lab
        验证报告或版本不受支持；这些情况不生成或覆盖任何报告。
    """
    if not isinstance(path, str) or not path.strip():
        raise InputError("基线报告路径不能为空")
    if not os.path.isfile(path):
        raise InputError(f"基线报告不存在：{path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise InputError(f"基线报告不是合法 JSON：{path}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise InputError(f"基线报告不可读：{path}") from exc
    if not isinstance(data, dict) or data.get("format") != VERIFICATION_FORMAT:
        raise InputError(f"基线不是 rtl-lab 验证报告：{path}")
    if data.get("schema_version") not in SUPPORTED_BASELINE_SCHEMA_VERSIONS:
        raise InputError(
            f"基线报告版本不受支持：{path}"
            f"（schema_version={data.get('schema_version')!r}）"
        )
    return data


def _compare_category(category, baseline_entries, current_entries):
    """按 name 对齐对比一个类别，返回该类别的差异列表（按 name 码点排序）。"""
    fields = _COMPARE_FIELDS[category]
    base_by_name = {e["name"]: e for e in baseline_entries}
    curr_by_name = {e["name"]: e for e in current_entries}
    diffs = []
    for name in sorted(set(base_by_name) | set(curr_by_name)):
        expected = base_by_name.get(name)
        actual = curr_by_name.get(name)
        if expected is None:
            diffs.append({
                "kind": f"{category}_added",
                "name": name,
                "expected": None,
                "actual": actual,
            })
        elif actual is None:
            diffs.append({
                "kind": f"{category}_missing",
                "name": name,
                "expected": expected,
                "actual": None,
            })
        else:
            for field in fields:
                if expected.get(field) != actual.get(field):
                    diffs.append({
                        "kind": f"{category}_{field}",
                        "name": name,
                        "expected": expected.get(field),
                        "actual": actual.get(field),
                    })
    return diffs


def compare_verification_reports(current, baseline, *, baseline_path, workdir):
    """对比当前报告与基线报告，返回报告的 ``comparison`` 字段值。

    ``baseline`` 为脱敏后的基线路径；``passed`` 为无差异标记；
    ``mismatches`` 为结构化差异列表（无差异时为空）。
    """
    mismatches = []
    for category, key in _CATEGORIES:
        mismatches.extend(_compare_category(
            category, baseline.get(key) or [], current.get(key) or [],
        ))
    return {
        "baseline": sanitize_path(baseline_path, workdir),
        "passed": not mismatches,
        "mismatches": mismatches,
    }
