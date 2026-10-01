"""JSON 报告的构建与路径脱敏。

报告顶层固定包含：
``schema_version``、``tool``、``command``、``sources``、``testbench``、
``top``、``duration``、``seed``、``status``、``diagnostics``、
``assertions``、``coverage``。
"""

import json
import os
import re

#: JSON 报告结构版本，后续功能在此公开报告结构上扩展。
SCHEMA_VERSION = 1


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


def build_report(*, tool, command, sources, testbench, top, duration, seed,
                 status, diagnostics, assertions, coverage, workdir,
                 known_paths=()):
    """组装报告 dict；路径与诊断文本均做脱敏处理。

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
        "schema_version": 1,
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
        "assertions": [
            {
                "name": name,
                "status": assertions[name]["status"],
                "fail_count": assertions[name]["fail_count"],
            }
            for name in assertions
        ],
        "coverage": [
            {"name": name, "hits": coverage[name]}
            for name in coverage
        ],
    }


def write_report(report, report_path):
    """将报告写入 JSON 文件（UTF-8、两空格缩进、末尾换行）。"""
    parent = os.path.dirname(os.path.abspath(report_path))
    os.makedirs(parent, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write("\n")
