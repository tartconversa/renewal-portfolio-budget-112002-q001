"""Tests for portfolio rules: budget, funding isolation, dependencies, site sharing."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from renewal_analytics.io import load_dataset
from renewal_analytics.model import ScenarioSpec
from renewal_analytics.planner import analyze_scenario
from renewal_analytics.scenarios import BUILTIN_SCENARIOS, resolve_scenario

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


class ScenarioTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dataset = load_dataset(DATA)

    def analyse(self, name: str):
        return analyze_scenario(resolve_scenario(self.dataset, BUILTIN_SCENARIOS[name]))

    def test_baseline_blocks_successor_without_prior_year(self) -> None:
        analysis = self.analyse("confirmed_costs")
        plan = analysis.approved
        self.assertIn("P-01", plan.placements)
        self.assertIn("P-03", plan.placements)
        self.assertNotIn("P-02", plan.placements)
        self.assertEqual(plan.rejections["P-02"].code, "PREDECESSOR_NOT_COMPLETED")
        blocked = [d for d in plan.dependencies if d.successor == "P-02"]
        self.assertTrue(blocked)
        self.assertTrue(all(d.status != "SATISFIED" for d in blocked))

    def test_funding_source_restricts_work_type(self) -> None:
        analysis = self.analyse("confirmed_costs")
        for placement in analysis.approved.placements.values():
            self.assertIn(placement.line.fund_id, {"F-01", "F-02"})
            self.assertIn(analysis.snapshot["projects"][placement.project_id]["work_type"], placement.line.allowed_work)
        # P-03 is service work: it can only use the community line.
        self.assertEqual(analysis.approved.placements["P-03"].fund_id, "F-02")

    def test_budget_caps_are_respected(self) -> None:
        analysis = self.analyse("confirmed_costs")
        for row in analysis.approved.line_usage:
            self.assertLessEqual(row["used"], row["cap"])
            self.assertFalse(row["over_budget"])

    def test_cross_year_shared_site_charges_repeat_dig(self) -> None:
        analysis = self.analyse("next_year")
        placement = analysis.approved.placements["P-02"]
        self.assertEqual(placement.year, 2028)
        self.assertEqual(placement.repeat_dig_cost, 90_000)
        self.assertEqual(placement.total_cost, 380_000 + 90_000)
        row = next(r for r in analysis.approved.line_usage if r["fund_id"] == "F-04")
        self.assertEqual(row["used"], 470_000)

    def test_cost_revision_keeps_original_basis(self) -> None:
        analysis = self.analyse("next_year")
        entry = next(
            item for item in analysis.revision_log if item.get("project_id") == "P-02"
        )
        self.assertEqual(entry["previous"]["estimate"], 310_000)
        self.assertIsNone(entry["previous"]["confirmed_cost"])
        self.assertEqual(entry["updated"]["confirmed_cost"], 380_000)
        self.assertEqual(entry["evidence"], "cost-review-2027-02/P-02")
        self.assertEqual(entry["as_of"], "2027-02-15")
        self.assertEqual(entry["preserved"]["estimate"], 310_000)

    def test_upstream_dataset_is_not_mutated(self) -> None:
        before = {pid: (record.estimate, record.confirmed_cost) for pid, record in self.dataset.projects.items()}
        self.analyse("next_year")
        after = {pid: (record.estimate, record.confirmed_cost) for pid, record in self.dataset.projects.items()}
        self.assertEqual(before, after)

    def test_contracted_project_conflict_is_explicit(self) -> None:
        analysis = self.analyse("budget_cut")
        self.assertEqual(analysis.status, "INFEASIBLE_COMMITMENT")
        self.assertFalse(analysis.executable)
        conflict = next(
            c for c in analysis.approved.conflicts if c["project_id"] == "P-01"
        )
        self.assertEqual(conflict["code"], "CONTRACTED_PROJECT_UNSCHEDULED")
        self.assertEqual(conflict["cost"], 520_000)
        self.assertEqual(conflict["best_available_cap"], 450_000)
        self.assertEqual(conflict["shortfall"], 70_000)
        self.assertIn("P-01", analysis.approved.totals["unplaced_contracted"])

    def test_same_year_sequence_requires_explicit_assumption(self) -> None:
        analysis = self.analyse("same_year_sequence")
        plan = analysis.approved
        # The assumption is explicit, so the result is provisional rather than
        # a silently executable plan.
        self.assertEqual(analysis.status, "PROVISIONAL")
        self.assertIn("P-02", plan.placements)
        self.assertEqual(plan.placements["P-02"].year, plan.placements["P-01"].year)
        status = next(d for d in plan.dependencies if d.successor == "P-02" and d.predecessor == "P-01")
        self.assertEqual(status.status, "SATISFIED_SAME_YEAR")

    def test_estimates_scenario_is_provisional(self) -> None:
        analysis = self.analyse("estimates")
        self.assertEqual(analysis.status, "PROVISIONAL")
        self.assertFalse(analysis.executable)
        # P-01 and P-03 are selected on estimate basis but have a confirmed value
        # that differs, so their cost is not final.
        self.assertEqual(
            sorted(analysis.approved.totals["unconfirmed_cost_projects"]),
            ["P-01", "P-03"],
        )

    def test_cost_bases_track_their_figure(self) -> None:
        analysis = self.analyse("upper_bound")
        placement = analysis.approved.placements["P-01"]
        self.assertEqual(placement.work_cost, 520_000)  # max(480000, 520000)


class SolverBalanceTests(unittest.TestCase):
    """Fixtures where package size changes who gets served; the solver must
    optimise confirmed household coverage, and the report must flag a district
    left out while another receives several packages."""

    def _dataset(
        self,
        projects: str,
        needs: str,
        cap: int,
        *,
        dependencies: str = "predecessor,successor,shared_site,repeat_dig_cost\n",
        allowed_work: str = "service",
    ):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        data = tmp / "data"
        data.mkdir()
        (data / "projects.csv").write_text(
            "project_id,district,work_type,estimate,confirmed_cost,year,contracted\n"
            + projects,
            encoding="utf-8",
        )
        (data / "funding.csv").write_text(
            "fund_id,year,category,cap,allowed_work\n"
            f"F-A,2027,community,{cap},{allowed_work}\n",
            encoding="utf-8",
        )
        (data / "dependencies.csv").write_text(dependencies, encoding="utf-8")
        (data / "needs.csv").write_text(
            "district,group,need_code,affected_households,confirmed\n" + needs,
            encoding="utf-8",
        )
        return load_dataset(data)

    def test_benefit_prefers_more_confirmed_households(self) -> None:
        dataset = self._dataset(
            "A-1,Alpha,service,700000,700000,2027,false\n"
            "B-1,Beta,service,700000,700000,2027,false\n",
            "Alpha,older,step_free,100,true\n"
            "Beta,older,step_free,500,true\n",
            700_000,
        )
        plan = analyze_scenario(
            resolve_scenario(dataset, BUILTIN_SCENARIOS["confirmed_costs"])
        ).approved
        self.assertEqual(list(plan.placements), ["B-1"])
        self.assertEqual(plan.totals["covered_districts"], ["Beta"])
        self.assertEqual(plan.totals["covered_households"], 500)

    def test_split_packages_and_uncovered_district_are_flagged(self) -> None:
        # Alpha needs a pipe+street pair (2 packages) to complete the chain and
        # also has more confirmed households; Beta is a single project.  With a
        # 600k cap only Alpha fits, so Beta is left out while Alpha receives two
        # packages: exactly the split-package crowding risk the tool must flag.
        dataset = self._dataset(
            "A-1,Alpha,pipe,300000,300000,2027,false\n"
            "A-2,Alpha,street,250000,250000,2027,false\n"
            "B-1,Beta,service,500000,500000,2027,false\n",
            "Alpha,older,step_free,700,true\n"
            "Beta,older,step_free,500,true\n",
            600_000,
            dependencies="predecessor,successor,shared_site,repeat_dig_cost\n"
            "A-1,A-2,true,40000\n",
            allowed_work="pipe;street;service",
        )
        spec = ScenarioSpec(
            name="split_check",
            description="允许同年度接续，检查小包挤占",
            cost_basis="confirmed",
            strategies=("benefit",),
            approved_strategy="benefit",
            allow_same_year_predecessor=True,
        )
        analysis = analyze_scenario(resolve_scenario(dataset, spec))
        plan = analysis.approved
        self.assertEqual(sorted(plan.placements), ["A-1", "A-2"])
        self.assertNotIn("B-1", plan.placements)
        flags = {flag["code"]: flag for flag in plan.totals["balance_flags"]}
        self.assertIn("UNCOVERED_WHILE_OTHERS_MULTI_PACKAGE", flags)
        flag = flags["UNCOVERED_WHILE_OTHERS_MULTI_PACKAGE"]
        self.assertIn("Beta", flag["districts"])
        self.assertIn("Alpha", flag["districts"])

    def test_budget_cap_is_never_exceeded(self) -> None:
        dataset = self._dataset(
            "A-1,Alpha,service,600000,600000,2027,false\n"
            "B-1,Beta,service,600000,600000,2027,false\n",
            "Alpha,older,step_free,100,true\n"
            "Beta,older,step_free,500,true\n",
            700_000,
        )
        plan = analyze_scenario(
            resolve_scenario(dataset, BUILTIN_SCENARIOS["confirmed_costs"])
        ).approved
        for row in plan.line_usage:
            self.assertLessEqual(row["used"], row["cap"])
        self.assertLessEqual(plan.totals["total_cost"], 700_000)

if __name__ == "__main__":
    unittest.main()
