"""仿真时长解析。

时长以正整数加单位表示，单位限 Verilog 时间单位：
``fs``、``ps``、``ns``、``us``、``ms``、``s``。
"""

import re

#: 支持的时间单位（全部按相对于 1 fs 的倍数换算）。
UNITS = ("fs", "ps", "ns", "us", "ms", "s")

_UNIT_FACTOR_FS = {
    "fs": 1,
    "ps": 10 ** 3,
    "ns": 10 ** 6,
    "us": 10 ** 9,
    "ms": 10 ** 12,
    "s": 10 ** 15,
}

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([a-zA-Z]+)\s*$")


def parse_duration(value):
    """解析 ``"100ns"`` 形式的时长。

    :returns: ``(amount, unit)``，amount 为正整数，unit 为规范化小写单位。
    :raises InputError: 格式错误、数值非正或单位不受支持。
    """
    from .errors import InputError

    if not isinstance(value, str):
        raise InputError(f"时长必须是字符串，收到 {type(value).__name__}")

    match = _DURATION_RE.match(value)
    if not match:
        raise InputError(
            f"无法解析时长 {value!r}：应为正整数加单位，单位限 {', '.join(UNITS)}"
        )

    amount = int(match.group(1))
    unit = match.group(2).lower()

    if amount <= 0:
        raise InputError(f"时长必须为正数，收到 {amount!r}")
    if unit not in _UNIT_FACTOR_FS:
        raise InputError(
            f"不支持的时间单位 {unit!r}：仅支持 {', '.join(UNITS)}"
        )

    return amount, unit


def normalize_duration(value):
    """解析时长并返回 ``(amount, unit, fs_value)``。"""
    amount, unit = parse_duration(value)
    return amount, unit, amount * _UNIT_FACTOR_FS[unit]
