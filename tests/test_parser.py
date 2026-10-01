"""ASSERT/COVER 输出解析单测。"""

from rtl_lab.parser import ResultCollector


def test_pass_and_cover():
    c = ResultCollector()
    c.feed_text("ASSERT PASS a\nCOVER b\n")
    assert c.assertions == {"a": {"status": "passed", "fail_count": 0}}
    assert c.coverage == {"b": 1}


def test_fail_records_message_and_count():
    c = ResultCollector()
    c.feed_line("ASSERT FAIL f expected 1 got 0")
    entry = c.assertions["f"]
    assert entry["status"] == "failed"
    assert entry["fail_count"] == 1


def test_last_result_wins():
    c = ResultCollector()
    c.feed_line("ASSERT FAIL x msg")
    c.feed_line("ASSERT PASS x")
    assert c.assertions["x"]["status"] == "passed"
    assert c.assertions["x"]["fail_count"] == 1
    assert c.has_failure is False


def test_fail_after_pass_is_final_failure():
    c = ResultCollector()
    c.feed_line("ASSERT PASS x")
    c.feed_line("ASSERT FAIL x again")
    assert c.has_failure is True
    assert c.assertions["x"]["fail_count"] == 1


def test_repeated_fail_accumulates():
    c = ResultCollector()
    for _ in range(3):
        c.feed_line("ASSERT FAIL x boom")
    assert c.assertions["x"]["fail_count"] == 3


def test_cover_hit_count():
    c = ResultCollector()
    c.feed_text("COVER c1\nCOVER c1\nCOVER c2\n")
    assert c.coverage == {"c1": 2, "c2": 1}


def test_first_seen_order_preserved():
    c = ResultCollector()
    c.feed_text("ASSERT PASS z\nCOVER aa\nASSERT FAIL a m\nCOVER bb\n")
    assert list(c.assertions) == ["z", "a"]
    assert list(c.coverage) == ["aa", "bb"]


def test_blank_and_whitespace_names_ignored():
    c = ResultCollector()
    for line in ("ASSERT PASS   ", "ASSERT FAIL   ", "COVER  ",
                 "ASSERT PASS", "ASSERT FAIL", "COVER"):
        assert c.feed_line(line) is None
    assert c.assertions == {}
    assert c.coverage == {}


def test_unrelated_lines_ignored():
    c = ResultCollector()
    assert c.feed_line("ASSERT PASSING nope") is None
    assert c.feed_line("VCD warning") is None
    assert c.feed_line("ASSERT PASS real_one") is not None
    assert list(c.assertions) == ["real_one"]


def test_crlf_and_tabs():
    c = ResultCollector()
    c.feed_text("ASSERT PASS\ta\r\nCOVER\tb\r\n")
    assert "a" in c.assertions
    assert c.coverage == {"b": 1}


def test_no_coverage_is_not_failure():
    c = ResultCollector()
    c.feed_line("ASSERT PASS a")
    assert c.has_failure is False
    assert c.coverage == {}
