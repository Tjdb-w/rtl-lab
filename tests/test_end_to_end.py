"""End-to-end tests using the real iverilog/vvp toolchain.

Skipped when Icarus Verilog is not available on PATH.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from rtl_lab.cli import main
from rtl_lab.errors import (
    AssertionFailureError,
    CompilationError,
    SimulationError,
    ToolError,
)
from rtl_lab.runner import run

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

HAVE_ICARUS = shutil.which("iverilog") is not None and shutil.which("vvp") is not None
skip_no_icarus = unittest.skipUnless(HAVE_ICARUS, "iverilog/vvp not installed")


def _fixture(name):
    return os.path.join(FIXTURES, name)


@skip_no_icarus
class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.work = os.path.join(self.tmp, "work")
        self.report = os.path.join(self.tmp, "out", "report.json")

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, fixture_tb, *, top="tb", sources=None, duration="1us", seed=7):
        return run(
            sources=sources if sources is not None else [_fixture("counter.v")],
            testbench=_fixture(fixture_tb),
            top=top,
            duration=duration,
            workdir=self.work,
            seed=seed,
            report_path=self.report,
        )

    def test_passed(self):
        report = self._run("tb_pass.v")
        self.assertEqual(report["status"], "passed")
        self.assertTrue(os.path.isfile(self.report))
        on_disk = json.load(open(self.report, encoding="utf-8"))
        self.assertEqual(on_disk["status"], "passed")

        self.assertEqual(on_disk["schema_version"], "1.0")
        self.assertEqual(on_disk["tool"], "iverilog")
        self.assertEqual(on_disk["top"], "tb")
        self.assertEqual(on_disk["duration"], {"amount": 1, "unit": "us"})
        self.assertEqual(on_disk["seed"], 7)
        # Arrays keep input / first-seen order.
        self.assertEqual(
            [a["name"] for a in on_disk["assertions"]],
            ["check_reset", "check_count"],
        )
        self.assertEqual(
            [c["name"] for c in on_disk["coverage"]],
            ["reset_seen", "counting_seen"],
        )
        self.assertEqual(
            on_disk["assertions"][1],
            {"name": "check_count", "status": "pass", "fail_count": 0},
        )
        # No coverage points is not an error elsewhere; here hits accumulate.
        self.assertEqual(on_disk["coverage"][1]["hits"], 3)
        # The seed reached the testbench stdout.
        self.assertIn("seed=7", on_disk["diagnostics"]["simulator"]["stdout"])
        self.assertIn("+seed=7", on_disk["command"]["simulate"])

    def test_assertion_failed_wins_by_last_result(self):
        with self.assertRaises(AssertionFailureError) as ctx:
            self._run("tb_assert_fail.v")
        report = ctx.exception.report
        self.assertEqual(report["status"], "assertion_failed")
        by_name = {a["name"]: a for a in report["assertions"]}
        # Flaky: FAIL then PASS -> final pass, but failure was counted.
        self.assertEqual(by_name["flaky"]["status"], "pass")
        self.assertEqual(by_name["flaky"]["fail_count"], 1)
        # Stable failure stays failed.
        self.assertEqual(by_name["stable"]["status"], "fail")
        self.assertEqual(by_name["stable"]["fail_count"], 2)
        # Report still written.
        self.assertEqual(json.load(open(self.report))["status"], "assertion_failed")

    def test_no_coverage_points_is_not_failure(self):
        report = self._run("tb_no_cover.v")
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["coverage"], [])

    def test_compile_failure(self):
        bad = os.path.join(self.tmp, "bad.v")
        with open(bad, "w") as fh:
            fh.write("module broken(\n")
        with self.assertRaises(CompilationError) as ctx:
            run(
                sources=[bad],
                testbench=_fixture("tb_pass.v"),
                top="tb",
                duration="1us",
                workdir=self.work,
                report_path=self.report,
            )
        report = ctx.exception.report
        self.assertEqual(report["status"], "compile_failed")
        self.assertNotEqual(report["diagnostics"]["compiler"]["returncode"], 0)
        self.assertTrue(report["diagnostics"]["compiler"]["stderr"])
        self.assertIsNone(report["diagnostics"]["simulator"]["returncode"])
        self.assertEqual(json.load(open(self.report))["status"], "compile_failed")

    def test_simulation_nonzero_exit(self):
        with self.assertRaises(SimulationError) as ctx:
            self._run("tb_fatal.v", top="tb_fatal")
        report = ctx.exception.report
        self.assertEqual(report["status"], "simulation_failed")
        self.assertNotEqual(report["diagnostics"]["simulator"]["returncode"], 0)
        combined = (
            report["diagnostics"]["simulator"]["stdout"]
            + report["diagnostics"]["simulator"]["stderr"]
        )
        self.assertIn("blew up", combined)
        self.assertTrue(combined.strip())
        self.assertEqual(json.load(open(self.report))["status"], "simulation_failed")

    def test_missing_simulator_tool_is_tool_error(self):
        with self.assertRaises(ToolError):
            run(
                sources=[_fixture("counter.v")],
                testbench=_fixture("tb_pass.v"),
                top="tb",
                duration="1us",
                workdir=self.work + "-notool",
                report_path=self.report,
                simulator="vvp-does-not-exist",
            )
        # Tool errors produce no report (the tool never ran).
        self.assertFalse(os.path.exists(self.report))

    def test_console_shows_full_stdout(self):
        captured = io.StringIO()
        fake = type("S", (), {"stdout": captured, "stderr": io.StringIO()})()
        run(
            sources=[_fixture("counter.v")],
            testbench=_fixture("tb_pass.v"),
            top="tb",
            duration="1us",
            workdir=self.work,
            seed=1,
            console=fake,
        )
        self.assertIn("ASSERT PASS check_count", captured.getvalue())

    def test_report_leaks_no_absolute_paths(self):
        report = self._run("tb_pass.v")
        blob = json.dumps(report)
        self.assertNotIn(self.tmp, blob)
        self.assertNotIn(FIXTURES, blob)
        for source in report["sources"]:
            self.assertFalse(os.path.isabs(source))
        self.assertFalse(os.path.isabs(report["testbench"]))

    def test_sources_compiled_in_given_order(self):
        # A module defined in a later file; reversing order would still link
        # for iverilog, so instead assert the recorded command order matches
        # the input order exactly.
        sources = [_fixture("counter.v"), _fixture("extra.v")]
        report = run(
            sources=sources,
            testbench=_fixture("tb_pass.v"),
            top="tb",
            duration="1us",
            workdir=self.work,
            seed=1,
        )
        compile_args = report["command"]["compile"]
        idx_counter = compile_args.index(
            next(a for a in compile_args if a.endswith("counter.v"))
        )
        idx_extra = compile_args.index(
            next(a for a in compile_args if a.endswith("extra.v"))
        )
        self.assertLess(idx_counter, idx_extra)


@skip_no_icarus
class TestCli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.work = os.path.join(self.tmp, "work")
        self.report = os.path.join(self.tmp, "report.json")

    def tearDown(self):
        self._tmp.cleanup()

    def _invoke(self, *args):
        return main(list(args))

    def test_exit_zero_and_report(self):
        rc = self._invoke(
            "run", _fixture("counter.v"),
            "--testbench", _fixture("tb_pass.v"),
            "--top", "tb",
            "--duration", "1us",
            "--seed", "99",
            "--workdir", self.work,
            "--report", self.report,
        )
        self.assertEqual(rc, 0)
        data = json.load(open(self.report))
        self.assertEqual(data["status"], "passed")
        self.assertEqual(data["seed"], 99)

    def test_exit_codes(self):
        # 2: input error, no report written
        rc = self._invoke(
            "run", os.path.join(self.tmp, "missing.v"),
            "--testbench", _fixture("tb_pass.v"),
            "--top", "tb", "--duration", "1us", "--report", self.report,
        )
        self.assertEqual(rc, 2)
        self.assertFalse(os.path.exists(self.report))

        # 3: compilation failure
        bad = os.path.join(self.tmp, "bad.v")
        open(bad, "w").write("module x(\n")
        rc = self._invoke(
            "run", bad,
            "--testbench", _fixture("tb_pass.v"),
            "--top", "tb", "--duration", "1us",
            "--workdir", self.work, "--report", self.report,
        )
        self.assertEqual(rc, 3)
        self.assertEqual(json.load(open(self.report))["status"], "compile_failed")

        # 6: assertion failure
        rc = self._invoke(
            "run", _fixture("counter.v"),
            "--testbench", _fixture("tb_assert_fail.v"),
            "--top", "tb", "--duration", "1us",
            "--workdir", self.work + "2", "--report", self.report,
        )
        self.assertEqual(rc, 6)
        self.assertEqual(json.load(open(self.report))["status"], "assertion_failed")

        # 5: simulation failure
        rc = self._invoke(
            "run", _fixture("counter.v"),
            "--testbench", _fixture("tb_fatal.v"),
            "--top", "tb_fatal", "--duration", "1us",
            "--workdir", self.work + "3", "--report", self.report,
        )
        self.assertEqual(rc, 5)
        self.assertEqual(json.load(open(self.report))["status"], "simulation_failed")

    def test_console_script_installed(self):
        exe = shutil.which("rtl-lab")
        if not exe:
            self.skipTest("rtl-lab console script not on PATH")
        out = subprocess.run(
            [exe, "--help"], capture_output=True, text=True, check=False
        )
        self.assertEqual(out.returncode, 0)
        self.assertIn("run", out.stdout)


if __name__ == "__main__":
    unittest.main()
