"""Unit tests for the testbench output parser."""

import unittest

from rtl_lab.results import ResultCollector


class TestResultCollector(unittest.TestCase):
    def test_pass_and_cover(self):
        c = ResultCollector()
        c.feed_text(
            "noise line\n"
            "ASSERT PASS alpha\n"
            "COVER c1\n"
        )
        self.assertEqual(
            c.assertions,
            [{"name": "alpha", "status": "pass", "fail_count": 0}],
        )
        self.assertEqual(c.coverage, [{"name": "c1", "hits": 1}])
        self.assertEqual(c.failed_assertions, [])

    def test_last_result_wins_and_fail_count_accumulates(self):
        c = ResultCollector()
        c.feed_text(
            "ASSERT FAIL x boom\n"
            "ASSERT PASS x\n"
            "ASSERT FAIL x again\n"
        )
        self.assertEqual(
            c.assertions,
            [{"name": "x", "status": "fail", "fail_count": 2}],
        )
        self.assertEqual(c.failed_assertions, ["x"])

    def test_fail_then_pass_ends_green(self):
        c = ResultCollector()
        c.feed_text("ASSERT FAIL y msg\nASSERT PASS y\n")
        self.assertEqual(c.assertions[0]["status"], "pass")
        self.assertEqual(c.assertions[0]["fail_count"], 1)
        self.assertEqual(c.failed_assertions, [])

    def test_coverage_counts_repeated_hits(self):
        c = ResultCollector()
        c.feed_text("COVER k\nCOVER k\nCOVER k\n")
        self.assertEqual(c.coverage, [{"name": "k", "hits": 3}])

    def test_first_seen_order_is_preserved(self):
        c = ResultCollector()
        c.feed_text("ASSERT PASS b\nCOVER z\nASSERT PASS a\nCOVER y\n")
        self.assertEqual([a["name"] for a in c.assertions], ["b", "a"])
        self.assertEqual([cov["name"] for cov in c.coverage], ["z", "y"])

    def test_whitespace_and_empty_names_ignored(self):
        c = ResultCollector()
        c.feed_text(
            "  ASSERT PASS   spaced  \n"
            "ASSERT FAIL\n"
            "ASSERT PASS\n"
            "COVER\n"
            "ASSERT MAYBE x\n"
            "ASSERT\n"
        )
        self.assertEqual(
            c.assertions,
            [{"name": "spaced", "status": "pass", "fail_count": 0}],
        )
        self.assertEqual(c.coverage, [])

    def test_fail_message_is_not_part_of_the_name(self):
        c = ResultCollector()
        c.feed_text('ASSERT FAIL req1 signal was 0 expected 1\n')
        self.assertEqual(c.assertions[0]["name"], "req1")
        self.assertEqual(c.assertions[0]["fail_count"], 1)

    def test_crlf_and_blank_lines(self):
        c = ResultCollector()
        c.feed_text("\r\nASSERT PASS n\r\nCOVER c\r\n")
        self.assertEqual(c.assertions[0]["name"], "n")
        self.assertEqual(c.coverage[0]["name"], "c")


if __name__ == "__main__":
    unittest.main()
