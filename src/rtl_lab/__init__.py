"""RTL Lab：硬件仿真与 RTL 验证框架。

公开接口：

- :func:`run`：执行一次完整的 Verilog 编译与仿真流程；
- :func:`regress`：编译一次并按多个随机种子依次仿真（回归）；
- 异常类型 :class:`RTLLabError` 及其子类；
- :data:`SCHEMA_VERSION` / :data:`REGRESSION_SCHEMA_VERSION`：
  JSON 报告结构版本。
"""

from .errors import (
    RTLLabError,
    InputError,
    CompilationError,
    ToolError,
    SimulationError,
)
from .regression import RegressConfig, regress
from .report import REGRESSION_SCHEMA_VERSION, SCHEMA_VERSION
from .runner import run, RunConfig

__all__ = [
    "run",
    "RunConfig",
    "regress",
    "RegressConfig",
    "RTLLabError",
    "InputError",
    "CompilationError",
    "ToolError",
    "SimulationError",
    "SCHEMA_VERSION",
    "REGRESSION_SCHEMA_VERSION",
]
