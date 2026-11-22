"""CSV ingestion with strict, provenance-preserving validation.

Empty cells mean *unknown* — never zero.  A dataset with errors is still
returned so that the caller can report every problem at once, but it must not
be used to produce an executable plan.
"""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path
from typing import Any

from .model import (
    SCHEMA_VERSION,
    Dataset,
    Dependency,
    FundingLine,
    Issue,
    NeedRecord,
    ProjectRecord,
    SourceRef,
)

WORK_TYPES = ("pipe", "street", "service", "housing")

PROJECT_FIELDS = (
    "project_id",
    "district",
    "work_type",
    "estimate",
    "confirmed_cost",
    "year",
    "contracted",
)
FUNDING_FIELDS = ("fund_id", "year", "category", "cap", "allowed_work")
DEPENDENCY_FIELDS = ("predecessor", "successor", "shared_site", "repeat_dig_cost")
NEED_FIELDS = ("district", "group", "need_code", "affected_households", "confirmed")


class _Collector:
    def __init__(self) -> None:
        self.issues: list[Issue] = []

    @property
    def errors(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.level == "error"]

    def error(self, code: str, message: str, **where: Any) -> None:
        self.issues.append(Issue(level="error", code=code, message=message, **where))

    def warning(self, code: str, message: str, **where: Any) -> None:
        self.issues.append(Issue(level="warning", code=code, message=message, **where))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_raw(
    path: Path,
    expected: tuple[str, ...],
    collector: _Collector,
    *,
    allow_empty: bool = False,
) -> list[dict[str, Any]]:
    """Read a CSV, returning one dict per data row with source provenance.

    A row dict contains ``_line`` (physical CSV line) and ``_cells``.  Missing
    or duplicate header columns are fatal for the file.
    """
    if not path.is_file():
        collector.error("INPUT_MISSING", f"缺少输入文件 {path.name}", file=path.name)
        return []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        header = tuple(reader.fieldnames or ())
        missing = [name for name in expected if name not in header]
        extra = [name for name in header if name not in expected]
        if missing:
            collector.error(
                "INPUT_HEADER_MISSING",
                f"{path.name} 缺少字段: {', '.join(missing)}",
                file=path.name,
            )
            return []
        if extra:
            collector.warning(
                "INPUT_HEADER_EXTRA",
                f"{path.name} 含未在契约中声明的字段: {', '.join(extra)}",
                file=path.name,
            )
        rows: list[dict[str, Any]] = []
        for row in reader:
            cells = {key: (row.get(key) or "").strip() for key in expected}
            rows.append({"_line": reader.line_num, "_cells": cells})
        if not rows:
            if allow_empty:
                collector.warning(
                    "INPUT_NO_RECORDS",
                    f"{path.name} 没有数据行；按没有此类记录处理",
                    file=path.name,
                )
            else:
                collector.error("INPUT_EMPTY", f"{path.name} 没有数据行", file=path.name)
        return rows


def _parse_int(
    value: str,
    *,
    field: str,
    file: str,
    line: int,
    collector: _Collector,
    subject: str,
    required: bool = True,
    minimum: int = 0,
) -> int | None:
    if value == "":
        if required:
            collector.error(
                "FIELD_EMPTY",
                f"{field} 为空；空值表示未知，不能当作 0",
                file=file,
                row=line,
                field=field,
                subject=subject,
            )
        return None
    try:
        number = int(value)
    except ValueError:
        collector.error(
            "FIELD_NOT_INT",
            f"{field} 必须是整数，实际为 {value!r}",
            file=file,
            row=line,
            field=field,
            subject=subject,
        )
        return None
    if number < minimum:
        collector.error(
            "FIELD_OUT_OF_RANGE",
            f"{field} 不能小于 {minimum}，实际为 {number}",
            file=file,
            row=line,
            field=field,
            subject=subject,
        )
        return None
    return number


def _parse_bool(
    value: str,
    *,
    field: str,
    file: str,
    line: int,
    collector: _Collector,
    subject: str,
    required: bool = True,
) -> bool | None:
    if value == "":
        if required:
            collector.error(
                "FIELD_EMPTY",
                f"{field} 为空；无法判断现状，不能默认为否",
                file=file,
                row=line,
                field=field,
                subject=subject,
            )
        return None
    lowered = value.lower()
    if lowered in {"true", "1", "yes", "y"}:
        return True
    if lowered in {"false", "0", "no", "n"}:
        return False
    collector.error(
        "FIELD_NOT_BOOL",
        f"{field} 必须是 true/false，实际为 {value!r}",
        file=file,
        row=line,
        field=field,
        subject=subject,
    )
    return None


def _require_text(
    value: str,
    *,
    field: str,
    file: str,
    line: int,
    collector: _Collector,
    subject: str,
) -> str | None:
    if value == "":
        collector.error(
            "FIELD_EMPTY",
            f"{field} 为空，无法追溯记录",
            file=file,
            row=line,
            field=field,
            subject=subject,
        )
        return None
    return value


def _load_projects(path: Path, collector: _Collector) -> tuple[dict[str, ProjectRecord], list[str]]:
    rows = _read_raw(path, PROJECT_FIELDS, collector)
    projects: dict[str, ProjectRecord] = {}
    order: list[str] = []
    for item in rows:
        cells: dict[str, str] = item["_cells"]
        line: int = item["_line"]
        pid = _require_text(
            cells["project_id"],
            field="project_id",
            file=path.name,
            line=line,
            collector=collector,
            subject="?",
        )
        subject = pid or f"line {line}"
        if pid is not None and pid in projects:
            collector.error(
                "PROJECT_DUPLICATE_ID",
                f"project_id {pid} 重复；无法确定采用哪一条",
                file=path.name,
                row=line,
                field="project_id",
                subject=pid,
            )
            continue
        district = _require_text(
            cells["district"],
            field="district",
            file=path.name,
            line=line,
            collector=collector,
            subject=subject,
        )
        work_type = _require_text(
            cells["work_type"],
            field="work_type",
            file=path.name,
            line=line,
            collector=collector,
            subject=subject,
        )
        if work_type is not None and work_type not in WORK_TYPES:
            collector.error(
                "PROJECT_UNKNOWN_WORK",
                f"work_type {work_type!r} 不在支持列表 {', '.join(WORK_TYPES)} 中",
                file=path.name,
                row=line,
                field="work_type",
                subject=subject,
            )
        year = _parse_int(
            cells["year"],
            field="year",
            file=path.name,
            line=line,
            collector=collector,
            subject=subject,
            minimum=2000,
        )
        estimate_raw = cells["estimate"]
        confirmed_raw = cells["confirmed_cost"]
        estimate = _parse_int(
            estimate_raw,
            field="estimate",
            file=path.name,
            line=line,
            collector=collector,
            subject=subject,
            required=False,
        )
        confirmed = _parse_int(
            confirmed_raw,
            field="confirmed_cost",
            file=path.name,
            line=line,
            collector=collector,
            subject=subject,
            required=False,
        )
        contracted = _parse_bool(
            cells["contracted"],
            field="contracted",
            file=path.name,
            line=line,
            collector=collector,
            subject=subject,
        )
        if estimate_raw == "" and confirmed_raw == "":
            collector.error(
                "PROJECT_NO_COST",
                "没有任何成本依据（估算与核定值均为空），无法判断可执行性",
                file=path.name,
                row=line,
                field="estimate",
                subject=subject,
            )
        elif estimate is not None and confirmed is not None and estimate != confirmed:
            collector.warning(
                "COST_REVISION_PRESENT",
                f"估算 {estimate} 与核定值 {confirmed} 不一致；两个值都会保留，"
                "实际采用哪个取决于情景的成本口径",
                file=path.name,
                row=line,
                field="confirmed_cost",
                subject=subject,
            )
        if contracted is True and confirmed_raw == "":
            collector.warning(
                "CONTRACT_WITHOUT_CONFIRMED_COST",
                "已签约但缺少核定成本；签约承诺无法按确定金额核对",
                file=path.name,
                row=line,
                field="confirmed_cost",
                subject=subject,
            )
        if (
            pid is None
            or district is None
            or work_type is None
            or year is None
            or contracted is None
            or (estimate is None and confirmed is None)
        ):
            # A project with no usable cost figure must not enter the portfolio;
            # treating it as zero would understate the budget.
            continue
        projects[pid] = ProjectRecord(
            project_id=pid,
            district=district,
            work_type=work_type,
            estimate=estimate,
            confirmed_cost=confirmed,
            year=year,
            contracted=contracted,
            source=SourceRef(path.name, line),
        )
        order.append(pid)
    return projects, order


def _load_funding(path: Path, collector: _Collector) -> list[FundingLine]:
    rows = _read_raw(path, FUNDING_FIELDS, collector)
    seen: dict[tuple[str, int], int] = {}
    lines: list[FundingLine] = []
    for item in rows:
        cells: dict[str, str] = item["_cells"]
        line: int = item["_line"]
        fund_id = _require_text(
            cells["fund_id"],
            field="fund_id",
            file=path.name,
            line=line,
            collector=collector,
            subject="?",
        )
        subject = fund_id or f"line {line}"
        year = _parse_int(
            cells["year"],
            field="year",
            file=path.name,
            line=line,
            collector=collector,
            subject=subject,
            minimum=2000,
        )
        category = _require_text(
            cells["category"],
            field="category",
            file=path.name,
            line=line,
            collector=collector,
            subject=subject,
        )
        cap = _parse_int(
            cells["cap"],
            field="cap",
            file=path.name,
            line=line,
            collector=collector,
            subject=subject,
            minimum=0,
        )
        allowed_raw = cells["allowed_work"]
        if allowed_raw == "":
            collector.error(
                "FIELD_EMPTY",
                "allowed_work 为空；无法判断该资金可支持的工程类别",
                file=path.name,
                row=line,
                field="allowed_work",
                subject=subject,
            )
            allowed: tuple[str, ...] = ()
        else:
            allowed = tuple(part.strip() for part in allowed_raw.split(";") if part.strip())
            unknown = [part for part in allowed if part not in WORK_TYPES]
            if unknown:
                collector.warning(
                    "FUND_UNKNOWN_WORK",
                    f"allowed_work 含未支持类别: {', '.join(unknown)}",
                    file=path.name,
                    row=line,
                    field="allowed_work",
                    subject=subject,
                )
            if not allowed:
                collector.error(
                    "FUND_NO_ALLOWED_WORK",
                    "allowed_work 没有有效类别；该资金无法支持任何工程",
                    file=path.name,
                    row=line,
                    field="allowed_work",
                    subject=subject,
                )
        if fund_id is not None and year is not None:
            key = (fund_id, year)
            if key in seen:
                collector.error(
                    "FUND_DUPLICATE",
                    f"资金来源 {fund_id} 在 {year} 年有重复记录（行 {seen[key]} 与 {line}）；上限口径不唯一",
                    file=path.name,
                    row=line,
                    field="fund_id",
                    subject=fund_id,
                )
            else:
                seen[key] = line
        if fund_id is None or year is None or category is None or cap is None:
            continue
        lines.append(
            FundingLine(
                fund_id=fund_id,
                year=year,
                category=category,
                cap=cap,
                allowed_work=allowed,
                source=SourceRef(path.name, line),
            )
        )
    return lines


def _load_dependencies(
    path: Path, projects: dict[str, ProjectRecord], collector: _Collector
) -> list[Dependency]:
    rows = _read_raw(path, DEPENDENCY_FIELDS, collector, allow_empty=True)
    seen: dict[tuple[str, str], int] = {}
    dependencies: list[Dependency] = []
    for item in rows:
        cells: dict[str, str] = item["_cells"]
        line: int = item["_line"]
        pred = _require_text(
            cells["predecessor"],
            field="predecessor",
            file=path.name,
            line=line,
            collector=collector,
            subject="?",
        )
        succ = _require_text(
            cells["successor"],
            field="successor",
            file=path.name,
            line=line,
            collector=collector,
            subject=pred or "?",
        )
        subject = f"{pred or '?'}->{succ or '?'}"
        for role, pid in (("predecessor", pred), ("successor", succ)):
            if pid is not None and pid not in projects:
                collector.error(
                    "DEP_UNKNOWN_PROJECT",
                    f"{role} {pid} 不在项目表中",
                    file=path.name,
                    row=line,
                    field=role,
                    subject=subject,
                )
        if pred is not None and pred == succ:
            collector.error(
                "DEP_SELF",
                f"项目 {pred} 依赖自身",
                file=path.name,
                row=line,
                field="predecessor",
                subject=subject,
            )
        shared = _parse_bool(
            cells["shared_site"],
            field="shared_site",
            file=path.name,
            line=line,
            collector=collector,
            subject=subject,
        )
        repeat_cost = _parse_int(
            cells["repeat_dig_cost"],
            field="repeat_dig_cost",
            file=path.name,
            line=line,
            collector=collector,
            subject=subject,
            required=shared is True,
            minimum=0,
        )
        if shared is False and repeat_cost not in (None, 0):
            collector.warning(
                "DEP_REPEAT_COST_WITHOUT_SHARED_SITE",
                f"shared_site=false 但 repeat_dig_cost={repeat_cost}，记录自相矛盾；按无重复开挖成本处理",
                file=path.name,
                row=line,
                field="repeat_dig_cost",
                subject=subject,
            )
        if shared is True and repeat_cost == 0:
            collector.warning(
                "DEP_SHARED_SITE_ZERO_REPEAT_COST",
                "shared_site=true 但 repeat_dig_cost=0；请确认是否确实没有重复开挖成本",
                file=path.name,
                row=line,
                field="repeat_dig_cost",
                subject=subject,
            )
        if pred is not None and succ is not None:
            key = (pred, succ)
            if key in seen:
                collector.error(
                    "DEP_DUPLICATE",
                    f"依赖关系重复（行 {seen[key]} 与 {line}）",
                    file=path.name,
                    row=line,
                    field="predecessor",
                    subject=subject,
                )
            else:
                seen[key] = line
        if pred is None or succ is None or shared is None:
            continue
        dependencies.append(
            Dependency(
                predecessor=pred,
                successor=succ,
                shared_site=shared,
                repeat_dig_cost=repeat_cost,
                source=SourceRef(path.name, line),
            )
        )
    return dependencies


def _load_needs(path: Path, collector: _Collector) -> list[NeedRecord]:
    rows = _read_raw(path, NEED_FIELDS, collector, allow_empty=True)
    seen: dict[tuple[str, str, str], int] = {}
    needs: list[NeedRecord] = []
    for item in rows:
        cells: dict[str, str] = item["_cells"]
        line: int = item["_line"]
        district = _require_text(
            cells["district"],
            field="district",
            file=path.name,
            line=line,
            collector=collector,
            subject="?",
        )
        group = _require_text(
            cells["group"],
            field="group",
            file=path.name,
            line=line,
            collector=collector,
            subject=district or "?",
        )
        code = _require_text(
            cells["need_code"],
            field="need_code",
            file=path.name,
            line=line,
            collector=collector,
            subject=district or "?",
        )
        subject = f"{district or '?'}/{group or '?'}/{code or '?'}"
        households = _parse_int(
            cells["affected_households"],
            field="affected_households",
            file=path.name,
            line=line,
            collector=collector,
            subject=subject,
            minimum=1,
        )
        confirmed = _parse_bool(
            cells["confirmed"],
            field="confirmed",
            file=path.name,
            line=line,
            collector=collector,
            subject=subject,
            required=False,
        )
        if confirmed is None:
            collector.warning(
                "NEED_CONFIRMATION_UNKNOWN",
                "confirmed 为空；该需求不计入已确认需求，也不作为受益依据",
                file=path.name,
                row=line,
                field="confirmed",
                subject=subject,
            )
        if district is not None and group is not None and code is not None:
            key = (district, group, code)
            if key in seen:
                collector.error(
                    "NEED_DUPLICATE",
                    f"同一片区/群体/需求编码重复（行 {seen[key]} 与 {line}），户数会重复计算",
                    file=path.name,
                    row=line,
                    field="need_code",
                    subject=subject,
                )
            else:
                seen[key] = line
        if district is None or group is None or code is None or households is None:
            continue
        needs.append(
            NeedRecord(
                district=district,
                group=group,
                need_code=code,
                affected_households=households,
                confirmed=confirmed,
                source=SourceRef(path.name, line),
            )
        )
    return needs


def load_dataset(data_dir: str | Path) -> Dataset:
    """Load and validate every source table.

    Validation never repairs a row silently: an empty cell stays *unknown* and
    is reported, because a missing cost or dependency flag must not be read as
    zero/false by a budget calculation.
    """
    root = Path(data_dir)
    collector = _Collector()
    base_paths = {
        "projects": root / "projects.csv",
        "funding": root / "funding.csv",
        "dependencies": root / "dependencies.csv",
        "needs": root / "needs.csv",
    }

    projects, order = _load_projects(base_paths["projects"], collector)
    funding = _load_funding(base_paths["funding"], collector)
    dependencies = _load_dependencies(base_paths["dependencies"], projects, collector)
    needs = _load_needs(base_paths["needs"], collector)

    _portfolio_checks(projects, funding, dependencies, needs, collector)

    inputs = []
    for name, path in base_paths.items():
        if path.is_file():
            inputs.append(
                {"file": name + ".csv", "sha256": _sha256(path), "bytes": path.stat().st_size}
            )

    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "inputs": inputs,
    }
    return Dataset(
        projects=projects,
        project_order=order,
        funding=funding,
        dependencies=dependencies,
        needs=needs,
        issues=collector.issues,
        snapshot=snapshot,
    )


def _portfolio_checks(
    projects: dict[str, ProjectRecord],
    funding: list[FundingLine],
    dependencies: list[Dependency],
    needs: list[NeedRecord],
    collector: _Collector,
) -> None:
    if not projects:
        collector.error("NO_PROJECTS", "项目表为空，无法形成方案")
        return
    if not funding:
        collector.error("NO_FUNDING", "资金表为空，无法判断年度上限")
    years = {line.year for line in funding}
    for pid, project in projects.items():
        if years and project.year not in years:
            collector.warning(
                "PROJECT_YEAR_UNFUNDED",
                f"项目年度 {project.year} 没有对应资金上限记录",
                file=project.source.file,
                row=project.source.row,
                field="year",
                subject=pid,
            )
        if funding and not any(project.work_type in line.allowed_work for line in funding):
            # Funding can be amended by a scenario, so the base table warning is
            # not fatal; the scenario layer re-checks the effective envelopes.
            collector.warning(
                "PROJECT_UNFUNDABLE_TYPE",
                f"现有资金没有任何一条允许 {project.work_type} 工程；除非情景新增或调整资金，项目无法安排",
                file=project.source.file,
                row=project.source.row,
                field="work_type",
                subject=pid,
            )
    # dependency graph: cycles are fatal for scheduling.
    graph: dict[str, list[str]] = {pid: [] for pid in projects}
    for dep in dependencies:
        if dep.predecessor in graph and dep.successor in projects:
            graph[dep.predecessor].append(dep.successor)
    state: dict[str, int] = {}

    def visit(node: str, path: list[str]) -> None:
        state[node] = 1
        path.append(node)
        for nxt in graph.get(node, ()):
            if state.get(nxt) == 1:
                cycle = " -> ".join(path[path.index(nxt):] + [nxt])
                collector.error(
                    "DEP_CYCLE",
                    f"依赖关系成环: {cycle}；无法排出可执行顺序",
                    subject=cycle,
                )
            elif state.get(nxt, 0) == 0:
                visit(nxt, path)
        path.pop()
        state[node] = 2

    for pid in projects:
        if state.get(pid, 0) == 0:
            visit(pid, [])

    project_districts = {p.district for p in projects.values()}
    needs_districts = {n.district for n in needs}
    for district in sorted(project_districts - needs_districts):
        collector.warning(
            "DISTRICT_NO_NEED_RECORD",
            f"片区 {district} 有项目但没有需求记录；无法判断受益是否匹配民生需求",
            subject=district,
        )
    for district in sorted(needs_districts):
        confirmed = [n for n in needs if n.district == district and n.confirmed is True]
        if not confirmed:
            collector.warning(
                "DISTRICT_NO_CONFIRMED_NEED",
                f"片区 {district} 没有已确认需求；受益均衡指标将无法计算该片区",
                subject=district,
            )
