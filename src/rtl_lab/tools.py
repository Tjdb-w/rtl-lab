"""对 Icarus Verilog（iverilog / vvp）的封装。

- :func:`compile_sources`：按给定顺序编译设计与测试台，并以顶层模块为根；
- :func:`run_simulation`：执行 vvp，传入随机种子 plusargs 与仿真时长看门狗。
"""

import shutil
import subprocess

from .errors import ToolError


#: 看门狗模块名（带下划线前缀，避免与用户模块冲突）。
WATCHDOG_MODULE = "__rtl_lab_watchdog"

#: 看门狗触发时打印的标记行。
WATCHDOG_MARKER = "RTL_LAB_DURATION_REACHED"


def find_executable(name):
    """查找可执行文件，找不到时抛 :class:`ToolError`。"""
    path = shutil.which(name)
    if path is None:
        raise ToolError(
            f"未找到 {name} 可执行文件：请安装 Icarus Verilog 并将其加入 PATH"
        )
    return path


def compile_sources(*, source_files, top, output_path, extra_roots=()):
    """按给定顺序调用 iverilog 编译并以顶层模块做 elaboration。

    :param source_files: 设计源文件 + 测试台（已校验顺序的路径列表）。
    :param top: 顶层模块名（``-s`` 根模块）。
    :param output_path: 编译产物（.vvp）路径。
    :param extra_roots: 额外的根模块（如看门狗）。
    :returns: ``(returncode, stdout, stderr, command)``。
    :raises ToolError: iverilog 无法启动。
    """
    iverilog = find_executable("iverilog")
    cmd = [iverilog, "-g2012", "-o", output_path, "-s", top]
    for root in extra_roots:
        cmd += ["-s", root]
    cmd += list(source_files)

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as exc:
        raise ToolError(f"无法启动 iverilog：{exc}") from exc
    return proc.returncode, proc.stdout, proc.stderr, cmd


def make_watchdog_source(path, delay_value, unit):
    """生成在 ``#delay_value`` 个时间单位后结束仿真的看门狗源文件。

    看门狗与用户设计并列作为根模块；测试台若提前 ``$finish`` 则仿真正常结束。
    时间单位通过看门狗自身的 ``timescale`` 注入，不改动用户源文件。
    """
    content = (
        "`timescale 1{unit}/1{unit}\n"
        "module {module};\n"
        "  initial begin\n"
        "    #{delay} begin\n"
        "      $display(\"{marker}\");\n"
        "      $finish;\n"
        "    end\n"
        "  end\n"
        "endmodule\n"
    ).format(unit=unit, module=WATCHDOG_MODULE, delay=delay_value,
             marker=WATCHDOG_MARKER)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def run_simulation(*, vvp_path, seed, cwd):
    """调用 vvp 执行仿真。

    随机种子以 plusarg ``+SEED=<seed>`` 传入测试台。

    :returns: ``(returncode, stdout, stderr, command)``。
    :raises ToolError: vvp 无法启动。
    """
    vvp = find_executable("vvp")
    cmd = [vvp, "-n", vvp_path, f"+SEED={seed}"]
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True
        )
    except OSError as exc:
        raise ToolError(f"无法启动 vvp：{exc}") from exc
    return proc.returncode, proc.stdout, proc.stderr, cmd
