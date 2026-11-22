"""Domain model for the renewal portfolio planner.

All monetary amounts are integer yuan.  Dates are kept as source strings so
that the original wording of a revision can be reproduced in an audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

SCHEMA_VERSION = "1.0"

IssueLevel = Literal["error", "warning"]


@dataclass(frozen=True)
class SourceRef:
    file: str
    row: int  # CSV line number (header is line 1)

    def as_dict(self) -> dict[str, Any]:
        return {"file": self.file, "row": self.row}


@dataclass(frozen=True)
class Issue:
    level: IssueLevel
    code: str
    message: str
    file: str | None = None
    row: int | None = None
    field: str | None = None
    subject: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "file": self.file,
            "row": self.row,
            "field": self.field,
            "subject": self.subject,
        }


@dataclass(frozen=True)
class FundingLine:
    fund_id: str
    year: int
    category: str
    cap: int
    allowed_work: tuple[str, ...]
    source: SourceRef

    def as_dict(self) -> dict[str, Any]:
        return {
            "fund_id": self.fund_id,
            "year": self.year,
            "category": self.category,
            "cap": self.cap,
            "allowed_work": list(self.allowed_work),
            "source": self.source.as_dict(),
        }


@dataclass(frozen=True)
class ProjectRecord:
    project_id: str
    district: str
    work_type: str
    estimate: int | None
    confirmed_cost: int | None
    year: int
    contracted: bool
    source: SourceRef


@dataclass(frozen=True)
class Dependency:
    predecessor: str
    successor: str
    shared_site: bool
    repeat_dig_cost: int | None
    source: SourceRef

    def as_dict(self) -> dict[str, Any]:
        return {
            "predecessor": self.predecessor,
            "successor": self.successor,
            "shared_site": self.shared_site,
            "repeat_dig_cost": self.repeat_dig_cost,
            "source": self.source.as_dict(),
        }


@dataclass(frozen=True)
class NeedRecord:
    district: str
    group: str
    need_code: str
    affected_households: int | None
    confirmed: bool | None
    source: SourceRef

    def as_dict(self) -> dict[str, Any]:
        return {
            "district": self.district,
            "group": self.group,
            "need_code": self.need_code,
            "affected_households": self.affected_households,
            "confirmed": self.confirmed,
            "source": self.source.as_dict(),
        }


@dataclass
class Dataset:
    projects: dict[str, ProjectRecord] = field(default_factory=dict)
    project_order: list[str] = field(default_factory=list)
    funding: list[FundingLine] = field(default_factory=list)
    dependencies: list[Dependency] = field(default_factory=list)
    needs: list[NeedRecord] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    snapshot: dict[str, Any] = field(default_factory=dict)

    @property
    def errors(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.level == "warning"]

    def funding_years(self) -> list[int]:
        return sorted({line.year for line in self.funding})


@dataclass(frozen=True)
class CostRevision:
    project_id: str
    confirmed_cost: int
    as_of: str
    reason: str
    source: str
    scenario_source: SourceRef

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "confirmed_cost": self.confirmed_cost,
            "as_of": self.as_of,
            "reason": self.reason,
            "source": self.source,
            "scenario_source": self.scenario_source.as_dict(),
        }


@dataclass(frozen=True)
class ScenarioSpec:
    name: str
    description: str
    cost_basis: str
    strategies: tuple[str, ...]
    revisions: tuple[CostRevision, ...] = ()
    funding_additions: tuple[FundingLine, ...] = ()
    funding_adjustments: tuple[tuple[str, int, int, SourceRef], ...] = ()
    search_node_limit: int = 300_000
    search_time_limit_seconds: float = 2.5
    approved_strategy: str = "benefit"
    allow_same_year_predecessor: bool = False
    provenance: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "cost_basis": self.cost_basis,
            "strategies": list(self.strategies),
            "approved_strategy": self.approved_strategy,
            "search_node_limit": self.search_node_limit,
            "search_time_limit_seconds": self.search_time_limit_seconds,
            "allow_same_year_predecessor": self.allow_same_year_predecessor,
            "cost_revisions": [revision.as_dict() for revision in self.revisions],
            "funding_additions": [line.as_dict() for line in self.funding_additions],
            "funding_adjustments": [
                {"fund_id": fund_id, "year": year, "cap": cap, "scenario_source": source.as_dict()}
                for fund_id, year, cap, source in self.funding_adjustments
            ],
        }


@dataclass(frozen=True)
class EffectiveProject:
    base: ProjectRecord
    confirmed_cost: int | None
    revision: CostRevision | None

    @property
    def project_id(self) -> str:
        return self.base.project_id


@dataclass(frozen=True)
class CostFigure:
    amount: int
    low: int
    high: int
    basis: str  # "confirmed" or "estimate"
    unconfirmed: bool
    estimate: int | None
    confirmed_cost: int | None
    source: SourceRef
    revision: CostRevision | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "amount": self.amount,
            "low": self.low,
            "high": self.high,
            "basis": self.basis,
            "unconfirmed": self.unconfirmed,
            "estimate": self.estimate,
            "confirmed_cost": self.confirmed_cost,
            "source": self.source.as_dict(),
            "revision": self.revision.as_dict() if self.revision else None,
        }


@dataclass
class ScenarioData:
    dataset: Dataset
    spec: ScenarioSpec
    projects: dict[str, EffectiveProject]
    funding: list[FundingLine]
    issues: list[Issue]
    revision_log: list[dict[str, Any]]
    snapshot: dict[str, Any]

    @property
    def errors(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.level == "warning"]

    def funding_years(self) -> list[int]:
        return sorted({line.year for line in self.funding})


@dataclass(frozen=True)
class DistrictDemand:
    district: str
    households: int | None
    known: bool
    reason: str  # ok | no_confirmed_need | unknown_households
    needs: tuple[NeedRecord, ...]


