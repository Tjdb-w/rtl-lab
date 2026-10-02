"""rtl-lab 命令行入口。

用法::

    rtl-lab run --tb TB.v --top top_module --duration 100ns \
        [--workdir DIR] [--seed N] [--report report.json] \
        design1.v [design2.v ...]

    rtl-lab regress --tb TB.v --top top_module --duration 100ns \
        --seeds 1,2,3 [--workdir DIR] [--report report.json] \
        design1.v [design2.v ...]

    rtl-lab verify --run-id ID --tb TB.v [--tb TB2.v ...] \
        --top top_module --duration 100ns [--workdir DIR] \
        [--cov-threshold RATIO] [--report report.json] \
        design1.v [design2.v ...]

退出码：

- 0：仿真正常结束且断言全部通过（verify 还要求覆盖率达标、无跳过必测项）；
- 2：输入校验失败（不生成报告）；
- 3：编译失败（生成 compile_failed 报告）；
- 4：模拟器无法启动（不生成报告）；
- 5：仿真进程非零退出（生成 simulation_failed 报告）；
- 6：断言失败（生成 assertion_failed 报告）；
- 7：verify 输出位置不可写（不生成、不改动已有报告）。
"""

import argparse
import os
import sys

from .errors import RTLLabError
from .runner import RegressConfig, RunConfig, parse_seeds, regress, run
from .verify import CoverageConfig, TestSpec, VerifyConfig, verify


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

    p_verify = sub.add_parser(
        "verify", help="多测试台统一验证并生成可复现报告（schema v3）"
    )
    _add_verify_args(p_verify)
    return parser


def _add_verify_args(p):
    """verify 子命令参数：多测试台选择、跳过与覆盖率配置。"""
    p.add_argument(
        "sources", nargs="+", metavar="SOURCE",
        help="设计源文件（.v/.sv），按给定顺序编译",
    )
    p.add_argument(
        "--run-id", dest="run_id", required=True, metavar="ID",
        help="运行标识：非空，作为报告可复现性与结果归属锚点",
    )
    p.add_argument(
        "--tb", dest="tbs", action="append", required=True, metavar="PATH",
        help="测试台源文件，可多次指定；可用 NAME=PATH 显式命名",
    )
    p.add_argument(
        "--top", required=True, metavar="MODULE",
        help="默认顶层模块名（可用 --tb-top 按测试台覆盖）",
    )
    p.add_argument(
        "--tb-top", dest="tb_tops", action="append", default=[],
        metavar="NAME=TOP", help="为指定测试台单独设置顶层模块",
    )
    p.add_argument(
        "--tb-seed", dest="tb_seeds", action="append", default=[],
        metavar="NAME=SEED", help="为指定测试台单独设置随机种子",
    )
    p.add_argument(
        "--tb-skip", dest="tb_skips", action="append", default=[],
        metavar="NAME[=REASON]",
        help="跳过指定测试台（可附跳过原因），报告记为 skipped",
    )
    p.add_argument(
        "--tb-optional", dest="tb_optional", action="append", default=[],
        metavar="NAME", help="指定测试台为非必测（跳过不影响 passed）",
    )
    p.add_argument(
        "--duration", "--time", dest="duration", required=True,
        metavar="DURATION",
        help="仿真时长，正整数加单位：fs/ps/ns/us/ms/s，如 100ns",
    )
    p.add_argument(
        "--cov-threshold", dest="cov_threshold", default=None,
        metavar="RATIO", help="覆盖率阈值（0~1），低于则整体 failed",
    )
    p.add_argument(
        "--cov-point", dest="cov_points", action="append", default=[],
        metavar="POINT",
        help="声明期望覆盖点，可多次指定；未观测到的点按 0 命中计入",
    )
    p.add_argument(
        "--cov-required", dest="cov_required", action="append", default=[],
        metavar="POINT", help="必须命中的覆盖点，可多次指定",
    )
    p.add_argument(
        "--workdir", dest="workdir", default=".", metavar="DIR",
        help="工作目录（默认当前目录）",
    )
    p.add_argument(
        "--report", dest="report", default=None, metavar="PATH",
        help="JSON 报告输出路径",
    )


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


def _print_verify_summary(report):
    """统一验证的结尾汇总行（写 stderr）。"""
    totals = report["totals"]
    assertions = report["assertions"]
    cov = report["coverage_summary"]
    print(
        f"rtl-lab: {report['conclusion']}：运行 {report['run_id']}，"
        f"{totals['total']} 个测试台（{totals['passed']} 通过/"
        f"{totals['failed']} 失败/{totals['skipped']} 跳过），"
        f"{assertions['total']} 个断言（{assertions['failed']} 失败），"
        f"覆盖率 {cov['points_hit']}/{cov['points_total']}"
        f"（{cov['hit_ratio']:.2%}）",
        file=sys.stderr,
    )


def _named_value(token, *, value_is_path=False):
    """解析 ``NAME=VALUE``；value_is_path 时 VALUE 为测试台路径。

    路径模式下：若整个 token 本身就是已存在的文件，则整体作为路径
    （名称由文件 stem 推导），从而允许路径中含 ``=``；否则按首个
    ``=`` 拆分为显式名称与路径。普通模式要求必须含 ``=``。
    """
    if value_is_path and os.path.isfile(token):
        return None, token
    if "=" not in token:
        if value_is_path:
            return None, token
        raise ValueError(f"期望 NAME=VALUE，收到 {token!r}")
    name, value = token.split("=", 1)
    name = name.strip()
    if not name:
        raise ValueError(f"NAME=VALUE 的名称不能为空：{token!r}")
    return name, value


def _build_verify_config(args):
    """把 verify 子命令参数组装为 VerifyConfig（非法参数 ValueError）。"""
    testbenches = []
    for token in args.tbs:
        name, path = _named_value(token, value_is_path=True)
        testbenches.append(TestSpec(path, name=name))

    tops = {}
    for token in args.tb_tops:
        name, value = _named_value(token)
        tops[name] = value
    seeds = {}
    for token in args.tb_seeds:
        name, value = _named_value(token)
        if not value.isdigit():
            raise ValueError(f"测试台 {name!r} 的种子必须为非负整数")
        seeds[name] = int(value)
    skips = {}
    for token in args.tb_skips:
        if "=" in token:
            name, reason = token.split("=", 1)
        else:
            name, reason = token, ""
        name = name.strip()
        if not name:
            raise ValueError(f"跳过目标名称不能为空：{token!r}")
        skips[name] = reason
    optional = set()
    for token in args.tb_optional:
        name = token.strip()
        if not name:
            raise ValueError("--tb-optional 的名称不能为空")
        optional.add(name)

    known = {t.effective_name for t in testbenches}
    for label, mapping in (("--tb-top", tops), ("--tb-seed", seeds),
                           ("--tb-skip", skips)):
        unknown = sorted(set(mapping) - known)
        if unknown:
            raise ValueError(
                f"{label} 引用了不存在的测试台：{', '.join(unknown)}"
            )
    unknown = sorted(optional - known)
    if unknown:
        raise ValueError(
            f"--tb-optional 引用了不存在的测试台：{', '.join(unknown)}"
        )

    for spec in testbenches:
        name = spec.effective_name
        if name in tops:
            spec.top = tops[name]
        if name in seeds:
            spec.seed = seeds[name]
        if name in skips:
            spec.skip = True
            spec.skip_reason = skips[name]
        if name in optional:
            spec.required = False

    threshold = None
    if args.cov_threshold is not None:
        try:
            threshold = float(args.cov_threshold)
        except ValueError:
            raise ValueError(
                f"覆盖率阈值必须是 0~1 的数值，收到 {args.cov_threshold!r}"
            )
        if not 0 <= threshold <= 1:
            raise ValueError("覆盖率阈值必须落在 [0, 1] 区间")

    return VerifyConfig(
        sources=args.sources,
        testbenches=testbenches,
        default_top=args.top,
        duration=args.duration,
        run_id=args.run_id,
        coverage=CoverageConfig(
            threshold=threshold,
            points=args.cov_points,
            required_points=args.cov_required,
        ),
        workdir=args.workdir,
        report_path=args.report,
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
        elif args.command == "regress":
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
        else:
            # verify：参数组装阶段的 ValueError 与入口 ValueError 同码 2。
            config = _build_verify_config(args)
            report = verify(config)
    except ValueError as exc:
        # verify 的 run_id 为空、配置语义非法等：退出码 2（不生成报告）。
        print(f"rtl-lab: 错误：{exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        # 输出位置不可写：退出码 7，不生成、不改动已有报告。
        print(f"rtl-lab: 错误：{exc}", file=sys.stderr)
        return 7
    except RuntimeError as exc:
        # 结果不属于本次运行、命名冲突、重复记录、空结果等：退出码 1。
        print(f"rtl-lab: 错误：{exc}", file=sys.stderr)
        return 1
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
    elif args.command == "regress":
        _print_regress_summary(report)
    else:
        _print_verify_summary(report)
        # verify 即使结论 failed 也已生成完整报告，进程返回 6 与“检查失败”
        # 语义一致；passed 才返回 0。
        if report["conclusion"] != "passed":
            return 6
    return 0


if __name__ == "__main__":
    sys.exit(main())
