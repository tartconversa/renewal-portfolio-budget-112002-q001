"""Portfolio solver.

The solver assigns each project to at most one funding line, never exceeding a
line's annual cap, honouring predecessor timing and charging the extra digging
cost when shared-site works are split across years.

It is a branch-and-bound search whose *primary* objectives (contracted projects
placed, confirmed households covered, dependency links completed) are solved
exactly, and cross-checked in tests against a brute-force enumerator.  Cost is
only a deterministic preference among plans with equal primary objectives, so
the tool does not claim a global cost minimum.  If the node or time budget is
exhausted the result is marked ``SEARCH_INCOMPLETE`` and must not be treated as
a final executable plan.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from .model import (
    CostFigure,
    Dependency,
    EffectiveProject,
    FundingLine,
    Issue,
    NeedRecord,
    ScenarioData,
)
from .scenarios import cost_figure

STRATEGY_LABELS = {
    "benefit": "片区已确认受益最大化",
    "cost": "项目数量最大化",
    "district": "覆盖片区数量最大化",
    "sequence": "依赖链完整度最大化",
}

STATUS_LABELS = {
    "EXECUTABLE": "可执行",
    "PROVISIONAL": "暂定：存在未核定成本、需求口径未知或行政假设",
    "INFEASIBLE_COMMITMENT": "冲突：已签约项目无法全部安排",
    "SEARCH_INCOMPLETE": "未完成：搜索达到节点上限",
    "INVALID_INPUT": "不可执行：输入存在错误",
}


@dataclass(frozen=True)
class Placement:
    project_id: str
    year: int
    fund_id: str
    line_index: int
    line: FundingLine
    work_cost: int
    repeat_dig_cost: int
    cost: CostFigure
    reason: str = ""

    @property
    def total_cost(self) -> int:
        return self.work_cost + self.repeat_dig_cost

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "year": self.year,
            "fund_id": self.fund_id,
            "fund_category": self.line.category,
            "line_year": self.line.year,
            "line_source": self.line.source.as_dict(),
            "work_cost": self.work_cost,
            "repeat_dig_cost": self.repeat_dig_cost,
            "total_cost": self.total_cost,
            "cost_basis": self.cost.basis,
            "cost_figure": self.cost.as_dict(),
            "reason": self.reason,
        }


@dataclass
class DistrictDemand:
    district: str
    households: int
    needs: tuple[NeedRecord, ...]
    known: bool
    reason: str


@dataclass
class Rejection:
    code: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}


@dataclass
class DependencyStatus:
    predecessor: str
    successor: str
    shared_site: bool
    repeat_dig_cost: int | None
    source: dict[str, Any]
    status: str
    detail: str
    predecessor_year: int | None
    successor_year: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "predecessor": self.predecessor,
            "successor": self.successor,
            "shared_site": self.shared_site,
            "repeat_dig_cost": self.repeat_dig_cost,
            "source": self.source,
            "status": self.status,
            "detail": self.detail,
            "predecessor_year": self.predecessor_year,
            "successor_year": self.successor_year,
        }


@dataclass
class StrategyPlan:
    strategy: str
    status: str
    placements: dict[str, Placement]
    rejections: dict[str, Rejection]
    dependencies: list[DependencyStatus]
    districts: list[dict[str, Any]]
    line_usage: list[dict[str, Any]]
    totals: dict[str, Any]
    objective: tuple[int, ...]
    conflicts: list[dict[str, Any]]
    notes: list[str]
    search: dict[str, Any]
    warnings: list[Issue]

    def as_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "strategy_label": STRATEGY_LABELS.get(self.strategy, self.strategy),
            "status": self.status,
            "status_label": STATUS_LABELS.get(self.status, self.status),
            "objective": list(self.objective),
            "totals": self.totals,
            "placements": {
                pid: placement.as_dict() for pid, placement in self.placements.items()
            },
            "rejections": {
                pid: rejection.as_dict() for pid, rejection in self.rejections.items()
            },
            "dependencies": [status.as_dict() for status in self.dependencies],
            "districts": self.districts,
            "funding": self.line_usage,
            "conflicts": self.conflicts,
            "notes": self.notes,
            "search": self.search,
            "warnings": [issue.as_dict() for issue in self.warnings],
        }


@dataclass
class ScenarioAnalysis:
    scenario: str
    status: str
    validation: dict[str, Any]
    plans: dict[str, StrategyPlan]
    approved_strategy: str
    approved: StrategyPlan | None
    comparison: dict[str, Any]
    revision_log: list[dict[str, Any]]
    snapshot: dict[str, Any]

    @property
    def executable(self) -> bool:
        return self.approved is not None and self.approved.status == "EXECUTABLE"

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "status": self.status,
            "validation": self.validation,
            "approved_strategy": self.approved_strategy,
            "approved_status": self.approved.status if self.approved else None,
            "executable": self.executable,
            "plans": {name: plan.as_dict() for name, plan in self.plans.items()},
            "comparison": self.comparison,
            "revision_log": self.revision_log,
            "snapshot": self.snapshot,
        }


# ---------------------------------------------------------------------------
# Demand and dependency helpers


def build_demand(data: ScenarioData) -> dict[str, DistrictDemand]:
    grouped: dict[str, list[NeedRecord]] = {}
    for need in data.dataset.needs:
        grouped.setdefault(need.district, []).append(need)
    demand: dict[str, DistrictDemand] = {}
    for project in data.projects.values():
        grouped.setdefault(project.base.district, grouped.get(project.base.district, []))
    for district, needs in grouped.items():
        confirmed = [need for need in needs if need.confirmed is True]
        if not confirmed:
            demand[district] = DistrictDemand(
                district=district,
                households=0,
                needs=tuple(needs),
                known=False,
                reason="no_confirmed_need",
            )
            continue
        households = sum(need.affected_households or 0 for need in confirmed)
        demand[district] = DistrictDemand(
            district=district,
            households=households,
            needs=tuple(needs),
            known=True,
            reason="ok",
        )
    return demand


def _dependency_index(dependencies: Iterable[Dependency]) -> dict[str, list[Dependency]]:
    index: dict[str, list[Dependency]] = {}
    for dep in dependencies:
        index.setdefault(dep.successor, []).append(dep)
    for edges in index.values():
        edges.sort(key=lambda item: (item.predecessor, item.source.row))
    return index


def _topological_order(data: ScenarioData) -> list[str]:
    """Kahn's algorithm with a deterministic priority.

    Projects are grouped by district (so coverage decisions complete early) and
    the cheapest project in a district is tried first, while predecessors are
    always emitted before their successors.
    """
    predecessors: dict[str, set[str]] = {pid: set() for pid in data.projects}
    successors: dict[str, set[str]] = {pid: set() for pid in data.projects}
    for dep in data.dataset.dependencies:
        if dep.predecessor in data.projects and dep.successor in data.projects:
            predecessors[dep.successor].add(dep.predecessor)
            successors[dep.predecessor].add(dep.successor)
    position = {pid: index for index, pid in enumerate(data.dataset.project_order)}
    cost = {
        pid: cost_figure(project, data.spec.cost_basis).amount
        for pid, project in data.projects.items()
    }
    key = lambda pid: (
        data.projects[pid].base.district,
        cost[pid],
        position[pid],
    )
    order: list[str] = []
    ready = sorted(
        (pid for pid in data.projects if not predecessors[pid]), key=key
    )
    placed: set[str] = set()
    while ready:
        pid = ready.pop(0)
        order.append(pid)
        placed.add(pid)
        for nxt in successors[pid]:
            predecessors[nxt].discard(pid)
            if not predecessors[nxt] and nxt not in placed and nxt not in ready:
                ready.append(nxt)
        ready.sort(key=key)
    if len(order) != len(data.projects):
        raise ValueError("dependency cycle survived validation")
    return order


class _Solver:
    def __init__(
        self,
        data: ScenarioData,
        strategy: str,
        node_limit: int,
        time_limit_seconds: float = 2.5,
    ) -> None:
        self.data = data
        self.strategy = strategy
        self.node_limit = node_limit
        self.time_limit_seconds = time_limit_seconds
        self.deadline = time.monotonic() + time_limit_seconds
        self.demand = build_demand(data)
        self.dependencies = _dependency_index(data.dataset.dependencies)
        self.order = _topological_order(data)
        self.costs: dict[str, CostFigure] = {
            pid: cost_figure(project, data.spec.cost_basis)
            for pid, project in data.projects.items()
        }
        self.forced = {
            pid for pid, project in data.projects.items() if project.base.contracted
        }
        self.dependency_projects = {
            pid
            for dep in data.dataset.dependencies
            for pid in (dep.predecessor, dep.successor)
        }
        self.lines = data.funding
        self.remaining: list[int] = [line.cap for line in self.lines]
        self.decisions: dict[str, Placement | None] = {}
        self.best: tuple[tuple[int, ...], dict[str, Placement | None]] | None = None
        self.best_signature: tuple[tuple[str, int, str], ...] | None = None
        self.nodes = 0
        self.incomplete = False

    # -- placement ---------------------------------------------------------
    def _candidate_lines(self, project: EffectiveProject) -> list[int]:
        """Compatible funding lines, one representative per (year, category).

        Lines with the same year, category and allowed work are interchangeable
        for this project; keeping a single representative (the one with the
        most remaining capacity) removes symmetric branches without changing
        which projects can be funded.  Lines from different categories are
        never merged: their use restrictions are not interchangeable.
        """
        cost = self.costs[project.project_id].amount
        groups: dict[tuple[int, str, tuple[str, ...], int], list[int]] = {}
        for index, line in enumerate(self.lines):
            if line.year < project.base.year:
                continue
            if project.base.work_type not in line.allowed_work:
                continue
            if cost > line.cap:
                continue
            # Only *identical* lines are collapsed: same year, category,
            # allowed work and cap.  Any assignment between them is symmetric,
            # so keeping one representative cannot remove a feasible plan.
            groups.setdefault(
                (line.year, line.category, line.allowed_work, line.cap), []
            ).append(index)
        chosen = [min(options) for options in groups.values()]
        chosen.sort(key=lambda index: (self.lines[index].year, self.lines[index].fund_id))
        return chosen

    def _repeat_cost(self, pid: str, year: int) -> tuple[int, dict[str, int]]:
        extra = 0
        breakdown: dict[str, int] = {}
        for dep in self.dependencies.get(pid, ()):
            pred_decision = self.decisions.get(dep.predecessor)
            if pred_decision is None:
                continue
            if not dep.shared_site:
                continue
            if dep.predecessor is not None and pred_decision.year != year:
                amount = dep.repeat_dig_cost or 0
                if amount:
                    extra += amount
                    breakdown[dep.predecessor] = amount
        return extra, breakdown

    def _option_feasible(self, pid: str, line_index: int) -> tuple[bool, str, int, dict[str, int]]:
        project = self.data.projects[pid]
        year = self.lines[line_index].year
        for dep in self.dependencies.get(pid, ()):
            pred_decision = self.decisions.get(dep.predecessor)
            if pred_decision is None:
                return False, "PREDECESSOR_UNSCHEDULED", 0, {}
            if self.data.spec.allow_same_year_predecessor:
                if pred_decision.year > year:
                    return False, "PREDECESSOR_TIMING", 0, {}
            else:
                if pred_decision.year >= year:
                    return False, "PREDECESSOR_NOT_COMPLETED", 0, {}
        extra, breakdown = self._repeat_cost(pid, year)
        total = self.costs[pid].amount + extra
        if total > self.remaining[line_index]:
            return False, "LINE_CAPACITY", total, breakdown
        return True, "OK", total, breakdown

    def _metric(self) -> tuple[int, ...]:
        selected = [
            pid for pid, decision in self.decisions.items() if decision is not None
        ]
        forced_placed = sum(1 for pid in selected if pid in self.forced)
        covered: set[str] = {
            self.data.projects[pid].base.district for pid in selected
        }
        covered_households = sum(
            self.demand[d].households
            for d in covered
            if d in self.demand and self.demand[d].known
        )
        total_cost = sum(
            decision.total_cost
            for decision in self.decisions.values()
            if decision is not None
        )
        honored = 0
        for dep in self.data.dataset.dependencies:
            pred = self.decisions.get(dep.predecessor)
            succ = self.decisions.get(dep.successor)
            if pred is not None and succ is not None:
                if self.data.spec.allow_same_year_predecessor:
                    if pred.year <= succ.year:
                        honored += 1
                elif pred.year < succ.year:
                    honored += 1
        if self.strategy == "benefit":
            # Coverage first; among equal coverage, finish dependency chains
            # (e.g. street restoration after pipe works); cost breaks ties only
            # through the deterministic visit order.
            return (forced_placed, covered_households, honored, -total_cost)
        if self.strategy == "cost":
            return (forced_placed, len(selected), covered_households, honored, -total_cost)
        if self.strategy == "district":
            return (
                forced_placed,
                len(covered),
                covered_households,
                honored,
                -total_cost,
            )
        return (forced_placed, honored, len(selected), covered_households, -total_cost)

    @staticmethod
    def _primary_prefix(metric: tuple[int, ...]) -> tuple[int, ...]:
        """Everything except the trailing cost element.

        Primary objectives are optimised exactly; cost is a deterministic
        preference among equally good plans, so an equal primary prefix is
        treated as "cannot improve" and pruned.  This keeps the search exact
        for what the committee decides on, without pretending to prove a
        global cost minimum.
        """
        return metric[:-1] if metric else metric

    def _optimistic_households(self, index: int, covered: set[str]) -> int:
        """Relax each remaining district to its cheapest cover, and relax all
        funding lines into one budget.  Never under-estimates what is reachable,
        so it is a safe branch-and-bound bound."""
        budget = sum(self.remaining)
        cheapest: dict[str, int] = {}
        for pid in self.order[index:]:
            project = self.data.projects[pid]
            district = project.base.district
            if district in covered:
                continue
            if not any(
                project.base.work_type in line.allowed_work
                for line in self.lines
            ):
                continue
            cost = self.costs[pid].amount
            if district not in cheapest or cost < cheapest[district]:
                cheapest[district] = cost
        options = [
            (self.demand[district].households, cost)
            for district, cost in cheapest.items()
            if district in self.demand and self.demand[district].known
        ]
        options.sort(key=lambda item: item[1] / max(item[0], 1))
        total = 0
        spent = 0
        for households, cost in options:
            if spent + cost > budget:
                continue
            spent += cost
            total += households
        return total

    def _count_upper_bound(self, index: int, covered: set[str]) -> int:
        """Maximum number of additional projects that could still fit, using a
        single pooled budget.  Valid because pooling only relaxes constraints."""
        budget = sum(self.remaining)
        costs = sorted(
            self.costs[pid].amount
            for pid in self.order[index:]
            if any(
                self.data.projects[pid].base.work_type in line.allowed_work
                for line in self.lines
            )
        )
        count = 0
        for cost in costs:
            if cost > budget:
                break
            budget -= cost
            count += 1
        return count

    def _cost_lower_bound(self, index: int, covered: set[str]) -> int:
        """Lower bound on remaining spend: every unplaced contract must be
        paid for, and every not-yet-covered district needs at least its
        cheapest servable project.  Taking the max of the two is valid."""
        forced_cost = sum(
            self.costs[pid].amount
            for pid in self.order[index:]
            if pid in self.forced
        )
        per_district: dict[str, int] = {}
        for pid in self.order[index:]:
            project = self.data.projects[pid]
            district = project.base.district
            if district in covered:
                continue
            if not any(
                project.base.work_type in line.allowed_work
                for line in self.lines
            ):
                continue
            cost = self.costs[pid].amount
            if district not in per_district or cost < per_district[district]:
                per_district[district] = cost
        district_cost = sum(per_district.values())
        return max(forced_cost, district_cost)

    def _bound(self, index: int, covered: set[str], spent: int) -> tuple[int, ...]:
        remaining = self.order[index:]
        covered_now = sum(
            self.demand[district].households
            for district in covered
            if district in self.demand and self.demand[district].known
        )
        households = covered_now + self._optimistic_households(index, covered)
        forced_possible = sum(1 for pid in remaining if pid in self.forced)
        forced_now = sum(
            1
            for pid, decision in self.decisions.items()
            if decision is not None and pid in self.forced
        )
        edges_possible = len(self.data.dataset.dependencies)
        if self.strategy == "benefit":
            return (
                forced_now + forced_possible,
                households,
                edges_possible,
                -(spent + self._cost_lower_bound(index, covered)),
            )
        if self.strategy == "cost":
            return (
                forced_now + forced_possible,
                self._count_upper_bound(index, covered),
                households,
                edges_possible,
                -(spent + self._cost_lower_bound(index, covered)),
            )
        if self.strategy == "district":
            districts_remaining = {
                self.data.projects[pid].base.district for pid in remaining
            } - covered
            return (
                forced_now + forced_possible,
                len(covered) + len(districts_remaining),
                households,
                edges_possible,
                -(spent + self._cost_lower_bound(index, covered)),
            )
        return (
            forced_now + forced_possible,
            edges_possible,
            len(remaining),
            households,
            -(spent + self._cost_lower_bound(index, covered)),
        )

    def _signature(self) -> tuple[tuple[str, int, str], ...]:
        rows = []
        for pid in self.data.dataset.project_order:
            decision = self.decisions.get(pid)
            if decision is None:
                rows.append((pid, -1, ""))
            else:
                rows.append((pid, decision.year, decision.fund_id))
        return tuple(rows)

    def _record_best(self) -> None:
        self.nodes += 1
        if self.nodes > self.node_limit or time.monotonic() > self.deadline:
            self.incomplete = True
            return
        metric = self._metric()
        signature = self._signature()
        if self.best is None or metric > self.best[0]:
            self.best = (metric, dict(self.decisions))
            self.best_signature = signature
        elif metric == self.best[0] and (self.best_signature is None or signature < self.best_signature):
            self.best = (metric, dict(self.decisions))
            self.best_signature = signature

    def _dfs(self, index: int, covered: set[str], spent: int) -> None:
        if self.incomplete:
            return
        if self.nodes > self.node_limit or time.monotonic() > self.deadline:
            self.incomplete = True
            return
        if index == len(self.order):
            self._record_best()
            return
        if self.best is not None:
            bound = self._bound(index, covered, spent)
            if self._primary_prefix(bound) <= self._primary_prefix(self.best[0]):
                # Even an optimistic relaxation cannot beat the incumbent on the
                # primary objectives (contracted projects, coverage, dependency
                # completion).  Equal-primary plans are pruned too: cost is a
                # preference, not an objective the tool claims to minimise.
                return
        pid = self.order[index]
        project = self.data.projects[pid]
        candidates = self._candidate_lines(project)
        options: list[int] = []
        for line_index in candidates:
            if self._option_feasible(pid, line_index)[0]:
                options.append(line_index)

        if (
            self.strategy != "cost"
            and project.base.district in covered
            and pid not in self.forced
            and pid not in self.dependency_projects
        ):
            # The district is already covered and this project adds neither a
            # contract nor a dependency link.  Selecting it can only spend money,
            # so this branch is dominated by leaving it out.
            options = []

        # try placements first; deterministic order by year then fund id.
        for line_index in options:
            year = self.lines[line_index].year
            _ok, _reason, total, breakdown = self._option_feasible(pid, line_index)
            extra = sum(breakdown.values())
            self.remaining[line_index] -= total
            self.decisions[pid] = Placement(
                project_id=pid,
                year=year,
                fund_id=self.lines[line_index].fund_id,
                line_index=line_index,
                line=self.lines[line_index],
                work_cost=self.costs[pid].amount,
                repeat_dig_cost=extra,
                cost=self.costs[pid],
            )
            district = self.data.projects[pid].base.district
            new_covered = covered if district in covered else covered | {district}
            self._dfs(index + 1, new_covered, spent + total)
            self.decisions.pop(pid, None)
            self.remaining[line_index] += total
            if self.incomplete:
                return

        # leave the project out; contracted projects may still be left out when
        # infeasible, but the objective penalises that so it is a last resort.
        self.decisions[pid] = None
        self._dfs(index + 1, covered, spent)
        self.decisions.pop(pid, None)

    def _seed_incumbent(self) -> None:
        """Build a feasible plan greedily so branch-and-bound starts with a
        realistic incumbent.  Without one, DFS would first visit the "place
        everything" leaf and then comb through many worse combinations."""
        decisions: dict[str, Placement | None] = {}
        remaining = list(self.remaining)
        for pid in self.order:
            project = self.data.projects[pid]
            best: tuple[int, int] | None = None
            for line_index in self._candidate_lines(project):
                year = self.lines[line_index].year
                feasible = True
                for dep in self.dependencies.get(pid, ()):
                    pred = decisions.get(dep.predecessor)
                    if pred is None:
                        feasible = False
                        break
                    if self.data.spec.allow_same_year_predecessor:
                        if pred.year > year:
                            feasible = False
                            break
                    elif pred.year >= year:
                        feasible = False
                        break
                if not feasible:
                    continue
                extra = 0
                for dep in self.dependencies.get(pid, ()):
                    pred = decisions.get(dep.predecessor)
                    if (
                        pred is not None
                        and dep.shared_site
                        and pred.year != year
                        and dep.repeat_dig_cost
                    ):
                        extra += dep.repeat_dig_cost
                total = self.costs[pid].amount + extra
                if total > remaining[line_index]:
                    continue
                key = (total, line_index)
                if best is None or key < best:
                    best = key
            if best is not None:
                total, line_index = best
                remaining[line_index] -= total
                decisions[pid] = Placement(
                    project_id=pid,
                    year=self.lines[line_index].year,
                    fund_id=self.lines[line_index].fund_id,
                    line_index=line_index,
                    line=self.lines[line_index],
                    work_cost=self.costs[pid].amount,
                    repeat_dig_cost=total - self.costs[pid].amount,
                    cost=self.costs[pid],
                )
            else:
                decisions[pid] = None
        self.decisions = decisions
        self.remaining = remaining
        self.best = (self._metric(), dict(decisions))
        self.best_signature = self._signature()
        self.decisions = {}
        self.remaining = [line.cap for line in self.lines]

    def solve(self) -> tuple[dict[str, Placement | None], tuple[int, ...], bool, int]:
        """Best plan, its objective, whether the search was cut short, and nodes.

        An empty best plan is returned as ``{}`` and the caller treats it as a
        plan that schedules nothing, never as an error - every project then
        receives an explicit rejection reason.
        """
        self._seed_incumbent()
        self._dfs(0, set(), 0)
        if self.best is None:
            return {}, (), True, self.nodes
        return self.best[1], self.best[0], self.incomplete, self.nodes


# ---------------------------------------------------------------------------
# Rejections, dependency status and district balance


def _rejections(
    data: ScenarioData,
    solver: _Solver,
    decisions: dict[str, Placement | None],
) -> dict[str, Rejection]:
    demand = solver.demand
    selected_districts = {
        data.projects[pid].base.district
        for pid, decision in decisions.items()
        if decision is not None
    }
    out: dict[str, Rejection] = {}
    for pid in data.dataset.project_order:
        if decisions.get(pid) is not None:
            continue
        project = data.projects[pid]
        cost = solver.costs[pid]
        candidates = solver._candidate_lines(project)
        detail = {
            "work_type": project.base.work_type,
            "requested_year": project.base.year,
            "cost_basis": cost.basis,
            "cost": cost.amount,
            "unconfirmed_cost": cost.unconfirmed,
            "source": project.base.source.as_dict(),
            "contracted": project.base.contracted,
        }
        if not any(project.base.work_type in line.allowed_work for line in data.funding):
            out[pid] = Rejection(
                "UNFUNDABLE_TYPE",
                f"没有任何资金来源允许 {project.base.work_type} 工程",
                detail,
            )
            continue
        if not candidates:
            matching = [
                line
                for line in data.funding
                if project.base.work_type in line.allowed_work and line.year >= project.base.year
            ]
            if matching and all(cost.amount > line.cap for line in matching):
                detail["lines"] = [line.as_dict() for line in matching]
                out[pid] = Rejection(
                    "COST_EXCEEDS_EACH_LINE",
                    "单项成本已超过所有可用资金单条上限，无法由单一来源承担",
                    detail,
                )
            else:
                out[pid] = Rejection(
                    "NO_LINE_FOR_REQUESTED_YEAR",
                    "项目请求年度及之后没有允许该类工程的资金上限",
                    detail,
                )
            continue
        # dependency cause
        dep_cause: Rejection | None = None
        for dep in solver.dependencies.get(pid, ()):
            pred = decisions.get(dep.predecessor)
            if pred is None:
                dep_cause = Rejection(
                    "PREDECESSOR_UNSCHEDULED",
                    f"前置工程 {dep.predecessor} 未入选，依赖工程不能计入可交付量",
                    {**detail, "predecessor": dep.predecessor, "dependency_source": dep.source.as_dict()},
                )
                break
            if data.spec.allow_same_year_predecessor:
                if pred.year > project.base.year and not any(
                    solver.lines[index].year >= pred.year for index in candidates
                ):
                    dep_cause = Rejection(
                        "PREDECESSOR_TIMING",
                        f"前置工程 {dep.predecessor} 排在 {pred.year} 年，依赖工程没有不早于该年的资金来源",
                        {**detail, "predecessor": dep.predecessor},
                    )
                    break
            elif pred.year >= min(solver.lines[index].year for index in candidates):
                dep_cause = Rejection(
                    "PREDECESSOR_NOT_COMPLETED",
                    f"前置工程 {dep.predecessor} 未能在依赖工程之前完成",
                    {**detail, "predecessor": dep.predecessor},
                )
                break
        if dep_cause is not None:
            out[pid] = dep_cause
            continue
        district = project.base.district
        if project.base.contracted:
            out[pid] = Rejection(
                "CONTRACT_CONFLICT",
                "已签约项目未能排入任何资金上限；承诺受影响",
                {**detail, "district": district},
            )
            continue
        # residual capacity after the approved plan
        residual = []
        for index in candidates:
            line = solver.lines[index]
            used = sum(
                decision.total_cost
                for decision in decisions.values()
                if decision is not None and decision.line_index == index
            )
            residual.append({"fund_id": line.fund_id, "year": line.year, "remaining": line.cap - used})
        detail["residual_capacity"] = residual
        if district in selected_districts and demand.get(district, None) and demand[district].known:
            out[pid] = Rejection(
                "NO_ADDITIONAL_CONFIRMED_BENEFIT",
                "同片区已有项目入选；再加小包不会增加已确认受益，优先保留资金给其他片区",
                detail,
            )
        elif residual and all(item["remaining"] < cost.amount for item in residual):
            out[pid] = Rejection(
                "NO_REMAINING_CAPACITY",
                "入选方案已占满该工程可用的年度资金，剩余额度不足以容纳本项目",
                detail,
            )
        else:
            out[pid] = Rejection(
                "LOWER_PRIORITY",
                "在所选策略的排序中优先级较低；已有更高受益/更少成本的项目占用同一资金",
                detail,
            )
    return out


def _dependency_statuses(
    data: ScenarioData, decisions: dict[str, Placement | None]
) -> list[DependencyStatus]:
    statuses = []
    for dep in data.dataset.dependencies:
        pred = decisions.get(dep.predecessor)
        succ = decisions.get(dep.successor)
        pred_year = pred.year if pred else None
        succ_year = succ.year if succ else None
        if pred is None and succ is None:
            status = "BOTH_UNSCHEDULED"
            detail = "前置与依赖工程均未入选，关系不触发"
        elif pred is None:
            status = "BLOCKED"
            detail = "依赖工程已入选但前置工程未入选；该可交付量不应被计入"
        elif succ is None:
            status = "SUCCESSOR_UNSCHEDULED"
            detail = "前置工程入选，依赖工程延期/未安排"
        elif data.spec.allow_same_year_predecessor and pred.year == succ.year:
            status = "SATISFIED_SAME_YEAR"
            detail = "同年度内按前置先施工、依赖后接续；若无法保证年度内顺序应改为跨年"
        elif pred.year < succ.year:
            status = "SATISFIED_CROSS_YEAR"
            detail = "前置工程在更早年度完成"
        else:
            status = "VIOLATED_TIMING"
            detail = "依赖工程早于前置工程，按当前口径不可执行"
        statuses.append(
            DependencyStatus(
                predecessor=dep.predecessor,
                successor=dep.successor,
                shared_site=dep.shared_site,
                repeat_dig_cost=dep.repeat_dig_cost,
                source=dep.source.as_dict(),
                status=status,
                detail=detail,
                predecessor_year=pred_year,
                successor_year=succ_year,
            )
        )
    return statuses


def _districts(
    data: ScenarioData,
    decisions: dict[str, Placement | None],
    demand: dict[str, DistrictDemand],
) -> list[dict[str, Any]]:
    districts = sorted(set(demand) | {p.base.district for p in data.projects.values()})
    rows = []
    for district in districts:
        selected = [
            pid
            for pid, decision in decisions.items()
            if decision is not None and data.projects[pid].base.district == district
        ]
        scheduled_cost = sum(decisions[pid].total_cost for pid in selected if decisions[pid])
        info = demand.get(district)
        rows.append(
            {
                "district": district,
                "confirmed_need_households": info.households if info else 0,
                "demand_known": bool(info and info.known),
                "demand_reason": info.reason if info else "no_need_record",
                "needs": [need.as_dict() for need in (info.needs if info else ())],
                "covered": bool(selected),
                "selected_projects": selected,
                "selected_project_count": len(selected),
                "scheduled_cost": scheduled_cost,
            }
        )
    return rows


def _balance_flags(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    uncovered_with_need = [
        row
        for row in rows
        if row["demand_known"] and row["confirmed_need_households"] > 0 and not row["covered"]
    ]
    covered_multi = [row for row in rows if row["selected_project_count"] >= 2]
    for district in uncovered_with_need:
        if covered_multi:
            flags.append(
                {
                    "code": "UNCOVERED_WHILE_OTHERS_MULTI_PACKAGE",
                    "districts": [district["district"], *[row["district"] for row in covered_multi]],
                    "message": (
                        f"片区 {district['district']} 有已确认需求但未安排项目，"
                        f"同时 {', '.join(row['district'] for row in covered_multi)} 安排了多个小包；"
                        "存在拆分挤占其他片区的风险"
                    ),
                }
            )
        else:
            flags.append(
                {
                    "code": "UNCOVERED_CONFIRMED_NEED",
                    "districts": [district["district"]],
                    "message": f"片区 {district['district']} 有已确认需求但未安排项目",
                }
            )
    known = [row for row in rows if row["demand_known"] and row["confirmed_need_households"] > 0]
    unknown = [row for row in rows if not row["demand_known"]]
    if unknown:
        flags.append(
            {
                "code": "DEMAND_UNKNOWN",
                "districts": [row["district"] for row in unknown],
                "message": "部分片区缺少已确认需求或需求户数，受益均衡结论不完整",
            }
        )
    overlap_risk = any(
        len([need for need in row["needs"] if need["confirmed"] is True]) > 1 for row in known
    )
    if overlap_risk:
        flags.append(
            {
                "code": "DEMAND_OVERLAP_ASSUMED",
                "districts": [row["district"] for row in known],
                "message": "同一片区存在多条已确认需求记录，受益户数按需求条目汇总，可能存在重叠；需以来源记录复核",
            }
        )
    return flags


def _line_usage(
    data: ScenarioData, decisions: dict[str, Placement | None]
) -> list[dict[str, Any]]:
    rows = []
    for index, line in enumerate(data.funding):
        used = sum(
            decision.total_cost
            for decision in decisions.values()
            if decision is not None and decision.line_index == index
        )
        projects = [
            decision.project_id
            for decision in decisions.values()
            if decision is not None and decision.line_index == index
        ]
        rows.append(
            {
                "fund_id": line.fund_id,
                "year": line.year,
                "category": line.category,
                "allowed_work": list(line.allowed_work),
                "cap": line.cap,
                "used": used,
                "remaining": line.cap - used,
                "over_budget": used > line.cap,
                "projects": sorted(projects),
                "source": line.source.as_dict(),
            }
        )
    return rows


def _conflicts(
    data: ScenarioData,
    decisions: dict[str, Placement | None],
    line_usage: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    conflicts = []
    for pid, project in data.projects.items():
        if not project.base.contracted or decisions.get(pid) is not None:
            continue
        matching = [
            row
            for row in line_usage
            if project.base.work_type in row["allowed_work"] and row["year"] >= project.base.year
        ]
        cost = cost_figure(project, data.spec.cost_basis).amount
        best_cap = max((row["cap"] for row in matching), default=0)
        conflicts.append(
            {
                "code": "CONTRACTED_PROJECT_UNSCHEDULED",
                "project_id": pid,
                "district": project.base.district,
                "work_type": project.base.work_type,
                "source": project.base.source.as_dict(),
                "cost": cost,
                "best_available_cap": best_cap,
                "shortfall": max(0, cost - best_cap),
                "message": (
                    f"已签约项目 {pid} 无法在现有资金来源与上限内安排"
                    f"（成本 {cost:,}，可用单条上限最高 {best_cap:,}，缺口 {max(0, cost - best_cap):,}）；"
                    "执行部门不得将其视为已消失，需要追加资金、调整年度或重新协商合同"
                ),
                "candidate_lines": matching,
            }
        )
    for row in line_usage:
        if row["over_budget"]:
            conflicts.append(
                {
                    "code": "LINE_OVER_BUDGET",
                    "fund_id": row["fund_id"],
                    "year": row["year"],
                    "message": f"资金 {row['fund_id']} 在 {row['year']} 年超限 {row['used'] - row['cap']}",
                }
            )
    total_cap = sum(row["cap"] for row in line_usage)
    scheduled = sum(
        placement.total_cost
        for placement in decisions.values()
        if placement is not None
    )
    if scheduled > total_cap:
        conflicts.append(
            {
                "code": "PORTFOLIO_OVER_BUDGET",
                "message": f"方案总成本 {scheduled:,} 超过全部资金上限 {total_cap:,}",
            }
        )
    return conflicts


def _inclusion_reasons(
    data: ScenarioData,
    decisions: dict[str, Placement | None],
    demand: dict[str, DistrictDemand],
    solver: _Solver,
) -> dict[str, Placement]:
    placements: dict[str, Placement] = {}
    for pid, decision in decisions.items():
        if decision is None:
            continue
        project = data.projects[pid]
        info = demand.get(project.base.district)
        if info and info.known:
            benefit = (
                f"片区 {project.base.district} 已确认需求 {info.households} 户，"
                "入选后该片区受益被覆盖"
            )
        elif info:
            benefit = f"片区 {project.base.district} 需求口径未知，受益无法量化"
        else:
            benefit = "该片区没有需求记录，受益无法量化"
        dependency_bits = []
        for dep in solver.dependencies.get(pid, ()):
            pred = decisions.get(dep.predecessor)
            if pred is None:
                dependency_bits.append(f"前置 {dep.predecessor} 未入选（不应计入交付）")
            elif pred.year < decision.year:
                dependency_bits.append(f"前置 {dep.predecessor} 已于 {pred.year} 年完成")
            elif data.spec.allow_same_year_predecessor and pred.year == decision.year:
                dependency_bits.append(f"前置 {dep.predecessor} 同年度先施工")
            else:
                dependency_bits.append(f"前置 {dep.predecessor} 时间不满足")
        if decision.repeat_dig_cost:
            dependency_bits.append(
                f"因与前置工程错开年度，计重复开挖成本 {decision.repeat_dig_cost:,}"
            )
        funding = (
            f"占用 {decision.fund_id}（{decision.line.year} 年，{decision.line.category}，"
            f"允许 {', '.join(decision.line.allowed_work)}），来源 {decision.line.source.file}:{decision.line.source.row}"
        )
        if decision.year > project.base.year:
            timing = f"由请求年度 {project.base.year} 延期至 {decision.year} 年"
        else:
            timing = f"按请求年度 {decision.year} 年安排"
        reason = "；".join([benefit, *dependency_bits, timing, funding])
        placements[pid] = Placement(
            project_id=decision.project_id,
            year=decision.year,
            fund_id=decision.fund_id,
            line_index=decision.line_index,
            line=decision.line,
            work_cost=decision.work_cost,
            repeat_dig_cost=decision.repeat_dig_cost,
            cost=decision.cost,
            reason=reason,
        )
    return placements


def analyze_plan(
    data: ScenarioData,
    strategy: str,
    node_limit: int,
    time_limit_seconds: float = 2.5,
) -> StrategyPlan:
    if strategy not in STRATEGY_LABELS:
        raise ValueError(f"unknown strategy {strategy}")
    if data.errors:
        return StrategyPlan(
            strategy=strategy,
            status="INVALID_INPUT",
            placements={},
            rejections={},
            dependencies=[],
            districts=[],
            line_usage=[],
            totals={},
            objective=(),
            conflicts=[],
            notes=["输入校验未通过，未执行组合测算"],
            search={
                "nodes": 0,
                "node_limit": node_limit,
                "time_limit_seconds": time_limit_seconds,
                "exhaustive": False,
            },
            warnings=data.warnings,
        )
    solver = _Solver(data, strategy, node_limit, time_limit_seconds)
    decisions, objective, incomplete, nodes = solver.solve()
    demand = solver.demand
    placements = _inclusion_reasons(data, decisions, demand, solver)
    rejections = _rejections(data, solver, decisions)
    dependencies = _dependency_statuses(data, decisions)
    districts = _districts(data, decisions, demand)
    flags = _balance_flags(districts)
    line_usage = _line_usage(data, decisions)
    conflicts = _conflicts(data, decisions, line_usage)
    unplaced_forced = [
        pid
        for pid, project in data.projects.items()
        if project.base.contracted and decisions.get(pid) is None
    ]
    unconfirmed = [
        pid for pid, placement in placements.items() if placement.cost.unconfirmed
    ]
    project_districts = {project.base.district for project in data.projects.values()}
    unknown_demand = sorted(
        district
        for district in project_districts
        if district in demand and not demand[district].known
    )
    unknown_demand_total = sum(
        demand[district].households for district in unknown_demand
    )
    if unplaced_forced:
        status = "INFEASIBLE_COMMITMENT"
    elif incomplete:
        status = "SEARCH_INCOMPLETE"
    elif unconfirmed or unknown_demand:
        status = "PROVISIONAL"
    else:
        status = "EXECUTABLE"
    total_cost = sum(placement.total_cost for placement in placements.values())
    covered_districts = [row["district"] for row in districts if row["covered"]]
    covered_households = sum(
        row["confirmed_need_households"] for row in districts if row["covered"] and row["demand_known"]
    )
    demand_households = sum(
        row["confirmed_need_households"] for row in districts if row["demand_known"]
    )
    requested_cost = sum(
        cost_figure(project, data.spec.cost_basis).amount
        for project in data.projects.values()
    )
    total_cap = sum(line.cap for line in data.funding)
    totals = {
        "selected_count": len(placements),
        "requested_count": len(data.projects),
        "total_cost": total_cost,
        "work_cost": sum(placement.work_cost for placement in placements.values()),
        "repeat_dig_cost": sum(placement.repeat_dig_cost for placement in placements.values()),
        "requested_cost": requested_cost,
        "total_cap": total_cap,
        "unfunded_cost": requested_cost - total_cost,
        "budget_pressure": round(requested_cost / total_cap, 4) if total_cap else None,
        "within_cap": total_cost <= total_cap,
        "covered_districts": covered_districts,
        "uncovered_districts": [row["district"] for row in districts if not row["covered"]],
        "covered_households": covered_households,
        "confirmed_demand_households": demand_households,
        "weighted_coverage": (
            round(covered_households / demand_households, 4) if demand_households else None
        ),
        "unplaced_contracted": unplaced_forced,
        "unconfirmed_cost_projects": unconfirmed,
        "demand_unknown_districts": unknown_demand,
        "demand_unknown_households": unknown_demand_total,
        "same_year_assumption": data.spec.allow_same_year_predecessor,
        "deferred_projects": [
            pid
            for pid, placement in placements.items()
            if placement.year > data.projects[pid].base.year
        ],
    }
    notes: list[str] = []
    if data.spec.allow_same_year_predecessor:
        notes.append(
            "前置工程与依赖工程安排在同一年度时，按年度内前置先完成、依赖后接续处理；"
            "执行部门须在施工组织中保证该顺序。"
        )
    else:
        notes.append("前置工程必须安排在更早年度；同年度不能计入依赖工程的可交付量。")
    notes.append(
        "共享施工范围若跨年度安排，重复开挖成本计入依赖工程所在资金行。"
    )
    notes.append(
        "主目标（已签约项目、片区受益、依赖链完整）按精确搜索求解；"
        "受益相同时按较低成本优先，但工具不声称全局成本最小。"
    )
    if incomplete:
        notes.append(
            "搜索达到节点或时间上限，本方案是已找到的最好安排，但未证明主目标最优；"
            "状态标记为“未完成”，不得当作最终可执行方案。"
        )
    notes.append(
        "项目与具体民生需求之间没有直接映射，片区受益按“该片区是否有项目入选”计算，"
        "户数取该片区已确认需求户数之和；存在需求重叠时以来源记录复核。"
    )
    return StrategyPlan(
        strategy=strategy,
        status=status,
        placements=placements,
        rejections=rejections,
        dependencies=dependencies,
        districts=districts,
        line_usage=line_usage,
        totals={**totals, "balance_flags": flags},
        objective=objective,
        conflicts=conflicts,
        notes=notes,
        search={
            "nodes": nodes,
            "node_limit": node_limit,
            "time_limit_seconds": time_limit_seconds,
            "exhaustive": not incomplete,
        },
        warnings=data.warnings,
    )


def _plan_diff(base: StrategyPlan, other: StrategyPlan) -> dict[str, Any]:
    base_ids = set(base.placements)
    other_ids = set(other.placements)
    moved = []
    cost_changed = []
    for pid in sorted(base_ids & other_ids):
        left = base.placements[pid]
        right = other.placements[pid]
        if (left.year, left.fund_id) != (right.year, right.fund_id):
            moved.append(
                {
                    "project_id": pid,
                    "from": {"year": left.year, "fund_id": left.fund_id},
                    "to": {"year": right.year, "fund_id": right.fund_id},
                }
            )
        if left.total_cost != right.total_cost:
            cost_changed.append(
                {
                    "project_id": pid,
                    "from": left.total_cost,
                    "to": right.total_cost,
                }
            )
    return {
        "status": {"from": base.status, "to": other.status},
        "added": sorted(other_ids - base_ids),
        "removed": sorted(base_ids - other_ids),
        "moved": moved,
        "cost_changed": cost_changed,
        "total_cost": {
            "from": base.totals.get("total_cost"),
            "to": other.totals.get("total_cost"),
        },
        "covered_districts": {
            "from": base.totals.get("covered_districts"),
            "to": other.totals.get("covered_districts"),
        },
        "new_conflicts": [
            conflict
            for conflict in other.conflicts
            if conflict.get("project_id") not in {item.get("project_id") for item in base.conflicts}
        ],
    }


def analyze_scenario(data: ScenarioData) -> ScenarioAnalysis:
    validation = {
        "errors": [issue.as_dict() for issue in data.errors],
        "warnings": [issue.as_dict() for issue in data.warnings],
        "executable_input": not data.errors,
    }
    plans: dict[str, StrategyPlan] = {}
    if not data.errors:
        for strategy in data.spec.strategies:
            plans[strategy] = analyze_plan(
                data,
                strategy,
                data.spec.search_node_limit,
                data.spec.search_time_limit_seconds,
            )
    approved = plans.get(data.spec.approved_strategy)
    if data.errors:
        status = "INVALID_INPUT"
    elif approved is None:
        status = "INVALID_INPUT"
    else:
        status = approved.status
    comparison: dict[str, Any] = {"strategies": []}
    if plans:
        baseline = approved or next(iter(plans.values()))
        for name, plan in plans.items():
            comparison["strategies"].append(
                {
                    "strategy": name,
                    "label": STRATEGY_LABELS[name],
                    "status": plan.status,
                    "selected_count": plan.totals.get("selected_count"),
                    "total_cost": plan.totals.get("total_cost"),
                    "covered_districts": plan.totals.get("covered_districts"),
                    "covered_households": plan.totals.get("covered_households"),
                    "weighted_coverage": plan.totals.get("weighted_coverage"),
                    "unplaced_contracted": plan.totals.get("unplaced_contracted"),
                    "diff_from_approved": _plan_diff(baseline, plan) if name != baseline.strategy else None,
                }
            )
    return ScenarioAnalysis(
        scenario=data.spec.name,
        status=status,
        validation=validation,
        plans=plans,
        approved_strategy=data.spec.approved_strategy,
        approved=approved,
        comparison=comparison,
        revision_log=data.revision_log,
        snapshot=data.snapshot,
    )
