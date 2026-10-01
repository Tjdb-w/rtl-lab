"""时长解析单测。"""

import pytest

from rtl_lab.duration import parse_duration, normalize_duration, UNITS
from rtl_lab.errors import InputError


@pytest.mark.parametrize("text,amount,unit", [
    ("1fs", 1, "fs"), ("10ps", 10, "ps"), ("100ns", 100, "ns"),
    ("2us", 2, "us"), ("3ms", 3, "ms"), ("5s", 5, "s"),
    ("  7ns  ", 7, "ns"), ("42NS", 42, "ns"),
])
def test_valid_durations(text, amount, unit):
    assert parse_duration(text) == (amount, unit)


def test_normalize_fs_value():
    assert normalize_duration("1us") == (1, "us", 10 ** 9)
    assert normalize_duration("1s") == (1, "s", 10 ** 15)


@pytest.mark.parametrize("text", [
    "0ns", "00ps", "abc", "100", "100 x", "100mhz", "ns",
    "1.5ns", "-5ns", "", "100ns100",
])
def test_invalid_durations(text):
    with pytest.raises(InputError):
        parse_duration(text)


def test_all_six_units_supported():
    assert set(UNITS) == {"fs", "ps", "ns", "us", "ms", "s"}
