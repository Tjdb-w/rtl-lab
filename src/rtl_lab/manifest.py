"""verify 清单（``--manifest``）加载：JSON 清单 → 等价 :class:`VerifyConfig`。

清单是一个普通 UTF-8 编码的 JSON 对象，``schema_version`` 固定为 1：

- 必填：``run_id``、``duration``、``sources``、``testbenches``、``coverage``；
- 可选：``compile``、``seeds``、``jobs``、``timeout``、``workdir``；
- ``sources`` 为有序路径数组；``testbenches`` 为有序对象数组，每项以
  ``testbench`` 指定文件，可选 ``top``、``name``、``required``、``skip``、
  ``seed``；``coverage`` 含 ``threshold``、``points``；``compile`` 含
  ``include_dirs``、``defines``、``parameters``；
- 相对路径（``sources``、``testbench``、``include_dirs``、``workdir``）
  一律以清单所在目录解析，与启动目录无关；数组顺序即配置顺序；
- 顶层 ``seeds`` 非空时启用多种子矩阵，仍禁止与测试台 ``seed`` 同时生效
  （沿用 verify 既有校验）。

:func:`load_verify_manifest` 只构造等价的 :class:`VerifyConfig`，不执行
编译或仿真；构造后的配置与逐项参数走完全相同的执行、断言与覆盖率解析、
基线比较和 JSON 报告路径。清单不可读、非普通 UTF-8 JSON 对象、
``schema_version`` 不为 1 或含未知键时抛 :class:`InputError`（退出码 2，
不生成或覆盖报告）；其余字段校验沿用既有 :class:`InputError` /
:class:`ValueError` 语义。
"""

import json
import os

from .errors import InputError
from .verification import CoverageConfig, TestSpec, VerifyConfig

#: 当前支持的清单 schema 版本。
MANIFEST_SCHEMA_VERSION = 1

#: 顶层允许的键。
_TOP_LEVEL_KEYS = frozenset({
    "schema_version", "run_id", "duration", "sources", "testbenches",
    "coverage", "compile", "seeds", "jobs", "timeout", "workdir",
})

#: 顶层必填键。
_REQUIRED_KEYS = frozenset({
    "run_id", "duration", "sources", "testbenches", "coverage",
})

#: 测试台条目允许的键（``testbench`` 必填，其余可选）。
_TESTBENCH_KEYS = frozenset({
    "testbench", "top", "name", "required", "skip", "seed",
})

#: coverage 对象允许的键。
_COVERAGE_KEYS = frozenset({"threshold", "points"})

#: compile 对象允许的键。
_COMPILE_KEYS = frozenset({"include_dirs", "defines", "parameters"})


def _reject_unknown_keys(obj, allowed, label):
    """对象含未知键时抛 :class:`InputError`（键名按字典序列出）。"""
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise InputError(f"{label}含未知键：{', '.join(unknown)}")


def _require_object(value, label):
    """要求 JSON 对象（dict），否则抛 :class:`InputError`。"""
    if not isinstance(value, dict):
        raise InputError(f"{label}必须是 JSON 对象")
    return value


def _require_str_list(value, label):
    """要求字符串数组并返回列表副本，否则抛 :class:`InputError`。"""
    if not isinstance(value, list):
        raise InputError(f"{label}必须是数组")
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise InputError(f"{label}的元素必须是非空字符串")
    return list(value)


def _resolve(base_dir, path):
    """相对路径按清单目录解析；绝对路径原样保留。"""
    if os.path.isabs(path):
        return path
    return os.path.normpath(os.path.join(base_dir, path))


def _load_json_object(path):
    """读取清单文件并解析为 JSON 对象；任何读取/解析失败均为 InputError。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise InputError(f"清单不可读：{path}（{exc}）") from exc
    except UnicodeDecodeError as exc:
        raise InputError(f"清单不是普通 UTF-8 文本：{path}（{exc}）") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InputError(f"清单不是合法 JSON：{path}（{exc}）") from exc
    return _require_object(data, "清单")


def _build_testbenches(items, base_dir):
    """把 testbenches 数组转换为有序 :class:`TestSpec` 列表。"""
    if not isinstance(items, list):
        raise InputError("testbenches 必须是数组")
    specs = []
    for index, item in enumerate(items):
        label = f"testbenches[{index}]"
        _require_object(item, label)
        _reject_unknown_keys(item, _TESTBENCH_KEYS, label)
        if "testbench" not in item:
            raise InputError(f"{label} 缺少必填键 testbench")
        testbench = item["testbench"]
        if not isinstance(testbench, str) or not testbench.strip():
            raise InputError(f"{label}.testbench 必须是非空字符串")
        for key in ("top", "name"):
            if key in item and (
                not isinstance(item[key], str) or not item[key].strip()
            ):
                raise InputError(f"{label}.{key} 必须是非空字符串")
        for key in ("required", "skip"):
            if key in item and not isinstance(item[key], bool):
                raise InputError(f"{label}.{key} 必须是布尔值")
        if "seed" in item and (
            isinstance(item["seed"], bool)
            or not isinstance(item["seed"], int)
        ):
            raise InputError(f"{label}.seed 必须是非负整数")
        specs.append(TestSpec(
            testbench=_resolve(base_dir, testbench),
            top=item.get("top"),
            name=item.get("name"),
            required=item.get("required", True),
            skip=item.get("skip", False),
            seed=item.get("seed", 0),
        ))
    return specs


def _build_coverage(obj):
    """构造 :class:`CoverageConfig`；校验失败与 CLI 一样归为 InputError。"""
    _require_object(obj, "coverage")
    _reject_unknown_keys(obj, _COVERAGE_KEYS, "coverage")
    points = obj.get("points", [])
    if not isinstance(points, list):
        raise InputError("coverage.points 必须是数组")
    try:
        return CoverageConfig(
            threshold=obj.get("threshold", 1.0), points=points
        )
    except ValueError as exc:
        raise InputError(str(exc)) from exc


def _build_compile(obj, base_dir):
    """解析 compile 对象，返回 ``(include_dirs, defines, parameters)``。"""
    _require_object(obj, "compile")
    _reject_unknown_keys(obj, _COMPILE_KEYS, "compile")
    include_dirs = [
        _resolve(base_dir, p)
        for p in _require_str_list(
            obj.get("include_dirs", []), "compile.include_dirs"
        )
    ]
    defines = _require_str_list(obj.get("defines", []), "compile.defines")
    parameters = _require_str_list(
        obj.get("parameters", []), "compile.parameters"
    )
    return include_dirs, defines, parameters


def load_verify_manifest(path):
    """加载 verify 清单并返回等价的 :class:`VerifyConfig`。

    只构造配置，不执行编译或仿真；报告、基线与 JUnit XML 输出路径不
    属于清单内容，由调用方（CLI 的 ``--report`` / ``--baseline`` /
    ``--junit``，按启动目录解析）在返回的配置上另行设置。

    :param path: 清单文件路径（JSON 对象，``schema_version`` 为 1）。
    :raises InputError: 清单不可读、非普通 UTF-8 JSON 对象、
        ``schema_version`` 不为 1、含未知键、缺少必填键或字段非法。
    """
    data = _load_json_object(path)

    schema_version = data.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != MANIFEST_SCHEMA_VERSION
    ):
        raise InputError(
            f"清单 schema_version 必须为 {MANIFEST_SCHEMA_VERSION}，"
            f"收到 {schema_version!r}"
        )

    _reject_unknown_keys(data, _TOP_LEVEL_KEYS, "清单")
    missing = sorted(_REQUIRED_KEYS - set(data))
    if missing:
        raise InputError(f"清单缺少必填键：{', '.join(missing)}")

    # 相对路径以清单所在目录解析（取绝对路径，保证与启动目录无关）。
    base_dir = os.path.dirname(os.path.abspath(path))

    sources = [
        _resolve(base_dir, p)
        for p in _require_str_list(data["sources"], "sources")
    ]
    specs = _build_testbenches(data["testbenches"], base_dir)
    coverage = _build_coverage(data["coverage"])
    include_dirs, defines, parameters = _build_compile(
        data.get("compile", {}), base_dir
    )

    workdir = data.get("workdir", ".")
    if not isinstance(workdir, str) or not workdir.strip():
        raise InputError("workdir 必须是非空字符串")

    # jobs/seeds/timeout/run_id/duration 等字段的类型与取值校验沿用
    # VerifyConfig 及 verify 的既有规则（InputError/ValueError）。
    return VerifyConfig(
        sources=sources,
        testbenches=specs,
        run_id=data["run_id"],
        duration=data["duration"],
        coverage=coverage,
        workdir=_resolve(base_dir, workdir),
        jobs=data.get("jobs", 1),
        seeds=data.get("seeds"),
        timeout=data.get("timeout"),
        include_dirs=include_dirs,
        defines=defines,
        parameters=parameters,
    )
