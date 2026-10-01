"""RTL Lab: compile Verilog designs, run a testbench, and emit a verification report."""

from .errors import (
    RTLabError,
    InputError,
    CompilationError,
    ToolError,
    SimulationError,
    AssertionFailureError,
)
from .runner import run

__version__ = "0.1.0"

__all__ = [
    "run",
    "RTLabError",
    "InputError",
    "CompilationError",
    "ToolError",
    "SimulationError",
    "AssertionFailureError",
    "__version__",
]
