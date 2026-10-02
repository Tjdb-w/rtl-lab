"""RTL Lab：硬件仿真与 RTL 验证框架。

公开接口：

- :func:`run`：执行一次完整的 Verilog 编译与仿真流程；
- :func:`regress`：多随机种子回归（编译一次，多种子依次仿真）；
- :func:`verify`：多测试台统一验证，产出可复现的 schema v3 报告；
- 配置类 :class:`RunConfig` / :class:`RegressConfig` /
  :class:`VerifyConfig`、:class:`TestSpec`、:class:`CoverageConfig`；
- 异常类型 :class:`RTLLabError` 及其子类；
- :data:`SCHEMA_VERSION`：单次 JSON 报告结构版本；
- :data:`VERIFICATION_SCHEMA_VERSION`：统一验证报告结构版本。
"""

from .errors import (
    RTLLabError,
    InputError,
    CompilationError,
    ToolError,
    SimulationError,
)
from .report import SCHEMA_VERSION, VERIFICATION_SCHEMA_VERSION
from .runner import RegressConfig, RunConfig, regress, run
from .verification import CoverageConfig, TestSpec, VerifyConfig, verify

__all__ = [
    "run",
    "regress",
    "verify",
    "RunConfig",
    "RegressConfig",
    "VerifyConfig",
    "TestSpec",
    "CoverageConfig",
    "RTLLabError",
    "InputError",
    "CompilationError",
    "ToolError",
    "SimulationError",
    "SCHEMA_VERSION",
    "VERIFICATION_SCHEMA_VERSION",
]
