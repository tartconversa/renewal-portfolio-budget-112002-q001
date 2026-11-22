"""Compare scenarios and export a reviewable change record.

Comparison operates on the *rendered analysis dict*, not on live data.  That
lets a frozen report (saved JSON) serve as the baseline: a later run is compared
against the basis that was actually approved at the time, even if the source
CSV files or cost figures have since changed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .planner import STATUS_LABELS, ScenarioAnalysis


def load_analysis_report(path: str | Path) -> dict[str, Any]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    required = {"scenario", "status", "approved_strategy", "plans", "validation", "snapshot"}
    missing = sorted(required - set(raw))
    if missing:
        raise ValueError(f"报告缺少字段: {', '.join(missing)}")
    return raw


def _approved_plan(report: dict[str, Any]) -> dict[str, Any] | None:
    name = report.get("approved_strategy")
    return report.get("plans", {}).get(name)


def _rejection_map(plan: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not plan:
        return {}
    return {
        pid: {"code": row["code"], "message": row["message"]}
        for pid, row in plan.get("rejections", {}).items()
    }


def _placement_map(plan: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not plan:
        return {}
    return {
        pid: {
            "year": row["year"],
            "fund_id": row["fund_id"],
            "total_cost": row["total_cost"],
            "basis": row["cost_basis"],
            "unconfirmed": row["cost_figure"]["unconfirmed"],
        }
        for pid, row in plan.get("placements", {}).items()
    }


def compare_reports(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    a = _approved_plan(before)
    b = _approved_plan(after)
    ma, mb = _placement_map(a), _placement_map(b)
    ra, rb = _rejection_map(a), _rejection_map(b)
    added = sorted(set(mb) - set(ma))
    removed = sorted(set(ma) - set(mb))
    moved = []
    recosted = []
    for pid in sorted(set(ma) & set(mb)):
        if (ma[pid]["year"], ma[pid]["fund_id"]) != (mb[pid]["year"], mb[pid]["fund_id"]):
            moved.append({"project_id": pid, "from": ma[pid], "to": mb[pid]})
        if ma[pid]["total_cost"] != mb[pid]["total_cost"]:
            recosted.append(
                {
                    "project_id": pid,
                    "from": ma[pid]["total_cost"],
                    "to": mb[pid]["total_cost"],
                    "basis_from": ma[pid]["basis"],
                    "basis_to": mb[pid]["basis"],
                }
            )
    reasons_changed = [
        {"project_id": pid, "from": ra[pid], "to": rb[pid]}
        for pid in sorted(set(ra) & set(rb))
        if ra[pid]["code"] != rb[pid]["code"]
    ]
    conflicts_a = (a or {}).get("conflicts", [])
    conflicts_b = (b or {}).get("conflicts", [])
    new_conflicts = [c for c in conflicts_b if c not in conflicts_a]
    resolved_conflicts = [c for c in conflicts_a if c not in conflicts_b]
    return {
        "from": before.get("scenario"),
        "to": after.get("scenario"),
        "from_status": before.get("status"),
        "to_status": after.get("status"),
        "from_status_label": STATUS_LABELS.get(before.get("status"), before.get("status")),
        "to_status_label": STATUS_LABELS.get(after.get("status"), after.get("status")),
        "added": [{"project_id": pid, **mb[pid]} for pid in added],
        "removed": [{"project_id": pid, **ma[pid]} for pid in removed],
        "moved": moved,
        "recosted": recosted,
        "reasons_changed": reasons_changed,
        "new_rejections": [
            {"project_id": pid, **rb[pid]} for pid in sorted(set(rb) - set(ra))
        ],
        "dropped_rejections": [
            {"project_id": pid, **ra[pid]} for pid in sorted(set(ra) - set(rb))
        ],
        "new_conflicts": new_conflicts,
        "resolved_conflicts": resolved_conflicts,
        "totals": {
            "from": (a or {}).get("totals"),
            "to": (b or {}).get("totals"),
        },
        "revision_log": {
            "from": before.get("revision_log", []),
            "to": after.get("revision_log", []),
        },
        "sources": {
            "from": before.get("snapshot", {}).get("inputs", []),
            "to": after.get("snapshot", {}).get("inputs", []),
        },
    }


def compare_analyses(left: ScenarioAnalysis, right: ScenarioAnalysis) -> dict[str, Any]:
    return compare_reports(left.as_dict(), right.as_dict())


def render_comparison(comparison: dict[str, Any]) -> str:
    lines = [
        f"# 方案比较：{comparison['from']} → {comparison['to']}",
        "",
        f"- 状态：{comparison['from_status_label']} → {comparison['to_status_label']}",
        "",
    ]
    if comparison.get("removed"):
        lines.append("## 退出安排")
        for row in comparison["removed"]:
            lines.append(
                f"- {row['project_id']}：原安排 {row['year']} / {row['fund_id']}，成本 {row['total_cost']:,}"
            )
        lines.append("")
    if comparison.get("added"):
        lines.append("## 新增安排")
        for row in comparison["added"]:
            lines.append(
                f"- {row['project_id']}：{row['year']} / {row['fund_id']}，成本 {row['total_cost']:,}"
            )
        lines.append("")
    if comparison.get("moved"):
        lines.append("## 年度或来源调整")
        for row in comparison["moved"]:
            lines.append(
                f"- {row['project_id']}：{row['from']['year']} / {row['from']['fund_id']} → "
                f"{row['to']['year']} / {row['to']['fund_id']}"
            )
        lines.append("")
    if comparison.get("recosted"):
        lines.append("## 成本变化")
        for row in comparison["recosted"]:
            lines.append(
                f"- {row['project_id']}：{row['from']:,}（{row['basis_from']}）→ "
                f"{row['to']:,}（{row['basis_to']}）"
            )
        lines.append("")
    if (
        comparison.get("reasons_changed")
        or comparison.get("new_rejections")
        or comparison.get("dropped_rejections")
    ):
        lines.append("## 未采纳原因变化")
        for row in comparison.get("reasons_changed", []):
            lines.append(f"- {row['project_id']}：{row['from']['code']} → {row['to']['code']}")
        for row in comparison.get("new_rejections", []):
            lines.append(f"- {row['project_id']}：新增原因 {row['code']}，{row['message']}")
        for row in comparison.get("dropped_rejections", []):
            lines.append(f"- {row['project_id']}：原因 {row['code']} 不再适用")
        lines.append("")
    if comparison.get("new_conflicts"):
        lines.append("## 新增冲突（受影响承诺）")
        for conflict in comparison["new_conflicts"]:
            lines.append(f"- `{conflict['code']}`：{conflict['message']}")
        lines.append("")
    if comparison.get("resolved_conflicts"):
        lines.append("## 已消解冲突")
        for conflict in comparison["resolved_conflicts"]:
            lines.append(f"- `{conflict['code']}`：{conflict['message']}")
        lines.append("")
    lines.append("## 依据变化")
    for side in ("from", "to"):
        entries = comparison.get("revision_log", {}).get(side, [])
        if not entries:
            lines.append(f"- {comparison[side]}：沿用原始依据，无修订")
            continue
        for entry in entries:
            if entry.get("kind", "cost_revision") == "cost_revision":
                lines.append(
                    f"- {comparison[side]} / {entry['project_id']}："
                    f"{entry['reason']}（{entry['as_of']}，{entry['evidence']}）"
                )
            elif entry.get("kind") == "funding_cap":
                lines.append(
                    f"- {comparison[side]} / {entry['fund_id']} {entry['year']}："
                    f"上限 {entry['previous']['cap']:,} → {entry['updated']['cap']:,}"
                )
            else:
                lines.append(
                    f"- {comparison[side]} / {entry['fund_id']} {entry['year']}："
                    f"新增资金来源，上限 {entry['updated']['cap']:,}"
                )
    lines.append("")
    lines.append("## 输入指纹")
    for side in ("from", "to"):
        sources = comparison.get("sources", {}).get(side, [])
        joined = ", ".join(
            f"{item['file']}:{item['sha256'][:12]}…" for item in sources
        ) or "—"
        lines.append(f"- {comparison[side]}：{joined}")
    lines.append("")
    return "\n".join(lines)
