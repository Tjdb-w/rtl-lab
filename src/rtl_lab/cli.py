"""rtl-lab 命令行入口。

用法::

    rtl-lab run --tb TB.v --top top_module --duration 100ns \
        [--workdir DIR] [--seed N] [--report report.json] \
        design1.v [design2.v ...]

    rtl-lab regress --tb TB.v --top top_module --duration 100ns \
        --seeds 1,2,3 [--workdir DIR] [--report report.json] \
        design1.v [design2.v ...]

退出码：

- 0：仿真正常结束且断言全部通过；
- 2：输入校验失败（不生成报告）；
- 3：编译失败（生成 compile_failed 报告）；
- 4：模拟器无法启动（不生成报告）；
- 5：仿真进程非零退出（生成 simulation_failed 报告）；
- 6：断言失败（生成 assertion_failed 报告）。
"""

import argparse
import sys

from .errors import RTLLabError
from .runner import RegressConfig, RunConfig, parse_seeds, regress, run


def _add_common_args(p, *, with_seed):
    """添加 run / regress 共用的参数。"""
    p.add_argument(
        "sources", nargs="+", metavar="SOURCE",
        help="设计源文件（.v/.sv），按给定顺序编译",
    )
    p.add_argument(
        "--tb", "--testbench", dest="testbench", required=True,
        metavar="FILE", help="测试台源文件（.v/.sv）",
    )
    p.add_argument(
        "--top", required=True, metavar="MODULE", help="顶层模块名",
    )
    p.add_argument(
        "--duration", "--time", dest="duration", required=True,
        metavar="DURATION",
        help="仿真时长，正整数加单位：fs/ps/ns/us/ms/s，如 100ns",
    )
    p.add_argument(
        "--workdir", dest="workdir", default=".", metavar="DIR",
        help="工作目录（默认当前目录）",
    )
    if with_seed:
        p.add_argument(
            "--seed", type=int, default=0, metavar="INT",
            help="随机种子，以 +SEED=<seed> plusarg 传入测试台（默认 0）",
        )
    else:
        p.add_argument(
            "--seeds", dest="seeds", required=True, metavar="INT,INT,...",
            help="逗号分隔的非负整数随机种子，按顺序依次以 "
                 "+SEED=<seed> 仿真，不可重复",
        )
    p.add_argument(
        "--report", dest="report", default=None, metavar="PATH",
        help="JSON 报告输出路径",
    )


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="rtl-lab",
        description="RTL Lab：编译 Verilog 设计、运行仿真并生成验证报告",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="编译并执行一次仿真")
    _add_common_args(p_run, with_seed=True)

    p_regress = sub.add_parser(
        "regress", help="编译一次并对多个随机种子依次仿真"
    )
    _add_common_args(p_regress, with_seed=False)
    return parser


def _emit_stream(text, stream):
    """把一段已捕获的输出原样写回指定控制台流。"""
    if text:
        stream.write(text)
        if not text.endswith("\n"):
            stream.write("\n")


def _print_run_summary(report):
    """单次运行的结尾汇总行（写 stderr）。"""
    n_fail = sum(a["fail_count"] for a in report["assertions"])
    print(
        f"rtl-lab: {report['status']}："
        f"{len(report['assertions'])} 个断言，{n_fail} 次失败，"
        f"{len(report['coverage'])} 个覆盖率点",
        file=sys.stderr,
    )


def _print_regress_summary(report):
    """回归的结尾汇总行：种子数、失败断言、覆盖率点（写 stderr）。"""
    failed_names = [
        a["name"] for a in report["assertions"]
        if a["status"] == "failed"
    ]
    n_fail = sum(a["fail_count"] for a in report["assertions"])
    print(
        f"rtl-lab: {report['status']}："
        f"{len(report['seeds'])} 个种子，"
        f"{len(failed_names)} 个失败断言（{n_fail} 次失败），"
        f"{len(report['coverage'])} 个覆盖率点",
        file=sys.stderr,
    )


def main(argv=None):
    """命令行入口，返回进程退出码。"""
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "run":
            config = RunConfig(
                sources=args.sources,
                testbench=args.testbench,
                top=args.top,
                duration=args.duration,
                workdir=args.workdir,
                seed=args.seed,
                report_path=args.report,
            )
            report = run(config)
        else:
            # 种子解析失败（空值/负数/非整数/重复）与其他输入错误同为退出码 2。
            config = RegressConfig(
                sources=args.sources,
                testbench=args.testbench,
                top=args.top,
                duration=args.duration,
                seeds=parse_seeds(args.seeds),
                workdir=args.workdir,
                report_path=args.report,
            )
            report = regress(config)
    except RTLLabError as exc:
        # 控制台展示诊断信息，但不替代 JSON 报告。
        print(f"rtl-lab: 错误：{exc}", file=sys.stderr)
        _emit_stream(exc.raw_stdout, sys.stdout)
        _emit_stream(exc.raw_stderr, sys.stderr)
        # 回归在编译/仿真/断言失败时仍生成报告，末尾照常显示汇总。
        if args.command == "regress" and exc.report is not None:
            _print_regress_summary(exc.report)
        return exc.exit_code

    # 成功：将仿真 stdout/stderr 分别完整回放到对应控制台流。
    _emit_stream(report.raw_stdout, sys.stdout)
    _emit_stream(report.raw_stderr, sys.stderr)

    if args.command == "run":
        _print_run_summary(report)
    else:
        _print_regress_summary(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
