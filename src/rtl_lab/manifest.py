"""verify 清单（``--manifest``）：从 JSON 清单构造等价的 :class:`VerifyConfig`。

清单是一个 UTF-8 JSON 对象，``schema_version`` 固定为 1：

- 必填：``run_id``、``duration``、``sources``（有序路径数组）、
  ``testbenches``（有序对象数组，每项以 ``testbench`` 指定文件，可选
  ``top``、``name``、``required``、``skip``、``seed``）与 ``coverage``
  （含 ``threshold``、``points``）；
- 可选：``compile``（含 ``include_dirs``、``defines``、``parameters``）、
  ``seeds``（非空时启用多种子矩阵，仍不可与测试台 ``seed`` 并用）、
  ``jobs``、``timeout``、``workdir``。

清单中的相对路径（``sources``、``testbench``、``include_dirs``、
``workdir``）一律按清单所在目录解析，与启动目录无关；数组顺序即配置
顺序。报告与基线路径不来自清单（仅 ``--report``/``--baseline`` 可与之
并用），仍按启动目录解析。

:func:`load_verify_manifest` 只做结构校验并构造等价的
:class:`VerifyConfig`，不执行编译或仿真；文件存在性、时长、种子矩阵等
其余字段校验沿用 :func:`rtl_lab.verification.verify` 的既有规则
（:class:`InputError`、:class:`ValueError` 与退出码 2、8 不变）。清单
不存在/不可读、不是 UTF-8 JSON 对象、``schema_version`` 不为 1 或含
未知键时抛 :class:`InputError`，且不生成或覆盖任何报告。
"""

import json
import os

from .errors import InputError
from .verification import CoverageConfig, TestSpec, VerifyConfig

#: 清单结构版本（``schema_version`` 的固定取值）。
MANIFEST_SCHEMA_VERSION = 1

#: 顶层允许的键。
_TOP_LEVEL_KEYS = frozenset({
    "schema_version",
    "run_id",
    "duration",
    "sources",
    "testbenches",
    "coverage",
    "compile",
    "seeds",
    "jobs",
    "timeout",
    "workdir",
})

#: 顶层必填键（按此顺序报告缺失）。
_REQUIRED_KEYS = ("run_id", "duration", "sources", "testbenches", "coverage")

#: 测试台条目允许的键；``testbench`` 必填，其余可选。
_TESTBENCH_KEYS = frozenset(
    {"testbench", "top", "name", "required", "skip", "seed"}
)

#: ``coverage`` 对象允许的键。
_COVERAGE_KEYS = frozenset({"threshold", "points"})

#: ``compile`` 对象允许的键。
_COMPILE_KEYS = frozenset({"include_dirs", "defines", "parameters"})


def _check_unknown_keys(obj, allowed, label):
    """拒绝未知键：清单结构之外的键一律视为输入错误。"""
    unknown = [k for k in obj if k not in allowed]
    if unknown:
        raise InputError(
            f"清单{label}含未知键：{'、'.join(repr(k) for k in unknown)}"
        )


def _str_field(value, label):
    if not isinstance(value, str):
        raise InputError(f"清单 {label} 必须是字符串")
    return value


def _str_list_field(value, label):
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        raise InputError(f"清单 {label} 必须是字符串数组")
    return list(value)


def _bool_field(value, label):
    if not isinstance(value, bool):
        raise InputError(f"清单 {label} 必须是布尔值")
    return value


def _int_field(value, label):
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputError(f"清单 {label} 必须是整数")
    return value


def _parse_testbenches(value, resolve):
    """把 ``testbenches`` 数组逐项转为 :class:`TestSpec`（顺序保留）。"""
    if not isinstance(value, list) or any(
        not isinstance(item, dict) for item in value
    ):
        raise InputError("清单 testbenches 必须是对象数组")
    specs = []
    for index, item in enumerate(value):
        label = f"testbenches[{index}]"
        _check_unknown_keys(item, _TESTBENCH_KEYS, f" {label}")
        if "testbench" not in item:
            raise InputError(f"清单 {label} 缺少 testbench")
        testbench = _str_field(item["testbench"], f"{label}.testbench")
        top = item.get("top")
        if top is not None:
            _str_field(top, f"{label}.top")
        name = item.get("name")
        if name is not None:
            _str_field(name, f"{label}.name")
        specs.append(TestSpec(
            testbench=resolve(testbench),
            top=top,
            name=name,
            required=_bool_field(item.get("required", True), f"{label}.required"),
            skip=_bool_field(item.get("skip", False), f"{label}.skip"),
            seed=_int_field(item.get("seed", 0), f"{label}.seed"),
        ))
    return specs


def _parse_coverage(value):
    """构造 :class:`CoverageConfig`；取值非法与 CLI 一样归为输入错误。"""
    if not isinstance(value, dict):
        raise InputError("清单 coverage 必须是对象")
    _check_unknown_keys(value, _COVERAGE_KEYS, " coverage")
    threshold = value.get("threshold", 1.0)
    points = _str_list_field(value.get("points", []), "coverage.points")
    try:
        return CoverageConfig(threshold=threshold, points=points)
    except ValueError as exc:
        raise InputError(str(exc)) from exc


def _parse_compile(value, resolve):
    """解析可选的 ``compile`` 对象，返回 (include_dirs, defines, parameters)。"""
    if value is None:
        return [], [], []
    if not isinstance(value, dict):
        raise InputError("清单 compile 必须是对象")
    _check_unknown_keys(value, _COMPILE_KEYS, " compile")
    include_dirs = [
        resolve(p)
        for p in _str_list_field(value.get("include_dirs", []), "compile.include_dirs")
    ]
    defines = _str_list_field(value.get("defines", []), "compile.defines")
    parameters = _str_list_field(
        value.get("parameters", []), "compile.parameters"
    )
    return include_dirs, defines, parameters


def load_verify_manifest(path):
    """读取 verify 清单并构造等价的 :class:`VerifyConfig`。

    只构造配置，不执行编译或仿真；返回的配置可直接传给
    :func:`rtl_lab.verification.verify`，执行语义与逐项参数完全一致。

    :raises InputError: 清单不存在、不可读、不是 UTF-8 JSON 对象、
        ``schema_version`` 不为 1、含未知键、缺少必填键或字段类型非法；
        这些情况不生成或覆盖任何报告。
    """
    if not isinstance(path, str) or not path.strip():
        raise InputError("清单路径不能为空")
    if not os.path.isfile(path):
        raise InputError(f"清单不存在：{path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise InputError(f"清单不是合法 JSON：{path}") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise InputError(f"清单不可读：{path}") from exc
    if not isinstance(data, dict):
        raise InputError(f"清单必须是 JSON 对象：{path}")

    _check_unknown_keys(data, _TOP_LEVEL_KEYS, "")
    schema_version = data.get("schema_version")
    if type(schema_version) is not int or schema_version != MANIFEST_SCHEMA_VERSION:
        raise InputError(
            f"清单 schema_version 必须为 {MANIFEST_SCHEMA_VERSION}：{path}"
        )
    missing = [key for key in _REQUIRED_KEYS if key not in data]
    if missing:
        raise InputError(f"清单缺少必填键：{'、'.join(missing)}")

    # 清单内相对路径按清单所在目录解析，与启动目录无关。
    base_dir = os.path.dirname(path)

    def resolve(p):
        if not base_dir or os.path.isabs(p):
            return p
        return os.path.join(base_dir, p)

    run_id = _str_field(data["run_id"], "run_id")
    duration = _str_field(data["duration"], "duration")
    sources = [resolve(p) for p in _str_list_field(data["sources"], "sources")]
    specs = _parse_testbenches(data["testbenches"], resolve)
    coverage = _parse_coverage(data["coverage"])
    include_dirs, defines, parameters = _parse_compile(
        data.get("compile"), resolve
    )

    seeds = data.get("seeds")
    if seeds is not None and not isinstance(seeds, list):
        raise InputError("清单 seeds 必须是非负整数数组")

    jobs = _int_field(data.get("jobs", 1), "jobs")

    workdir = data.get("workdir", ".")
    _str_field(workdir, "workdir")

    return VerifyConfig(
        sources=sources,
        testbenches=specs,
        run_id=run_id,
        duration=duration,
        coverage=coverage,
        # 仅清单中显式给出的相对 workdir 按清单目录解析；缺省沿用
        # VerifyConfig 的 "."（与逐项参数等价）。
        workdir=resolve(workdir) if "workdir" in data else workdir,
        jobs=jobs,
        seeds=seeds,
        timeout=data.get("timeout"),
        include_dirs=include_dirs,
        defines=defines,
        parameters=parameters,
    )
