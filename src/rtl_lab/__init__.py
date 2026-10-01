"""RTL Lab：硬件仿真与 RTL 验证框架。

公开接口：

- :func:`run`：执行一次完整的 Verilog 编译与仿真流程；
- 异常类型 :class:`RTLLabError` 及其子类；
- :data:`SCHEMA_VERSION`：JSON 报告结构版本。
"""

from .errors import (
    RTLLabError,
    InputError,
    CompilationError,
    ToolError,
    SimulationError,
)
from .report import SCHEMA_VERSION
from .runner import run, RunConfig

__all__ = [
    "run",
    "RunConfig",
    "RTLLabError",
    "InputError",
    "CompilationError",
    "ToolError",
    "SimulationError",
    "SCHEMA_VERSION",
]
