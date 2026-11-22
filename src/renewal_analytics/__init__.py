"""Public entry points for the renewal portfolio planner."""

from .compare import compare_analyses, render_comparison
from .io import load_dataset
from .planner import analyze_scenario, build_demand
from .report import render_json, render_markdown, write_reports
from .scenarios import BUILTIN_SCENARIOS, load_scenario_file, resolve_scenario

__all__ = [
    "BUILTIN_SCENARIOS",
    "analyze_scenario",
    "build_demand",
    "compare_analyses",
    "load_dataset",
    "load_scenario_file",
    "render_comparison",
    "render_json",
    "render_markdown",
    "resolve_scenario",
    "write_reports",
]
