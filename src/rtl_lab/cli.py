"""rtl-lab 命令行入口。

用法::

    rtl-lab run --tb TB.v --top top_module --duration 100ns \
        [--workdir DIR] [--seed N] [--report report.json] \
        design1.v [design2.v ...]

    rtl-lab regress --tb TB.v --top top_module --duration 100ns \
        --seeds 1,2,3 [--workdir DIR] [--report report.json] \
        design1.v [design2.v ...]

    rtl-lab verify --run-id ID --duration 100ns \
        --tb FILE[@top[@name]] ... [--cover NAME ...] \
        [--coverage-threshold R] [--skip NAME ...] [--optional NAME ...] \
        [--tb-seed NAME=SEED ...] [--jobs N] \
        [--baseline report.json] \
        [--workdir DIR] [--report report.json] \
        design1.v [design2.v ...]

退出码：

- 0：仿真正常结束且断言全部通过（verify 为总体结论 passed）；
- 2：输入校验失败（不生成报告），含 verify 基线不存在、不可读、非合法
  JSON、不是 rtl-lab 验证报告或版本不受支持；
- 3：编译失败（生成 compile_failed 报告）；
- 4：模拟器无法启动（不生成报告）；
- 5：仿真进程非零退出（生成 simulation_failed 报告）；
- 6：断言失败（生成 assertion_failed 报告）；
- 7：统一验证总体结论 failed（仍生成完整 verify 报告）；带基线时存在
  任一基线差异也为 failed（报告含 comparison，退出码 7）；
- 8：verify 前置条件失败（run_id 为空、输出位置不可写、结果归属/命名
  冲突、无可执行测试台或无任何覆盖率结果；不生成或覆盖报告）。
"""

import argparse
import os
import sys

from .errors import InputError, RTLLabError
from .runner import RegressConfig, RunConfig, parse_seeds, regress, run
from .verification import CoverageConfig, TestSpec, VerifyConfig, verify


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
        "verify", help="多测试台统一验证并生成 schema v3 报告"
                      "（--baseline 时为 schema v4）"
    )
    _add_verify_args(p_verify)
    return parser


def _add_verify_args(p):
    """添加 verify 子命令参数（sources 共用，测试台可多个）。"""
    p.add_argument(
        "sources", nargs="+", metavar="SOURCE",
        help="设计源文件（.v/.sv），所有测试台共享，按给定顺序编译",
    )
    p.add_argument(
        "--run-id", dest="run_id", required=True, metavar="ID",
        help="运行标识，不能为空；用于结果归属与追溯",
    )
    p.add_argument(
        "--duration", "--time", dest="duration", required=True,
        metavar="DURATION",
        help="仿真时长，正整数加单位：fs/ps/ns/us/ms/s，如 100ns",
    )
    p.add_argument(
        "--tb", "--testbench", dest="testbenches", action="append",
        required=True, metavar="FILE[@TOP[@NAME]]",
        help="测试台，可重复指定以选择多个；格式 文件[@顶层[@名称]]，"
             "省略顶层/名称时取文件 basename",
    )
    p.add_argument(
        "--workdir", dest="workdir", default=".", metavar="DIR",
        help="工作目录（各测试台产物置于其下独立子目录，默认当前目录）",
    )
    p.add_argument(
        "--coverage-threshold", dest="coverage_threshold", type=float,
        default=1.0, metavar="RATIO",
        help="覆盖率达标阈值 0~1，命中覆盖率点/总点（默认 1.0）",
    )
    p.add_argument(
        "--cover", dest="cover_points", action="append", default=[],
        metavar="NAME",
        help="显式要求的覆盖率点名，可重复；点名但无统计记为 unavailable",
    )
    p.add_argument(
        "--skip", dest="skip", action="append", default=[], metavar="NAME",
        help="跳过指定测试台名，可重复；跳过的必测项会使结论 failed",
    )
    p.add_argument(
        "--optional", dest="optional", action="append", default=[],
        metavar="NAME",
        help="把指定测试台名标记为非必测，可重复；跳过非必测不影响结论",
    )
    p.add_argument(
        "--tb-seed", dest="tb_seeds", action="append", default=[],
        metavar="NAME=SEED",
        help="为指定测试台名设置随机种子（默认 0），可重复",
    )
    p.add_argument(
        "--jobs", dest="jobs", type=int, default=1, metavar="N",
        help="并发执行测试台的数量（默认 1，结果始终按选择顺序收集）",
    )
    p.add_argument(
        "--report", dest="report", default=None, metavar="PATH",
        help="JSON 报告输出路径",
    )
    p.add_argument(
        "--baseline", dest="baseline", default=None, metavar="PATH",
        help="基线 verify JSON 报告路径；先生成当前结果再与基线对比，"
             "报告升级为 schema v4 并追加 comparison，有差异时结论 failed",
    )


def _parse_tb_spec(spec):
    """解析 ``FILE[@TOP[@NAME]]``；名称/顶层省略时返回 None 由 TestSpec 补。"""
    parts = spec.split("@")
    if len(parts) > 3 or not parts[0]:
        raise InputError(f"测试台规格非法：{spec!r}（应为 FILE[@TOP[@NAME]]）")
    if len(parts) >= 2 and not parts[1].strip():
        raise InputError(f"测试台顶层名为空：{spec!r}")
    if len(parts) == 3 and not parts[2].strip():
        raise InputError(f"测试台名称为空：{spec!r}")
    path = parts[0]
    top = parts[1] if len(parts) >= 2 else None
    name = parts[2] if len(parts) == 3 else None
    return path, top, name


def _build_verify_config(args):
    """把 verify 命令行参数组装为 :class:`VerifyConfig`。"""
    skip = set(args.skip)
    optional = set(args.optional)
    seeds = {}
    for item in args.tb_seeds:
        if "=" not in item:
            raise InputError(f"--tb-seed 格式应为 NAME=SEED：{item!r}")
        tname, value = item.split("=", 1)
        tname, value = tname.strip(), value.strip()
        if not tname or not value.isdigit():
            raise InputError(f"--tb-seed 格式应为 NAME=SEED：{item!r}")
        seeds[tname] = int(value)

    specs = []
    for spec in args.testbenches:
        path, top, name = _parse_tb_spec(spec)
        tname = name or os.path.splitext(os.path.basename(path))[0]
        specs.append(TestSpec(
            testbench=path, top=top, name=name,
            required=tname not in optional,
            skip=tname in skip,
            seed=seeds.get(tname, 0),
        ))

    try:
        coverage = CoverageConfig(
            threshold=args.coverage_threshold, points=args.cover_points
        )
    except ValueError as exc:
        raise InputError(str(exc)) from exc

    return VerifyConfig(
        sources=args.sources,
        testbenches=specs,
        run_id=args.run_id,
        duration=args.duration,
        coverage=coverage,
        workdir=args.workdir,
        report_path=args.report,
        baseline_path=args.baseline,
        jobs=args.jobs,
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
    tbs = report["testbench_summary"]
    cov = report["coverage_summary"]
    ass = report["assertion_summary"]
    ratio = cov["ratio"]
    ratio_text = "-" if ratio is None else f"{ratio:.0%}"
    line = (
        f"rtl-lab: {report['result']}："
        f"运行 {report['run_id']}；"
        f"测试台 通过 {tbs['passed']}/失败 {tbs['failed']}/跳过 {tbs['skipped']}；"
        f"断言 {ass['total']}（失败 {ass['failed']}）；"
        f"覆盖率 {cov['hit_points']}/{cov['total_points']} "
        f"({ratio_text}，阈值 {cov['threshold']:g})"
    )
    # 带基线对比时末尾汇总差异条数（0 条即与基线一致）。
    comparison = report.get("comparison")
    if comparison is not None:
        line += f"；基线差异 {len(comparison['mismatches'])} 条"
    print(line, file=sys.stderr)


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
            config = _build_verify_config(args)
            report = verify(config)
    except RTLLabError as exc:
        # 控制台展示诊断信息，但不替代 JSON 报告。
        print(f"rtl-lab: 错误：{exc}", file=sys.stderr)
        _emit_stream(exc.raw_stdout, sys.stdout)
        _emit_stream(exc.raw_stderr, sys.stderr)
        # 回归在编译/仿真/断言失败时仍生成报告，末尾照常显示汇总。
        if args.command == "regress" and exc.report is not None:
            _print_regress_summary(exc.report)
        return exc.exit_code
    except (ValueError, OSError, RuntimeError) as exc:
        # verify 前置条件/归属类失败：退出码 8，不生成或覆盖报告。
        # InputError 属于 RTLLabError，已在上面的分支按退出码 2 处理。
        print(f"rtl-lab: 错误：{exc}", file=sys.stderr)
        return 8

    if args.command == "verify":
        _print_verify_summary(report)
        return 0 if report["result"] == "passed" else 7

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
