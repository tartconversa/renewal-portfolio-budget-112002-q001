"""Command line interface for the renewal portfolio planner."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .compare import compare_reports, load_analysis_report, render_comparison
from .io import load_dataset
from .model import ScenarioSpec
from .planner import analyze_scenario
from .report import render_markdown, write_reports
from .scenarios import BUILTIN_SCENARIOS, load_scenario_file, resolve_scenario

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_NOT_EXECUTABLE = 2


def _load_spec(name: str | None, scenario_file: str | None) -> ScenarioSpec:
    if scenario_file:
        spec, issues = load_scenario_file(scenario_file)
        for issue in issues:
            print(_format_issue(issue), file=sys.stderr)
        if spec is None:
            raise SystemExit(EXIT_INVALID)
        return spec
    if not name:
        raise SystemExit("必须指定 --scenario 或 --scenario-file")
    if name not in BUILTIN_SCENARIOS:
        raise SystemExit(
            f"未知情景 {name}；可用: {', '.join(sorted(BUILTIN_SCENARIOS))}"
        )
    return BUILTIN_SCENARIOS[name]


def _format_issue(issue) -> str:
    where = f"{issue.file}:{issue.row}" if issue.file else ""
    subject = f" [{issue.subject}]" if issue.subject else ""
    return f"{issue.level.upper()} {issue.code}{subject} {where} {issue.message}".strip()


def _analyze(data_dir: str, spec: ScenarioSpec):
    dataset = load_dataset(data_dir)
    scenario = resolve_scenario(dataset, spec)
    return analyze_scenario(scenario)


def _report_and_exit(analysis, out_dir: str | None, emit: str) -> int:
    if emit == "markdown":
        print(render_markdown(analysis))
    elif emit == "json":
        print(json.dumps(analysis.as_dict(), ensure_ascii=False, indent=2))
    if out_dir:
        paths = write_reports(analysis, out_dir)
        print(f"报告已写入 {paths['markdown']} 与 {paths['json']}", file=sys.stderr)
    if analysis.status == "INVALID_INPUT":
        return EXIT_INVALID
    if analysis.status == "EXECUTABLE":
        return EXIT_OK
    return EXIT_NOT_EXECUTABLE


def cmd_validate(args: argparse.Namespace) -> int:
    dataset = load_dataset(args.data)
    errors = dataset.errors
    warnings = dataset.warnings
    print(
        json.dumps(
            {
                "errors": [issue.as_dict() for issue in errors],
                "warnings": [issue.as_dict() for issue in warnings],
                "counts": {
                    "projects": len(dataset.projects),
                    "funding_lines": len(dataset.funding),
                    "dependencies": len(dataset.dependencies),
                    "needs": len(dataset.needs),
                },
                "executable_input": not errors,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return EXIT_INVALID if errors else EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    spec = _load_spec(args.scenario, args.scenario_file)
    analysis = _analyze(args.data, spec)
    return _report_and_exit(analysis, args.out, args.emit)


def _load_side(
    *, report: str | None, scenario: str | None, scenario_file: str | None, data_dir: str
) -> dict:
    if report:
        return load_analysis_report(report)
    spec = _load_spec(scenario, scenario_file)
    return _analyze(data_dir, spec).as_dict()


def cmd_compare(args: argparse.Namespace) -> int:
    left = _load_side(
        report=args.before_report,
        scenario=args.before,
        scenario_file=args.before_file,
        data_dir=args.data,
    )
    right = _load_side(
        report=args.after_report,
        scenario=args.after,
        scenario_file=args.after_file,
        data_dir=args.data,
    )
    comparison = compare_reports(left, right)
    if args.emit == "json":
        print(json.dumps(comparison, ensure_ascii=False, indent=2))
    else:
        print(render_comparison(comparison))
    if args.out:
        target = Path(args.out)
        target.mkdir(parents=True, exist_ok=True)
        (target / f"compare-{left.get('scenario')}-to-{right.get('scenario')}.json").write_text(
            json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    if left.get("status") == "INVALID_INPUT" or right.get("status") == "INVALID_INPUT":
        return EXIT_INVALID
    if left.get("status") != "EXECUTABLE" or right.get("status") != "EXECUTABLE":
        return EXIT_NOT_EXECUTABLE
    return EXIT_OK


def cmd_list(_args: argparse.Namespace) -> int:
    for name, spec in BUILTIN_SCENARIOS.items():
        print(f"{name}\t{spec.cost_basis}\t{spec.description}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="renewal-analytics",
        description="城市更新年度组合测算：预算、依赖、共享开挖与片区受益比较",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="只校验输入材料，不生成方案")
    validate.add_argument("--data", default="data", help="输入目录（默认 data）")
    validate.set_defaults(func=cmd_validate)

    run = sub.add_parser("run", help="测算一个情景并输出批准确认方案")
    run.add_argument("--data", default="data")
    run.add_argument("--scenario", help="内置情景名称")
    run.add_argument("--scenario-file", help="自定义情景 JSON")
    run.add_argument("--out", help="报告输出目录")
    run.add_argument("--emit", choices=("markdown", "json"), default="markdown")
    run.set_defaults(func=cmd_run)

    compare = sub.add_parser("compare", help="比较两个情景的批准确认方案")
    compare.add_argument("--data", default="data")
    compare.add_argument("--before", help="变更前情景名称")
    compare.add_argument("--before-file", help="变更前自定义情景 JSON")
    compare.add_argument("--before-report", help="变更前冻结的 JSON 报告（保留当时依据）")
    compare.add_argument("--after", help="变更后情景名称")
    compare.add_argument("--after-file", help="变更后自定义情景 JSON")
    compare.add_argument("--after-report", help="变更后冻结的 JSON 报告")
    compare.add_argument("--out")
    compare.add_argument("--emit", choices=("markdown", "json"), default="markdown")
    compare.set_defaults(func=cmd_compare)

    listing = sub.add_parser("list-scenarios", help="列出内置情景")
    listing.set_defaults(func=cmd_list)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_INVALID


if __name__ == "__main__":
    raise SystemExit(main())
