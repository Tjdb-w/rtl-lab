"""rtl-lab 命令行入口。

用法::

    rtl-lab run --tb TB.v --top top_module --duration 100ns \
        [--workdir DIR] [--seed N] [--report report.json] \
        design1.v [design2.v ...]

退出码：

- 0：仿真正常结束且断言全部通过；
- 2：输入校验失败（不生成报告）；
- 3：编译失败（生成 compile_failed 报告）；
- 4：模拟器无法启动；
- 5：仿真进程非零退出（生成 simulation_failed 报告）；
- 6：断言失败（生成 assertion_failed 报告）。
"""

import argparse
import sys

from .errors import RTLLabError
from .runner import RunConfig, run


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="rtl-lab",
        description="RTL Lab：编译 Verilog 设计、运行仿真并生成验证报告",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="编译并执行一次仿真")
    p_run.add_argument(
        "sources", nargs="+", metavar="SOURCE",
        help="设计源文件（.v/.sv），按给定顺序编译",
    )
    p_run.add_argument(
        "--tb", "--testbench", dest="testbench", required=True,
        metavar="FILE", help="测试台源文件（.v/.sv）",
    )
    p_run.add_argument(
        "--top", required=True, metavar="MODULE", help="顶层模块名",
    )
    p_run.add_argument(
        "--duration", "--time", dest="duration", required=True,
        metavar="DURATION",
        help="仿真时长，正整数加单位：fs/ps/ns/us/ms/s，如 100ns",
    )
    p_run.add_argument(
        "--workdir", dest="workdir", default=".", metavar="DIR",
        help="工作目录（默认当前目录）",
    )
    p_run.add_argument(
        "--seed", type=int, default=0, metavar="INT",
        help="随机种子，以 +SEED=<seed> plusarg 传入测试台（默认 0）",
    )
    p_run.add_argument(
        "--report", dest="report", default=None, metavar="PATH",
        help="JSON 报告输出路径",
    )
    return parser


def _emit_stream(text, stream):
    """把一段已捕获的输出原样写回指定控制台流。"""
    if text:
        stream.write(text)
        if not text.endswith("\n"):
            stream.write("\n")


def main(argv=None):
    """命令行入口，返回进程退出码。"""
    parser = _build_parser()
    args = parser.parse_args(argv)

    config = RunConfig(
        sources=args.sources,
        testbench=args.testbench,
        top=args.top,
        duration=args.duration,
        workdir=args.workdir,
        seed=args.seed,
        report_path=args.report,
    )

    try:
        report = run(config)
    except RTLLabError as exc:
        # 控制台展示诊断信息，但不替代 JSON 报告。
        print(f"rtl-lab: 错误：{exc}", file=sys.stderr)
        _emit_stream(exc.raw_stdout, sys.stdout)
        _emit_stream(exc.raw_stderr, sys.stderr)
        return exc.exit_code

    # 成功：将仿真 stdout/stderr 分别完整回放到对应控制台流。
    _emit_stream(report.raw_stdout, sys.stdout)
    _emit_stream(report.raw_stderr, sys.stderr)

    n_fail = sum(a["fail_count"] for a in report["assertions"])
    print(
        f"rtl-lab: {report['status']}："
        f"{len(report['assertions'])} 个断言，{n_fail} 次失败，"
        f"{len(report['coverage'])} 个覆盖率点",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
