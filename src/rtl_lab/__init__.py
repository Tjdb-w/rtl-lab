"""RTL Lab：硬件仿真与 RTL 验证框架。

公开接口：

- :func:`run`：执行一次完整的 Verilog 编译与仿真流程；
- :func:`regress`：多随机种子回归（编译一次，多种子依次仿真）；
- 异常类型 :class:`RTLLabError` 及其子类；
- :data:`SCHEMA_VERSION`：单次 JSON 报告结构版本。
"""

from .errors import (
    RTLLabError,
    InputError,
    CompilationError,
    ToolError,
    SimulationError,
)
from .report import SCHEMA_VERSION
from .runner import RegressConfig, RunConfig, regress, run

__all__ = [
    "run",
    "regress",
    "RunConfig",
    "RegressConfig",
    "RTLLabError",
    "InputError",
    "CompilationError",
    "ToolError",
    "SimulationError",
    "SCHEMA_VERSION",
]
