"""--seeds 解析与 RegressConfig 校验单测。"""

import pytest

from rtl_lab.errors import InputError
from rtl_lab.runner import parse_seeds


def test_comma_separated_basic():
    assert parse_seeds("0,1,2") == [0, 1, 2]


def test_whitespace_around_items():
    assert parse_seeds(" 3 , 7,  42 ") == [3, 7, 42]


def test_leading_zeros_allowed():
    assert parse_seeds("00,007") == [0, 7]


def test_large_nonnegative_ints():
    assert parse_seeds("0,99999999999999999999") == [0, 99999999999999999999]


def test_list_input_accepted():
    assert parse_seeds([5, 6]) == [5, 6]


@pytest.mark.parametrize("bad", [
    "",          # 完全为空
    "1,,3",      # 中间空值
    ",",         # 仅分隔符
    " 1, ,2",    # 空白项
    "-1",        # 负数
    "1,-2",      # 含负数
    "1.5",       # 非整数
    "1,2.0,3",
    "abc",       # 非数字
    "1,x",
    "+1",        # 带符号
    "0x1",       # 非十进制
    "1e3",       # 科学计数法
])
def test_invalid_string_tokens_rejected(bad):
    with pytest.raises(InputError):
        parse_seeds(bad)


@pytest.mark.parametrize("bad", [
    "1,1",          # 重复
    "1,2,1",
    "0,0",
    " 3 ,3",        # 带空白的重复
])
def test_duplicate_seeds_rejected(bad):
    with pytest.raises(InputError):
        parse_seeds(bad)


@pytest.mark.parametrize("bad", [[-1], [1, -2], [True], [1, False],
                                 ["a"], [1, 2.0], []])
def test_invalid_list_items_rejected(bad):
    with pytest.raises(InputError):
        parse_seeds(bad)


def test_non_string_non_list_rejected():
    with pytest.raises(InputError):
        parse_seeds(42)
