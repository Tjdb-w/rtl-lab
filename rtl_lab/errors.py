"""Typed errors for RTL Lab.

Each operational error carries the process exit code the command line uses.
"""


class RTLabError(Exception):
    """Base class for all RTL Lab errors."""

    exit_code = 1


class InputError(RTLabError):
    """Invalid user input (missing files, bad suffix/top/duration)."""

    exit_code = 2


class CompilationError(RTLabError):
    """The design/testbench failed to compile.

    The partially built report is attached so the CLI can still write the
    ``compile_failed`` JSON report.
    """

    exit_code = 3

    def __init__(self, message, report):
        super().__init__(message)
        self.report = report


class ToolError(RTLabError):
    """The simulator tool could not be started (missing binary, etc.)."""

    exit_code = 4


class SimulationError(RTLabError):
    """The simulation process ended with a non-zero exit code."""

    exit_code = 5

    def __init__(self, message, report):
        super().__init__(message)
        self.report = report


class AssertionFailureError(RTLabError):
    """At least one assertion's final state is FAIL."""

    exit_code = 6

    def __init__(self, message, report):
        super().__init__(message)
        self.report = report
