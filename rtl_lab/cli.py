"""Command line entry point: ``rtl-lab run``."""

import argparse
import sys

from .errors import (
    AssertionFailureError,
    CompilationError,
    InputError,
    RTLabError,
    SimulationError,
    ToolError,
)
from .runner import VALID_UNITS, run


def _positive_seed(value):
    try:
        ivalue = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"seed must be an integer, got {value!r}")
    return ivalue


def build_parser():
    parser = argparse.ArgumentParser(
        prog="rtl-lab",
        description="Compile Verilog with Icarus and run a testbench, "
                    "collecting assertions/coverage into a JSON report.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="compile and run one simulation")
    p_run.add_argument(
        "sources", nargs="+", metavar="SOURCE",
        help="design source file(s) (.v/.sv), compiled in the given order",
    )
    p_run.add_argument(
        "--testbench", "--tb", required=True, metavar="FILE",
        help="testbench file (.v/.sv), compiled after the design sources",
    )
    p_run.add_argument(
        "--top", required=True, metavar="MODULE",
        help="top-level simulation module",
    )
    p_run.add_argument(
        "--duration", required=True, metavar="TIME",
        help=f"simulation duration, positive integer with unit "
             f"({'/'.join(VALID_UNITS)}), e.g. 100ns",
    )
    p_run.add_argument(
        "--workdir", metavar="DIR", default=None,
        help="working directory for build artifacts (default ./.rtl-lab-work)",
    )
    p_run.add_argument(
        "--seed", type=_positive_seed, default=1, metavar="INT",
        help="random seed passed to the testbench as +seed=<INT> (default 1)",
    )
    p_run.add_argument(
        "--report", metavar="FILE", default=None,
        help="path of the JSON report to write",
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        try:
            run(
                sources=args.sources,
                testbench=args.testbench,
                top=args.top,
                duration=args.duration,
                workdir=args.workdir,
                seed=args.seed,
                report_path=args.report,
            )
        except CompilationError as exc:
            print(f"rtl-lab: {exc}", file=sys.stderr)
            return exc.exit_code
        except SimulationError as exc:
            print(f"rtl-lab: {exc}", file=sys.stderr)
            return exc.exit_code
        except AssertionFailureError as exc:
            print(f"rtl-lab: {exc}", file=sys.stderr)
            return exc.exit_code
        except ToolError as exc:
            print(f"rtl-lab: {exc}", file=sys.stderr)
            return exc.exit_code
        except InputError as exc:
            print(f"rtl-lab: input error: {exc}", file=sys.stderr)
            return InputError.exit_code
        except RTLabError as exc:
            print(f"rtl-lab: {exc}", file=sys.stderr)
            return exc.exit_code
        return 0

    parser.error(f"unknown command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
