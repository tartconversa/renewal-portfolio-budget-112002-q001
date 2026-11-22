"""Scenario definitions, cost revisions and effective funding envelopes.

A scenario is a *named, reproducible* set of assumptions layered on top of the
source tables.  It never mutates the dataset; instead it produces effective
project costs and funding envelopes together with a revision log so the
original basis remains visible.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from .io import _Collector, FUNDING_FIELDS
from .model import (
    CostFigure,
    CostRevision,
    Dataset,
    EffectiveProject,
    FundingLine,
    Issue,
    ScenarioData,
    ScenarioSpec,
    SourceRef,
)

COST_BASES = ("confirmed", "estimate", "upper_bound")
STRATEGIES = ("benefit", "cost", "district", "sequence")

BUILTIN_SCENARIOS: dict[str, ScenarioSpec] = {
    "confirmed_costs": ScenarioSpec(
        name="confirmed_costs",
        description=(
            "基准情景：按已核定成本测算，前置工程必须安排在更早年度。"
            "P-02 依赖 P-01，但两者都只在 2027 年有可用资金，因此 P-02 不能计入 2027 年可交付量。"
        ),
        cost_basis="confirmed",
        strategies=STRATEGIES,
        approved_strategy="benefit",
    ),
    "estimates": ScenarioSpec(
        name="estimates",
        description=(
            "按最初估算口径测算，用于与核定值口径对照；缺少核定值的项目标记为未核定。"
        ),
        cost_basis="estimate",
        strategies=STRATEGIES,
        approved_strategy="benefit",
    ),
    "upper_bound": ScenarioSpec(
        name="upper_bound",
        description="每个项目取已知成本的较高值，检验最坏情况下的资金可行性。",
        cost_basis="upper_bound",
        strategies=STRATEGIES,
        approved_strategy="benefit",
    ),
    "next_year": ScenarioSpec(
        name="next_year",
        description=(
            "成本核定值更新后的重排：P-02 核定 380000、P-03 核定 305000；"
            "新增 2028 年 F-04 drainage 500000（pipe;street）。"
            "P-02 调整为 2028 年安排，并因与 P-01 共享施工范围跨年而产生重复开挖成本。"
        ),
        cost_basis="confirmed",
        strategies=STRATEGIES,
        revisions=(
            CostRevision(
                project_id="P-02",
                confirmed_cost=380_000,
                as_of="2027-02-15",
                reason="施工图预算核定，高于原估算",
                source="cost-review-2027-02/P-02",
                scenario_source=SourceRef("builtin:next_year", 1),
            ),
            CostRevision(
                project_id="P-03",
                confirmed_cost=305_000,
                as_of="2027-02-15",
                reason="按结算口径核定，略高于原核定值",
                source="cost-review-2027-02/P-03",
                scenario_source=SourceRef("builtin:next_year", 2),
            ),
        ),
        funding_additions=(
            FundingLine(
                fund_id="F-04",
                year=2028,
                category="drainage",
                cap=500_000,
                allowed_work=("pipe", "street"),
                source=SourceRef("builtin:next_year", 3),
            ),
        ),
        approved_strategy="benefit",
    ),
    "same_year_sequence": ScenarioSpec(
        name="same_year_sequence",
        description=(
            "允许前置工程与依赖工程在同一年度内按先后顺序接续施工，"
            "但必须在施工组织中保证前置先完成。用于与基准情景比较。"
        ),
        cost_basis="confirmed",
        strategies=STRATEGIES,
        allow_same_year_predecessor=True,
        approved_strategy="benefit",
    ),
    "same_year_sequence": ScenarioSpec(
        name="same_year_sequence",
        description=(
            "允许前置工程与依赖工程在同一年度内按先后顺序接续施工，"
            "但需在施工组织中保证前置先完成；重复开挖成本按同一施工年度免计。"
        ),
        cost_basis="confirmed",
        strategies=STRATEGIES,
        allow_same_year_predecessor=True,
        approved_strategy="benefit",
    ),
    "budget_cut": ScenarioSpec(
        name="budget_cut",
        description=(
            "资金上限收紧：F-01 降至 450000、F-02 降至 500000。"
            "已签约项目 P-01（核定 520000）没有任何单条资金可容纳，"
            "应显示冲突及受影响承诺，而不是静默消失。"
        ),
        cost_basis="confirmed",
        strategies=STRATEGIES,
        funding_adjustments=(
            ("F-01", 2027, 450_000, SourceRef("builtin:budget_cut", 1)),
            ("F-02", 2027, 500_000, SourceRef("builtin:budget_cut", 2)),
        ),
        approved_strategy="benefit",
    ),
}

SCENARIO_FIELDS = {
    "name",
    "description",
    "cost_basis",
    "strategies",
    "approved_strategy",
    "search_node_limit",
    "search_time_limit_seconds",
    "allow_same_year_predecessor",
    "cost_revisions",
    "funding_additions",
    "funding_adjustments",
}


def _revision_from_row(row: dict[str, Any], file: str, index: int, collector: _Collector) -> CostRevision | None:
    source = SourceRef(f"{file}#cost_revisions", index)
    required = ("project_id", "confirmed_cost", "as_of", "reason", "source")
    missing = [key for key in required if not str(row.get(key, "")).strip()]
    if missing:
        collector.error(
            "SCENARIO_REVISION_INCOMPLETE",
            f"成本修订缺少字段: {', '.join(missing)}；修订必须留痕",
            file=file,
            row=index,
        )
        return None
    try:
        amount = int(row["confirmed_cost"])
    except (TypeError, ValueError):
        collector.error(
            "SCENARIO_REVISION_COST",
            "confirmed_cost 必须是整数",
            file=file,
            row=index,
            subject=str(row.get("project_id")),
        )
        return None
    if amount < 0:
        collector.error(
            "SCENARIO_REVISION_COST",
            "confirmed_cost 不能为负",
            file=file,
            row=index,
            subject=str(row.get("project_id")),
        )
        return None
    return CostRevision(
        project_id=str(row["project_id"]),
        confirmed_cost=amount,
        as_of=str(row["as_of"]),
        reason=str(row["reason"]),
        source=str(row["source"]),
        scenario_source=source,
    )


def _funding_addition_from_row(
    row: dict[str, Any], file: str, index: int, collector: _Collector
) -> FundingLine | None:
    source = SourceRef(f"{file}#funding_additions", index)
    missing = [key for key in FUNDING_FIELDS if not str(row.get(key, "")).strip()]
    if missing:
        collector.error(
            "SCENARIO_FUNDING_INCOMPLETE",
            f"新增资金缺少字段: {', '.join(missing)}",
            file=file,
            row=index,
        )
        return None
    try:
        year = int(row["year"])
        cap = int(row["cap"])
    except (TypeError, ValueError):
        collector.error(
            "SCENARIO_FUNDING_NUMBER",
            "新增资金的 year 与 cap 必须是整数",
            file=file,
            row=index,
            subject=str(row.get("fund_id")),
        )
        return None
    if year < 2000 or cap < 0:
        collector.error(
            "SCENARIO_FUNDING_RANGE",
            "新增资金的 year 必须 >= 2000 且 cap 不能为负",
            file=file,
            row=index,
            subject=str(row.get("fund_id")),
        )
        return None
    allowed = tuple(part.strip() for part in str(row["allowed_work"]).split(";") if part.strip())
    if not allowed:
        collector.error(
            "SCENARIO_FUNDING_ALLOWED",
            "新增资金 allowed_work 不能为空",
            file=file,
            row=index,
            subject=str(row.get("fund_id")),
        )
        return None
    return FundingLine(
        fund_id=str(row["fund_id"]),
        year=year,
        category=str(row["category"]),
        cap=cap,
        allowed_work=allowed,
        source=source,
    )


def load_scenario_file(path: str | Path) -> tuple[ScenarioSpec | None, list[Issue]]:
    """Load a scenario JSON file, returning the spec and any validation issues."""
    collector = _Collector()
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        collector.error("SCENARIO_UNREADABLE", f"无法读取情景文件: {exc}", file=Path(path).name)
        return None, collector.issues
    if not isinstance(raw, dict):
        collector.error("SCENARIO_SHAPE", "情景文件顶层必须是对象", file=Path(path).name)
        return None, collector.issues
    name = str(raw.get("name", "")).strip()
    if not name:
        collector.error("SCENARIO_NAME", "情景必须提供 name", file=Path(path).name)
    unknown = sorted(set(raw) - SCENARIO_FIELDS)
    if unknown:
        collector.warning(
            "SCENARIO_UNKNOWN_FIELD",
            f"情景文件含未识别字段: {', '.join(unknown)}",
            file=Path(path).name,
        )
    basis = str(raw.get("cost_basis", "confirmed"))
    if basis not in COST_BASES:
        collector.error(
            "SCENARIO_COST_BASIS",
            f"cost_basis 必须是 {', '.join(COST_BASES)} 之一",
            file=Path(path).name,
        )
    strategies = raw.get("strategies", list(STRATEGIES))
    if not isinstance(strategies, list) or not strategies:
        collector.error(
            "SCENARIO_STRATEGIES", "strategies 必须是非空数组", file=Path(path).name
        )
        strategies = list(STRATEGIES)
    strategies_t = tuple(str(item) for item in strategies)
    bad = [item for item in strategies_t if item not in STRATEGIES]
    if bad:
        collector.error(
            "SCENARIO_STRATEGY_UNKNOWN",
            f"未知策略: {', '.join(bad)}",
            file=Path(path).name,
        )
    approved = str(raw.get("approved_strategy", strategies_t[0]))
    if approved not in strategies_t:
        collector.error(
            "SCENARIO_APPROVED_STRATEGY",
            "approved_strategy 必须出现在 strategies 中",
            file=Path(path).name,
        )
    limit = raw.get("search_node_limit", 300_000)
    if not isinstance(limit, int) or limit <= 0:
        collector.error(
            "SCENARIO_NODE_LIMIT", "search_node_limit 必须是正整数", file=Path(path).name
        )
        limit = 300_000
    time_limit = raw.get("search_time_limit_seconds", 2.5)
    if not isinstance(time_limit, (int, float)) or time_limit <= 0:
        collector.error(
            "SCENARIO_TIME_LIMIT",
            "search_time_limit_seconds 必须是正数",
            file=Path(path).name,
        )
        time_limit = 2.5
    allow_same_year = raw.get("allow_same_year_predecessor", False)
    if not isinstance(allow_same_year, bool):
        collector.error(
            "SCENARIO_SAME_YEAR_FLAG",
            "allow_same_year_predecessor 必须是 true/false",
            file=Path(path).name,
        )
        allow_same_year = False
    revisions = []
    for index, row in enumerate(raw.get("cost_revisions", []), start=1):
        if not isinstance(row, dict):
            collector.error(
                "SCENARIO_REVISION_SHAPE",
                "cost_revisions 每行必须是对象",
                file=Path(path).name,
                row=index,
            )
            continue
        revision = _revision_from_row(row, Path(path).name, index, collector)
        if revision:
            revisions.append(revision)
    additions = []
    for index, row in enumerate(raw.get("funding_additions", []), start=1):
        if not isinstance(row, dict):
            collector.error(
                "SCENARIO_FUNDING_SHAPE",
                "funding_additions 每行必须是对象",
                file=Path(path).name,
                row=index,
            )
            continue
        line = _funding_addition_from_row(row, Path(path).name, index, collector)
        if line:
            additions.append(line)
    adjustments = []
    for index, row in enumerate(raw.get("funding_adjustments", []), start=1):
        if not isinstance(row, dict) or not {"fund_id", "year", "cap"} <= set(row):
            collector.error(
                "SCENARIO_ADJUSTMENT_SHAPE",
                "funding_adjustments 每行需要 fund_id/year/cap",
                file=Path(path).name,
                row=index,
            )
            continue
        try:
            year = int(row["year"])
            cap = int(row["cap"])
        except (TypeError, ValueError):
            collector.error(
                "SCENARIO_ADJUSTMENT_NUMBER",
                "调整后的 year 与 cap 必须是整数",
                file=Path(path).name,
                row=index,
                subject=str(row.get("fund_id")),
            )
            continue
        if cap < 0:
            collector.error(
                "SCENARIO_ADJUSTMENT_RANGE",
                "调整后的 cap 不能为负",
                file=Path(path).name,
                row=index,
                subject=str(row.get("fund_id")),
            )
            continue
        adjustments.append(
            (
                str(row["fund_id"]),
                year,
                cap,
                SourceRef(f"{Path(path).name}#funding_adjustments", index),
            )
        )
    if collector.errors:
        return None, collector.issues
    return (
        ScenarioSpec(
            name=name,
            description=str(raw.get("description", "")),
            cost_basis=basis,
            strategies=strategies_t,
            approved_strategy=approved,
            revisions=tuple(revisions),
            funding_additions=tuple(additions),
            funding_adjustments=tuple(adjustments),
            search_node_limit=limit,
            search_time_limit_seconds=float(time_limit),
            allow_same_year_predecessor=allow_same_year,
            provenance={"file": str(path)},
        ),
        collector.issues,
    )


def resolve_scenario(
    dataset: Dataset,
    spec: ScenarioSpec,
) -> ScenarioData:
    """Build effective projects and funding for one scenario.

    The base dataset is never modified; each revision is layered on a copy and
    logged with both the old and the new basis.
    """
    collector = _Collector()
    issues = list(dataset.issues)
    projects = {
        pid: EffectiveProject(base=record, confirmed_cost=record.confirmed_cost, revision=None)
        for pid, record in dataset.projects.items()
    }

    revisions_by_project: dict[str, list[CostRevision]] = {}
    for revision in spec.revisions:
        if revision.project_id not in projects:
            collector.error(
                "SCENARIO_REVISION_UNKNOWN_PROJECT",
                f"成本修订引用未知项目 {revision.project_id}",
                file=revision.scenario_source.file,
                row=revision.scenario_source.row,
                subject=revision.project_id,
            )
            continue
        revisions_by_project.setdefault(revision.project_id, []).append(revision)

    revision_log: list[dict[str, Any]] = []
    sequence = 0
    for pid, revisions in revisions_by_project.items():
        ordered = sorted(revisions, key=lambda item: (item.as_of, item.source))
        current = projects[pid]
        for revision in ordered:
            sequence += 1
            old_estimate = current.base.estimate
            old_confirmed = current.confirmed_cost
            current = EffectiveProject(
                base=current.base, confirmed_cost=revision.confirmed_cost, revision=revision
            )
            revision_log.append(
                {
                    "sequence": sequence,
                    "project_id": pid,
                    "as_of": revision.as_of,
                    "reason": revision.reason,
                    "evidence": revision.source,
                    "scenario_source": revision.scenario_source.as_dict(),
                    "previous": {"estimate": old_estimate, "confirmed_cost": old_confirmed},
                    "updated": {
                        "estimate": current.base.estimate,
                        "confirmed_cost": revision.confirmed_cost,
                    },
                    "preserved": {
                        "source_file": current.base.source.file,
                        "source_row": current.base.source.row,
                        "estimate": current.base.estimate,
                    },
                }
            )
        projects[pid] = current

    funding = list(dataset.funding)
    for fund_id, year, cap, source in spec.funding_adjustments:
        matches = [
            index for index, line in enumerate(funding) if line.fund_id == fund_id and line.year == year
        ]
        if len(matches) != 1:
            collector.error(
                "SCENARIO_ADJUSTMENT_UNMATCHED",
                f"资金调整必须唯一匹配一条资金记录（{fund_id}, {year}），实际匹配 {len(matches)} 条",
                file=source.file,
                row=source.row,
                subject=fund_id,
            )
            continue
        index = matches[0]
        old = funding[index]
        funding[index] = replace(old, cap=cap, source=source)
        revision_log.append(
            {
                "sequence": len(revision_log) + 1,
                "kind": "funding_cap",
                "fund_id": fund_id,
                "year": year,
                "previous": {"cap": old.cap, "source": old.source.as_dict()},
                "updated": {"cap": cap, "scenario_source": source.as_dict()},
            }
        )
    for line in spec.funding_additions:
        existing = [item for item in funding if item.fund_id == line.fund_id and item.year == line.year]
        if existing:
            collector.error(
                "SCENARIO_FUNDING_DUPLICATE",
                f"新增资金来源 {line.fund_id} 在 {line.year} 年已存在；上限口径会重复",
                file=line.source.file,
                row=line.source.row,
                subject=line.fund_id,
            )
            continue
        funding.append(line)
        revision_log.append(
            {
                "sequence": len(revision_log) + 1,
                "kind": "funding_added",
                "fund_id": line.fund_id,
                "year": line.year,
                "updated": line.as_dict(),
            }
        )

    if not funding:
        collector.error("SCENARIO_NO_FUNDING", "情景没有可用资金上限")

    # Re-run portfolio-level checks against effective values, then collect.
    _effective_checks(projects, funding, collector)
    issues.extend(collector.issues)

    snapshot = dict(dataset.snapshot)
    snapshot["scenario"] = spec.as_dict()
    snapshot["scenario_issues"] = [issue.as_dict() for issue in collector.issues]
    snapshot["effective_funding"] = [line.as_dict() for line in funding]
    snapshot["revision_log"] = revision_log
    snapshot["effective_costs"] = {
        pid: cost_figure(project, spec.cost_basis).as_dict()
        for pid, project in projects.items()
    }
    snapshot["projects"] = {
        pid: {
            "district": project.base.district,
            "work_type": project.base.work_type,
            "requested_year": project.base.year,
            "contracted": project.base.contracted,
            "estimate": project.base.estimate,
            "confirmed_cost": project.confirmed_cost,
            "source": project.base.source.as_dict(),
        }
        for pid, project in projects.items()
    }
    snapshot["needs"] = [need.as_dict() for need in dataset.needs]
    snapshot["dependencies"] = [dep.as_dict() for dep in dataset.dependencies]
    return ScenarioData(
        dataset=dataset,
        spec=spec,
        projects=projects,
        funding=funding,
        issues=issues,
        revision_log=revision_log,
        snapshot=snapshot,
    )


def _effective_checks(
    projects: dict[str, EffectiveProject],
    funding: list[FundingLine],
    collector: _Collector,
) -> None:
    for pid, project in projects.items():
        if project.confirmed_cost is not None and project.confirmed_cost < 0:
            collector.error(
                "EFFECTIVE_NEGATIVE_COST",
                "核定成本不能为负",
                subject=pid,
            )
        if not any(project.base.work_type in line.allowed_work for line in funding):
            collector.error(
                "EFFECTIVE_UNFUNDABLE_TYPE",
                f"没有任何资金类别允许 {project.base.work_type}；项目无法安排",
                subject=pid,
            )


def cost_figure(project: EffectiveProject, basis: str) -> CostFigure:
    """Return the cost used for this scenario, keeping both known values."""
    base = project.base
    estimate = base.estimate
    confirmed = project.confirmed_cost
    known = [value for value in (estimate, confirmed) if value is not None]
    if not known:
        # Validation should have caught this, but never invent a cost.
        amount = 0
        return CostFigure(
            amount=amount,
            low=0,
            high=0,
            basis=basis,
            unconfirmed=True,
            estimate=estimate,
            confirmed_cost=confirmed,
            source=project.revision.scenario_source if project.revision else base.source,
            revision=project.revision,
        )
    if basis == "estimate":
        if estimate is None:
            amount = confirmed or 0
            unconfirmed = True
        else:
            amount = estimate
            unconfirmed = confirmed is None or confirmed != estimate
        low = high = amount
    elif basis == "upper_bound":
        amount = max(known)
        low, high = min(known), max(known)
        unconfirmed = confirmed is None
    else:  # confirmed
        if confirmed is not None:
            amount = confirmed
            unconfirmed = False
            low = high = amount
        else:
            amount = estimate or 0
            unconfirmed = True
            low = high = amount
    return CostFigure(
        amount=amount,
        low=low,
        high=high,
        basis=basis,
        unconfirmed=unconfirmed,
        estimate=estimate,
        confirmed_cost=confirmed,
        source=project.revision.scenario_source if project.revision else base.source,
        revision=project.revision,
    )
