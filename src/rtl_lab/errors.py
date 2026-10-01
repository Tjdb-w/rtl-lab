"""RTL Lab 的异常类型层次。"""


class RTLLabError(Exception):
    """所有 RTL Lab 错误的基类。

    :param message: 人类可读的错误描述。
    :param report: 已生成的 JSON 报告（dict）；输入校验阶段尚无报告。
    """

    #: 命令行对应的退出码。
    exit_code = 1

    def __init__(self, message, report=None, raw_stdout="", raw_stderr=""):
        super().__init__(message)
        self.report = report
        #: 未脱敏的原始 stdout/stderr（仅供控制台展示，报告中存放脱敏版本）。
        self.raw_stdout = raw_stdout or ""
        self.raw_stderr = raw_stderr or ""


class InputError(RTLLabError):
    """输入校验失败：文件不存在、后缀非法、顶层名为空、时长非法等。"""

    exit_code = 2


class CompilationError(RTLLabError):
    """设计或测试台编译（iverilog  elaboration）失败。"""

    exit_code = 3


class ToolError(RTLLabError):
    """外部工具（iverilog / vvp）无法启动。"""

    exit_code = 4


class SimulationError(RTLLabError):
    """仿真执行失败：进程非零退出或断言失败。

    :param assertion_failed: True 表示断言最终失败（退出码 6），
        False 表示仿真进程非零退出（退出码 5）。
    """

    exit_code = 5

    def __init__(self, message, report=None, assertion_failed=False,
                 raw_stdout="", raw_stderr=""):
        super().__init__(message, report, raw_stdout, raw_stderr)
        self.assertion_failed = assertion_failed
        if assertion_failed:
            self.exit_code = 6
