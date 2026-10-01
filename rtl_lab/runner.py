"""Core compile/simulate/report pipeline."""

import json
import os
import re
import shutil
import subprocess

from .errors import (
    AssertionFailureError,
    CompilationError,
    InputError,
    SimulationError,
    ToolError,
)
from .results import ResultCollector

SCHEMA_VERSION = "1.0"

VALID_UNITS = ("fs", "ps", "ns", "us", "ms", "s")
VALID_SUFFIXES = (".v", ".sv")

_DURATION_RE = re.compile(r"^\s*(\d+)\s*(fs|ps|ns|us|ms|s)\s*$")
_ABS_PATH_RE = re.compile(r"(?:/[A-Za-z0-9_.@+\-]+)+/?")


def parse_duration(value):
    """Parse ``"<amount><unit>"`` into ``(amount:int, unit)``.

    Amount must be a positive integer. Raises :class:`InputError`.
    """
    if not isinstance(value, str):
        raise InputError("duration must be a string such as '100ns'")
    match = _DURATION_RE.match(value)
    if not match:
        raise InputError(
            f"invalid duration {value!r}: expected a positive integer with a "
            f"unit one of {', '.join(VALID_UNITS)}"
        )
    amount = int(match.group(1))
    unit = match.group(2)
    if amount <= 0:
        raise InputError(f"invalid duration {value!r}: duration must be positive")
    return amount, unit


def _validate_source(path, role):
    if not isinstance(path, str) or not path:
        raise InputError(f"{role} path must be a non-empty string")
    if not path.lower().endswith(VALID_SUFFIXES):
        raise InputError(
            f"{role} {path!r} must have a .v or .sv suffix"
        )
    if not os.path.isfile(path):
        raise InputError(f"{role} file does not exist: {path}")


def _display_path(path, base_abs):
    """Path representation safe to put in the report."""
    if not os.path.isabs(path):
        return path
    try:
        rel = os.path.relpath(path, base_abs)
    except ValueError:
        rel = None
    if rel is not None and not rel.startswith(".." + os.sep) and rel != "..":
        return rel
    # Absolute path outside the base directory: keep only the file name.
    return os.path.basename(path)


def _scrub_text(text, base_abs):
    """Remove absolute paths from captured tool output.

    Paths inside the base directory are shown relative to it; paths
    outside are reduced to their basename. Message text is otherwise kept
    unchanged.
    """
    if not text:
        return text
    prefix = base_abs.rstrip(os.sep)

    def replace(match):
        found = match.group(0)
        if found == "/":
            return found
        if found.startswith(prefix + os.sep) or found == prefix:
            return found[len(prefix):].lstrip(os.sep) or "."
        return os.path.basename(found.rstrip("/")) or found

    return _ABS_PATH_RE.sub(replace, text)


def _run_captured(argv, cwd):
    """Run a process capturing stdout/stderr fully (deadlock-free).

    Raises :class:`ToolError` if the executable cannot be started.
    """
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError:
        raise ToolError(f"tool not found: {argv[0]}")
    except OSError as exc:
        raise ToolError(f"could not start {argv[0]}: {exc}")
    return completed.returncode, completed.stdout, completed.stderr


def _build_report(*, tool, command, sources, testbench, top, amount, unit,
                  seed, status, diagnostics, collector):
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": tool,
        "command": command,
        "sources": sources,
        "testbench": testbench,
        "top": top,
        "duration": {"amount": amount, "unit": unit},
        "seed": seed,
        "status": status,
        "diagnostics": diagnostics,
        "assertions": collector.assertions,
        "coverage": collector.coverage,
    }


def run(sources, testbench, top, duration, *, workdir=None, seed=1,
        report_path=None, compiler="iverilog", simulator="vvp",
        console=None):
    """Validate inputs, compile, simulate, and build the report.

    Returns the report dict. On failure raises one of the typed errors in
    :mod:`rtl_lab.errors`; compilation/simulation/assertion errors carry the
    finished report on ``error.report``.
    """
    # ---- validation -------------------------------------------------------
    if not isinstance(sources, (list, tuple)) or not sources:
        raise InputError("sources must be a non-empty list of file paths")
    for src in sources:
        _validate_source(src, "design source")
    _validate_source(testbench, "testbench")

    if not isinstance(top, str) or not top.strip():
        raise InputError("top module name must be a non-empty string")
    top = top.strip()

    amount, unit = parse_duration(duration)

    if isinstance(seed, bool) or not isinstance(seed, int):
        raise InputError("seed must be an integer")

    if workdir is None:
        workdir = os.path.join(os.getcwd(), ".rtl-lab-work")
    os.makedirs(workdir, exist_ok=True)
    workdir_abs = os.path.abspath(workdir)
    # Reports are scrubbed relative to the invocation directory so they never
    # carry absolute paths from outside the user's project tree.
    base_abs = os.getcwd()

    if console is None:
        import sys
        console = sys

    sources_display = [_display_path(p, base_abs) for p in sources]
    testbench_display = _display_path(testbench, base_abs)

    # The tools run with the working directory as cwd, so hand them absolute
    # paths; the report shows the scrubbed display forms instead.
    sources_abs = [os.path.abspath(p) for p in sources]
    testbench_abs = os.path.abspath(testbench)

    sim_binary = os.path.join(workdir_abs, "sim.vvp")
    # Compile design sources in the given order, then the testbench.
    compile_argv = [
        compiler, "-g2012", "-s", top, "-o", sim_binary,
        *sources_abs, testbench_abs,
    ]
    duration_arg = f"+duration={amount}{unit}"
    seed_arg = f"+seed={seed}"
    simulate_argv = [simulator, sim_binary, duration_arg, seed_arg]

    command_record = {
        "compiler": "iverilog",
        "simulator": "vvp",
        "compile": [_display_path(a, base_abs) if os.path.isabs(a) else a
                    for a in compile_argv],
        "simulate": [_display_path(a, base_abs) if os.path.isabs(a) else a
                     for a in simulate_argv],
        "plusargs": {"duration": f"{amount}{unit}", "seed": str(seed)},
    }

    collector = ResultCollector()
    diagnostics = {
        "compiler": {"returncode": None, "stdout": "", "stderr": ""},
        "simulator": {"returncode": None, "stdout": "", "stderr": ""},
    }

    def current_report(status):
        return _build_report(
            tool="iverilog",
            command=command_record,
            sources=sources_display,
            testbench=testbench_display,
            top=top,
            amount=amount,
            unit=unit,
            seed=seed,
            status=status,
            diagnostics=diagnostics,
            collector=collector,
        )

    # ---- compile ----------------------------------------------------------
    if shutil.which(compiler) is None:
        raise ToolError(f"compiler not found on PATH: {compiler}")

    returncode, stdout_b, stderr_b = _run_captured(compile_argv, workdir_abs)
    compile_stdout = _scrub_text(stdout_b.decode("utf-8", "replace"), base_abs)
    compile_stderr = _scrub_text(stderr_b.decode("utf-8", "replace"), base_abs)
    diagnostics["compiler"].update(
        returncode=returncode, stdout=compile_stdout, stderr=compile_stderr
    )

    if returncode != 0:
        report = current_report("compile_failed")
        _maybe_write_report(report, report_path)
        raise CompilationError(
            f"compilation failed with exit code {returncode}", report
        )

    # ---- simulate ---------------------------------------------------------
    if shutil.which(simulator) is None:
        raise ToolError(f"simulator not found on PATH: {simulator}")

    sim_returncode, stdout_b, stderr_b = _run_captured(
        simulate_argv, workdir_abs
    )

    sim_stdout_raw = stdout_b.decode("utf-8", "replace")
    sim_stderr_raw = stderr_b.decode("utf-8", "replace")
    # Mirror the complete simulator output on the console. The console copy is
    # a convenience; the JSON report remains the authoritative record.
    try:
        console.stdout.write(sim_stdout_raw)
        console.stdout.flush()
        if sim_stderr_raw:
            console.stderr.write(sim_stderr_raw)
            console.stderr.flush()
    except Exception:
        pass

    collector.feed_text(sim_stdout_raw)

    sim_stdout = _scrub_text(sim_stdout_raw, base_abs)
    sim_stderr = _scrub_text(sim_stderr_raw, base_abs)
    diagnostics["simulator"].update(
        returncode=sim_returncode, stdout=sim_stdout, stderr=sim_stderr
    )

    # ---- verdict ----------------------------------------------------------
    if sim_returncode != 0:
        report = current_report("simulation_failed")
        _maybe_write_report(report, report_path)
        raise SimulationError(
            f"simulation exited with code {sim_returncode}", report
        )

    failed = collector.failed_assertions
    if failed:
        report = current_report("assertion_failed")
        _maybe_write_report(report, report_path)
        raise AssertionFailureError(
            f"{len(failed)} assertion(s) failed: {', '.join(failed)}", report
        )

    report = current_report("passed")
    _maybe_write_report(report, report_path)
    return report


def _maybe_write_report(report, report_path):
    if not report_path:
        return
    path = report_path
    if not os.path.isabs(path):
        path = os.path.abspath(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)
