"""Cross-checks the solver against an independent brute-force enumerator.

The search prunes branches whose optimistic bound cannot beat the incumbent, so
these tests matter: a bug there would silently drop a better plan.  Each random
instance is solved with two funding lines and dependencies, then enumerated
exhaustively; the primary objectives (contracted projects, confirmed coverage,
dependency completion) must match exactly.
"""

from __future__ import annotations

import itertools
import random
import shutil
import tempfile
import unittest
from pathlib import Path

from renewal_analytics.io import load_dataset
from renewal_analytics.model import ScenarioSpec
from renewal_analytics.planner import _Solver, analyze_scenario
from renewal_analytics.scenarios import BUILTIN_SCENARIOS, resolve_scenario

DISTRICTS = ("A", "B", "C")
WORKS = ("pipe", "street", "service")
COSTS = (100_000, 150_000, 200_000, 250_000)


def build_instance(tmp: Path, rng: random.Random, allow_same_year: bool = False):
    data = tmp / "data"
    data.mkdir()
    count = rng.randint(3, 7)
    rows = ["project_id,district,work_type,estimate,confirmed_cost,year,contracted"]
    for index in range(count):
        cost = rng.choice(COSTS)
        rows.append(
            f"P{index},{rng.choice(DISTRICTS)},{rng.choice(WORKS)},"
            f"{cost},{cost},2027,{str(rng.random() < 0.25).lower()}"
        )
    (data / "projects.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    (data / "funding.csv").write_text(
        "fund_id,year,category,cap,allowed_work\n"
        f"F1,2027,x,{rng.choice([300_000, 450_000, 600_000])},pipe;street;service\n"
        f"F2,2027,y,{rng.choice([200_000, 350_000, 500_000])},pipe;street;service\n",
        encoding="utf-8",
    )
    deps = ["predecessor,successor,shared_site,repeat_dig_cost"]
    if count >= 3 and rng.random() < 0.6:
        # pick an ordered pair; ordering keeps the graph acyclic
        a, b = sorted(rng.sample(range(count), 2))
        shared = rng.random() < 0.5
        deps.append(f"P{a},P{b},{str(shared).lower()},{rng.choice([0, 30000]) if shared else 0}")
    (data / "dependencies.csv").write_text("\n".join(deps) + "\n", encoding="utf-8")
    (data / "needs.csv").write_text(
        "district,group,need_code,affected_households,confirmed\n"
        "A,older,step_free,100,true\n"
        "B,older,step_free,200,true\n"
        "C,older,step_free,50,true\n",
        encoding="utf-8",
    )
    spec = ScenarioSpec(
        name="check",
        description="brute-force cross-check",
        cost_basis="confirmed",
        strategies=("benefit",),
        approved_strategy="benefit",
        allow_same_year_predecessor=allow_same_year,
    )
    return resolve_scenario(load_dataset(data), spec)


def brute_force(data) -> tuple[int, ...]:
    solver = _Solver(data, "benefit", 10_000_000, 120)
    ids = data.dataset.project_order
    best: tuple[int, ...] | None = None
    # Every project may be placed on one compatible line *or* left out.
    options = [
        [*solver._candidate_lines(data.projects[pid]), None] for pid in ids
    ]
    for choice in itertools.product(*options):
        assignment = dict(zip(ids, choice))
        used: dict[int, int] = {}
        cost = 0
        feasible = True
        for pid, line_index in assignment.items():
            if line_index is None:
                continue
            item = solver.costs[pid].amount
            used[line_index] = used.get(line_index, 0) + item
            cost += item
            if used[line_index] > solver.lines[line_index].cap:
                feasible = False
                break
            # dependencies
            for dep in solver.dependencies.get(pid, ()):
                pred_line = assignment.get(dep.predecessor)
                if pred_line is None:
                    feasible = False
                    break
                pred_year = solver.lines[pred_line].year
                year = solver.lines[line_index].year
                if data.spec.allow_same_year_predecessor:
                    if pred_year > year:
                        feasible = False
                elif pred_year >= year:
                    feasible = False
            if not feasible:
                break
        if not feasible:
            continue
        selected = [pid for pid, line in assignment.items() if line is not None]
        covered = {data.projects[pid].base.district for pid in selected}
        households = sum(
            solver.demand[d].households for d in covered if d in solver.demand
        )
        honored = 0
        for dep in data.dataset.dependencies:
            if (
                assignment.get(dep.predecessor) is not None
                and assignment.get(dep.successor) is not None
            ):
                pred_year = solver.lines[assignment[dep.predecessor]].year
                succ_year = solver.lines[assignment[dep.successor]].year
                if pred_year < succ_year or (
                    data.spec.allow_same_year_predecessor and pred_year == succ_year
                ):
                    honored += 1
        key = (
            sum(1 for pid in selected if pid in solver.forced),
            households,
            honored,
            -cost,
        )
        if best is None or key > best:
            best = key
    return best or ()


class BruteForceComparisonTests(unittest.TestCase):
    def test_random_instances_match_brute_force(self) -> None:
        rng = random.Random(20261123)
        mismatches = []
        checked = 0
        for trial in range(40):
            tmp = Path(tempfile.mkdtemp())
            try:
                data = build_instance(tmp, rng, allow_same_year=trial % 2 == 0)
                if data.errors:
                    continue
                solver = _Solver(data, "benefit", 10_000_000, 120)
                _, objective, incomplete, _nodes = solver.solve()
                expected = brute_force(data)
                self.assertFalse(incomplete)
                checked += 1
                if objective[:-1] != expected[:-1]:
                    mismatches.append((trial, objective, expected))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        self.assertGreaterEqual(checked, 25)
        self.assertEqual(mismatches, [])

    def test_dependency_blocking_holds(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        rng = random.Random(5)
        data = build_instance(tmp, rng)
        analysis = analyze_scenario(data)
        plan = analysis.approved
        for status in plan.dependencies:
            if status.status == "BLOCKED":
                self.fail("a selected successor must never have an unscheduled predecessor")
            if status.successor_year is not None and status.predecessor_year is not None:
                self.assertLessEqual(status.predecessor_year, status.successor_year)


if __name__ == "__main__":
    unittest.main()
