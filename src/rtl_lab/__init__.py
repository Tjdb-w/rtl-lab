"""RTL Lab：硬件仿真与 RTL 验证框架。

公开接口：

- :func:`run`：执行一次完整的 Verilog 编译与仿真流程（schema v1）；
- :func:`regress`：多随机种子回归（编译一次，多种子依次仿真，v2）；
- :func:`verify`：统一验证执行，多个测试台独立编译/仿真并生成可复现、
  可追溯的统一报告（schema v3）；
- 配置类型 :class:`RunConfig` / :class:`RegressConfig` /
  :class:`VerifyConfig` 与测试台选择 :class:`TestSpec`、覆盖率配置
  :class:`CoverageConfig`；
- 异常类型 :class:`RTLLabError` 及其子类；
- 数据常量 :data:`SCHEMA_VERSION`（v1）、:data:`REGRESS_SCHEMA_VERSION`
  （v2）、:data:`VERIFY_SCHEMA_VERSION`（v3）。
"""

from .errors import (
    RTLLabError,
    InputError,
    CompilationError,
    ToolError,
    SimulationError,
)
from .report import REGRESS_SCHEMA_VERSION, SCHEMA_VERSION
from .runner import RegressConfig, RunConfig, regress, run
from .verify import (
    CoverageConfig,
    TestSpec,
    VERIFY_SCHEMA_VERSION,
    VerifyConfig,
    verify,
)

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
    "REGRESS_SCHEMA_VERSION",
    "VERIFY_SCHEMA_VERSION",
]
