"""Tests for input validation and duration parsing."""

import os
import tempfile
import unittest

from rtl_lab.errors import InputError
from rtl_lab.runner import parse_duration, run


class TestParseDuration(unittest.TestCase):
    def test_every_unit(self):
        for unit in ("fs", "ps", "ns", "us", "ms", "s"):
            self.assertEqual(parse_duration(f"10{unit}"), (10, unit))

    def test_surrounding_whitespace_allowed(self):
        self.assertEqual(parse_duration("  3 us "), (3, "us"))

    def test_rejects_zero_and_negative(self):
        for bad in ("0ns", "0 s"):
            with self.assertRaises(InputError):
                parse_duration(bad)

    def test_rejects_non_integer_and_unknown_unit(self):
        for bad in ("1.5ns", "ns", "10", "10xs", "-1ns", "abc", "100 NS"):
            with self.assertRaises(InputError):
                parse_duration(bad)


class TestRunValidation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.src = os.path.join(self.tmp, "dut.v")
        self.tb = os.path.join(self.tmp, "tb.v")
        with open(self.src, "w") as fh:
            fh.write("module dut; endmodule\n")
        with open(self.tb, "w") as fh:
            fh.write("module tb; initial #1 $finish; endmodule\n")

    def tearDown(self):
        self._tmp.cleanup()

    def _kwargs(self, **over):
        kw = dict(
            sources=[self.src],
            testbench=self.tb,
            top="tb",
            duration="10ns",
            workdir=os.path.join(self.tmp, "work"),
        )
        kw.update(over)
        return kw

    def test_missing_source(self):
        with self.assertRaises(InputError):
            run(**self._kwargs(sources=[os.path.join(self.tmp, "nope.v")]))

    def test_bad_suffix(self):
        other = os.path.join(self.tmp, "notes.txt")
        open(other, "w").close()
        with self.assertRaises(InputError):
            run(**self._kwargs(sources=[other]))
        with self.assertRaises(InputError):
            run(**self._kwargs(testbench=other))

    def test_suffix_case_and_sv_accepted(self):
        sv = os.path.join(self.tmp, "dut.sv")
        with open(sv, "w") as fh:
            fh.write("module dut; endmodule\n")
        # Only the pure-validation suffix surface is exercised here.
        from rtl_lab.runner import _validate_source
        _validate_source(sv, "design source")
        upper = os.path.join(self.tmp, "DUT.V")
        with open(upper, "w") as fh:
            fh.write("")
        _validate_source(upper, "design source")

    def test_empty_top(self):
        for bad in ("", "   "):
            with self.assertRaises(InputError):
                run(**self._kwargs(top=bad))

    def test_bad_duration(self):
        with self.assertRaises(InputError):
            run(**self._kwargs(duration="0ns"))
        with self.assertRaises(InputError):
            run(**self._kwargs(duration="5xs"))

    def test_bad_seed(self):
        for bad in ("1", 1.0, True, None):
            with self.assertRaises(InputError):
                run(**self._kwargs(seed=bad))

    def test_empty_sources(self):
        with self.assertRaises(InputError):
            run(**self._kwargs(sources=[]))


if __name__ == "__main__":
    unittest.main()
