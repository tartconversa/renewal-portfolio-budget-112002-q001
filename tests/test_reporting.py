"""Tests for reporting, frozen-baseline comparison and CLI exit codes."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from renewal_analytics.cli import main
from renewal_analytics.compare import compare_reports, load_analysis_report, render_comparison
from renewal_analytics.io import load_dataset
from renewal_analytics.planner import analyze_scenario
from renewal_analytics.report import render_json, render_markdown, write_reports
from renewal_analytics.scenarios import BUILTIN_SCENARIOS, load_scenario_file, resolve_scenario

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


class ReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        dataset = load_dataset(DATA)
        cls.analysis = analyze_scenario(
            resolve_scenario(dataset, BUILTIN_SCENARIOS["next_year"])
        )

    def test_markdown_contains_traceability_and_reasons(self) -> None:
        text = render_markdown(self.analysis)
        self.assertIn("项目安排与理由", text)
        self.assertIn("未采纳项目及原因", text)
        self.assertIn("依赖与共享施工范围", text)
        self.assertIn("片区受益与均衡", text)
        self.assertIn("projects.csv:2", text)
        self.assertIn("dependencies.csv:2", text)
        self.assertIn("sha256", text)
        self.assertIn("重复开挖", text)
        self.assertIn("施工图预算核定", text)

    def test_json_round_trip_keeps_sources(self) -> None:
        payload = json.loads(render_json(self.analysis))
        placement = payload["plans"]["benefit"]["placements"]["P-02"]
        self.assertEqual(placement["repeat_dig_cost"], 90_000)
        self.assertEqual(placement["line_source"]["file"], "builtin:next_year")
        self.assertEqual(payload["snapshot"]["projects"]["P-02"]["source"]["row"], 3)

    def test_frozen_report_can_be_baseline(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        paths = write_reports(self.analysis, tmp)
        frozen = load_analysis_report(paths["json"])
        later = analyze_scenario(
            resolve_scenario(load_dataset(DATA), BUILTIN_SCENARIOS["budget_cut"])
        ).as_dict()
        comparison = compare_reports(frozen, later)
        self.assertEqual(comparison["to_status"], "INFEASIBLE_COMMITMENT")
        self.assertTrue(any(c["project_id"] == "P-01" for c in comparison["new_conflicts"]))
        text = render_comparison(comparison)
        self.assertIn("受影响承诺", text)
        self.assertIn("退出安排", text)

    def test_budget_cut_vs_next_year(self) -> None:
        dataset = load_dataset(DATA)
        before = analyze_scenario(resolve_scenario(dataset, BUILTIN_SCENARIOS["next_year"])).as_dict()
        after = analyze_scenario(resolve_scenario(dataset, BUILTIN_SCENARIOS["budget_cut"])).as_dict()
        comparison = compare_reports(before, after)
        removed = {row["project_id"] for row in comparison["removed"]}
        self.assertEqual(removed, {"P-01", "P-02"})
        self.assertTrue(comparison["new_conflicts"])


class ScenarioFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _write(self, payload: dict) -> Path:
        path = self.tmp / "scenario.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_custom_scenario_requires_revision_evidence(self) -> None:
        path = self._write(
            {
                "name": "x",
                "cost_basis": "confirmed",
                "cost_revisions": [{"project_id": "P-02", "confirmed_cost": 1}],
            }
        )
        spec, issues = load_scenario_file(path)
        self.assertIsNone(spec)
        self.assertIn("SCENARIO_REVISION_INCOMPLETE", {issue.code for issue in issues})

    def test_custom_scenario_rejects_unknown_cost_basis(self) -> None:
        path = self._write({"name": "x", "cost_basis": "wishful"})
        spec, issues = load_scenario_file(path)
        self.assertIsNone(spec)
        self.assertIn("SCENARIO_COST_BASIS", {issue.code for issue in issues})

    def test_custom_scenario_runs(self) -> None:
        path = self._write(
            {
                "name": "custom",
                "cost_basis": "confirmed",
                "strategies": ["benefit", "cost"],
                "approved_strategy": "benefit",
                "cost_revisions": [
                    {
                        "project_id": "P-02",
                        "confirmed_cost": 300000,
                        "as_of": "2027-01-10",
                        "reason": "复核",
                        "source": "review/P-02",
                    }
                ],
                "funding_additions": [
                    {
                        "fund_id": "F-09",
                        "year": 2028,
                        "category": "drainage",
                        "cap": 300000,
                        "allowed_work": "street",
                    }
                ],
            }
        )
        spec, issues = load_scenario_file(path)
        self.assertIsNotNone(spec)
        self.assertEqual([issue.level for issue in issues], [])
        analysis = analyze_scenario(resolve_scenario(load_dataset(DATA), spec))
        # P-02 now has a confirmed cost but still lacks a prior-year funding
        # line, so the dependency rule keeps it out.
        self.assertIn("P-02", {pid for pid in analysis.approved.rejections})
        self.assertEqual(
            analysis.approved.rejections["P-02"].code, "PREDECESSOR_NOT_COMPLETED"
        )


class CliTests(unittest.TestCase):
    def test_validate_exits_zero_on_baseline(self) -> None:
        self.assertEqual(main(["validate", "--data", str(DATA)]), 0)

    def test_run_exits_zero_on_executable(self) -> None:
        self.assertEqual(
            main(["run", "--data", str(DATA), "--scenario", "confirmed_costs", "--emit", "json"]),
            0,
        )

    def test_run_exits_two_on_conflict(self) -> None:
        self.assertEqual(
            main(["run", "--data", str(DATA), "--scenario", "budget_cut", "--emit", "json"]),
            2,
        )

    def test_compare_exits_zero_when_both_executable(self) -> None:
        self.assertEqual(
            main(
                [
                    "compare",
                    "--data",
                    str(DATA),
                    "--before",
                    "confirmed_costs",
                    "--after",
                    "next_year",
                    "--emit",
                    "json",
                ]
            ),
            0,
        )

    def test_run_writes_reports(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        code = main(
            [
                "run",
                "--data",
                str(DATA),
                "--scenario",
                "next_year",
                "--out",
                str(tmp),
                "--emit",
                "json",
            ]
        )
        self.assertEqual(code, 0)
        self.assertTrue((tmp / "next_year.md").is_file())
        self.assertTrue((tmp / "next_year.json").is_file())


if __name__ == "__main__":
    unittest.main()
