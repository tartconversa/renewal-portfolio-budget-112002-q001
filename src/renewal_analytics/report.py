"""Human- and machine-readable reports.

The Markdown report is the hand-off artefact for executing departments: every
row carries its source reference, the reason for inclusion/deferral/rejection,
and the funding line it consumes.  The JSON report is the same data in full.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .model import Issue
from .planner import STATUS_LABELS, STRATEGY_LABELS, ScenarioAnalysis

REJECTION_LABELS = {
    "UNFUNDABLE_TYPE": "工程类别无资金可用",
    "COST_EXCEEDS_EACH_LINE": "单项成本超过可用资金单条上限",
    "NO_LINE_FOR_REQUESTED_YEAR": "请求年度及之后无可用资金",
    "PREDECESSOR_UNSCHEDULED": "前置工程未入选",
    "PREDECESSOR_TIMING": "前置工程时间不早于依赖工程",
    "PREDECESSOR_NOT_COMPLETED": "前置工程未在依赖工程前完成",
    "CONTRACT_CONFLICT": "已签约但无资金可安排",
    "NO_ADDITIONAL_CONFIRMED_BENEFIT": "同片区已覆盖，新增小包不增加已确认受益",
    "NO_REMAINING_CAPACITY": "可用资金剩余额度不足",
    "LOWER_PRIORITY": "当前策略下优先级较低",
}

DEPENDENCY_LABELS = {
    "BOTH_UNSCHEDULED": "均未安排",
    "BLOCKED": "依赖已入选但前置未入选（不可计入交付）",
    "SUCCESSOR_UNSCHEDULED": "前置入选、依赖延期",
    "SATISFIED_SAME_YEAR": "同年度接续满足",
    "SATISFIED_CROSS_YEAR": "跨年度满足",
    "VIOLATED_TIMING": "时间顺序不满足",
}


def _blocking_reason(plan) -> str:
    totals = plan.totals
    if plan.status == "PROVISIONAL":
        reasons = []
        if totals.get("unconfirmed_cost_projects"):
            reasons.append(
                "成本未核定：" + ", ".join(totals["unconfirmed_cost_projects"])
            )
        if totals.get("demand_unknown_districts"):
            reasons.append(
                "需求口径未知：" + ", ".join(totals["demand_unknown_districts"])
            )
        if totals.get("same_year_assumption"):
            reasons.append("使用了同年度接续假设")
        return "；".join(reasons) or "存在尚需确认的假设"
    if plan.status == "INVALID_INPUT":
        return "输入校验未通过"
    if plan.status == "INFEASIBLE_COMMITMENT":
        names = ", ".join(totals.get("unplaced_contracted") or []) or "已签约项目"
        return f"已签约项目无法全部安排：{names}"
    if plan.status == "SEARCH_INCOMPLETE":
        return "搜索达到节点或时间上限，未证明主目标最优"
    return "见策略状态"


def _money(value: int | None) -> str:
    if value is None:
        return "—"
    return f"{value:,}"


def _issue_line(issue: Issue) -> str:
    where = ""
    if issue.file:
        where = f"（{issue.file}"
        if issue.row:
            where += f" 第 {issue.row} 行"
        where += "）"
    subject = f" [{issue.subject}]" if issue.subject else ""
    return f"- `{issue.level}` / `{issue.code}`{subject}{where}：{issue.message}"


def render_markdown(analysis: ScenarioAnalysis) -> str:
    plan = analysis.approved
    lines: list[str] = []
    lines.append(f"# 年度更新组合测算：{analysis.scenario}")
    lines.append("")
    lines.append(f"- 方案状态：**{STATUS_LABELS.get(analysis.status, analysis.status)}**")
    if plan:
        lines.append(f"- 已批准策略：{STRATEGY_LABELS.get(plan.strategy, plan.strategy)}")
        lines.append(f"- 是否可执行：{'是' if analysis.executable else '否（见下）'}")
        if not analysis.executable:
            lines.append(f"- 未执行原因：{_blocking_reason(plan)}")
    lines.append("")
    lines.append("## 输入校验")
    validation = analysis.validation
    if validation["errors"]:
        lines.append("### 错误（存在错误时不给可执行方案）")
        lines.extend(_issue_line(Issue(**row)) for row in validation["errors"])
    else:
        lines.append("输入无错误。")
    if validation["warnings"]:
        lines.append("")
        lines.append("### 警告")
        lines.extend(_issue_line(Issue(**row)) for row in validation["warnings"])
    lines.append("")

    if plan is None:
        lines.append("## 方案")
        lines.append("输入存在错误，未执行组合测算。请先修正上列问题。")
        lines.append("")
        return "\n".join(lines)

    totals = plan.totals
    lines.append("## 批准确认方案")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 入选项目 | {totals.get('selected_count')} / {totals.get('requested_count')} |")
    lines.append(f"| 总成本（含重复开挖） | {_money(totals.get('total_cost'))} |")
    lines.append(f"| 其中工程成本 | {_money(totals.get('work_cost'))} |")
    lines.append(f"| 其中重复开挖成本 | {_money(totals.get('repeat_dig_cost'))} |")
    lines.append(f"| 全部资金上限 | {_money(totals.get('total_cap'))} |")
    pressure = totals.get("budget_pressure")
    lines.append(
        "| 资金压力（申请成本 / 上限） | {} |".format(
            f"{pressure * 100:.1f}%" if pressure is not None else "—"
        )
    )
    lines.append(
        f"| 未获安排成本（含未立项） | {_money(totals.get('unfunded_cost'))} |"
    )
    lines.append(
        f"| 已确认需求户数覆盖率 | "
        f"{(totals.get('weighted_coverage') or 0) * 100:.1f}% "
        f"（{_money(totals.get('covered_households'))} / {_money(totals.get('confirmed_demand_households'))} 户） |"
    )
    lines.append(f"| 覆盖片区 | {', '.join(totals.get('covered_districts') or []) or '—'} |")
    lines.append(f"| 未覆盖片区 | {', '.join(totals.get('uncovered_districts') or []) or '—'} |")
    lines.append(f"| 已签约但未安排 | {', '.join(totals.get('unplaced_contracted') or []) or '无'} |")
    lines.append(f"| 成本未核定入选项目 | {', '.join(totals.get('unconfirmed_cost_projects') or []) or '无'} |")
    lines.append("")
    lines.append("## 项目安排与理由")
    lines.append("")
    lines.append("| 项目 | 片区 | 类别 | 年度 | 资金来源 | 工程成本 | 重复开挖 | 合计 | 成本依据 | 来源 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for pid in sorted(plan.placements):
        placement = plan.placements[pid]
        project = analysis_snapshot_project(analysis, pid)
        lines.append(
            "| {pid} | {district} | {work} | {year} | {fund}（{fund_year}） | {work_cost} | {dig} | {total} | {basis}{unconf} | {src} |".format(
                pid=pid,
                district=project.get("district", "—"),
                work=project.get("work_type", "—"),
                year=placement.year,
                fund=placement.fund_id,
                fund_year=placement.line.year,
                work_cost=_money(placement.work_cost),
                dig=_money(placement.repeat_dig_cost),
                total=_money(placement.total_cost),
                basis=placement.cost.basis,
                unconf="（未核定）" if placement.cost.unconfirmed else "",
                src=_source(project.get("source")),
            )
        )
    lines.append("")
    lines.append("### 入选理由")
    lines.append("")
    for pid in sorted(plan.placements):
        lines.append(f"- **{pid}**：{plan.placements[pid].reason}")
    lines.append("")
    lines.append("## 未采纳项目及原因")
    lines.append("")
    if not plan.rejections:
        lines.append("无。")
    else:
        lines.append("| 项目 | 原因 | 说明 | 来源 |")
        lines.append("| --- | --- | --- | --- |")
        for pid in sorted(plan.rejections):
            rejection = plan.rejections[pid]
            project = analysis_snapshot_project(analysis, pid)
            lines.append(
                "| {pid} | {label} | {message} | {src} |".format(
                    pid=pid,
                    label=REJECTION_LABELS.get(rejection.code, rejection.code),
                    message=rejection.message,
                    src=_source(rejection.detail.get("source") or project.get("source")),
                )
            )
    lines.append("")
    lines.append("## 依赖与共享施工范围")
    lines.append("")
    if not plan.dependencies:
        lines.append("无依赖记录。")
    else:
        lines.append("| 前置 | 依赖 | 共享施工面 | 重复开挖成本 | 前置年度 | 依赖年度 | 状态 | 说明 | 来源 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
        for status in plan.dependencies:
            lines.append(
                "| {p} | {s} | {shared} | {cost} | {py} | {sy} | {st} | {detail} | {src} |".format(
                    p=status.predecessor,
                    s=status.successor,
                    shared="是" if status.shared_site else "否",
                    cost=_money(status.repeat_dig_cost),
                    py=status.predecessor_year if status.predecessor_year else "—",
                    sy=status.successor_year if status.successor_year else "—",
                    st=DEPENDENCY_LABELS.get(status.status, status.status),
                    detail=status.detail,
                    src=_source(status.source),
                )
            )
    lines.append("")
    lines.append("## 资金使用")
    lines.append("")
    lines.append("| 来源 | 年度 | 类别 | 允许工程 | 上限 | 已用 | 剩余 | 项目 | 来源行 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in plan.line_usage:
        lines.append(
            "| {f} | {y} | {c} | {w} | {cap} | {used} | {rem}{over} | {p} | {src} |".format(
                f=row["fund_id"],
                y=row["year"],
                c=row["category"],
                w=";".join(row["allowed_work"]),
                cap=_money(row["cap"]),
                used=_money(row["used"]),
                rem=_money(row["remaining"]),
                over=" **超限**" if row["over_budget"] else "",
                p=", ".join(row["projects"]) or "—",
                src=_source(row["source"]),
            )
        )
    lines.append("")
    lines.append("## 片区受益与均衡")
    lines.append("")
    lines.append("| 片区 | 已确认需求户数 | 需求口径 | 是否覆盖 | 项目数 | 项目 | 安排成本 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for row in plan.districts:
        reason = {
            "ok": "已确认需求",
            "no_confirmed_need": "无已确认需求",
            "no_need_record": "无需求记录",
        }.get(row["demand_reason"], row["demand_reason"])
        lines.append(
            "| {d} | {h} | {r} | {c} | {n} | {p} | {cost} |".format(
                d=row["district"],
                h=_money(row["confirmed_need_households"]) if row["demand_known"] else "未知",
                r=reason,
                c="是" if row["covered"] else "否",
                n=row["selected_project_count"],
                p=", ".join(row["selected_projects"]) or "—",
                cost=_money(row["scheduled_cost"]),
            )
        )
    flags = totals.get("balance_flags") or []
    if flags:
        lines.append("")
        lines.append("### 均衡风险提示")
        for flag in flags:
            lines.append(f"- `{flag['code']}`：{flag['message']}")
    lines.append("")
    lines.append("## 冲突")
    lines.append("")
    if not plan.conflicts:
        lines.append("无。")
    else:
        for conflict in plan.conflicts:
            lines.append(f"- `{conflict['code']}`：{conflict['message']}")
    lines.append("")
    lines.append("## 策略比较")
    lines.append("")
    lines.append("| 策略 | 状态 | 入选数 | 总成本 | 覆盖片区 | 覆盖户数 | 户数覆盖率 | 已签约未安排 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in analysis.comparison["strategies"]:
        lines.append(
            "| {label} | {status} | {n} | {cost} | {d} | {h} | {cov} | {u} |".format(
                label=row["label"],
                status=STATUS_LABELS.get(row["status"], row["status"]),
                n=row["selected_count"],
                cost=_money(row["total_cost"]),
                d=", ".join(row["covered_districts"] or []) or "—",
                h=_money(row["covered_households"]),
                cov=f"{(row['weighted_coverage'] or 0) * 100:.1f}%",
                u=", ".join(row["unplaced_contracted"] or []) or "无",
            )
        )
    lines.append("")
    lines.append("## 方法说明")
    for note in plan.notes:
        lines.append(f"- {note}")
    lines.append("")
    lines.append("## 变更与依据链")
    if analysis.revision_log:
        lines.append("")
        lines.append("| # | 类型 | 对象 | 原值 | 新值 | 依据 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for entry in analysis.revision_log:
            kind = entry.get("kind", "cost_revision")
            if kind == "cost_revision":
                lines.append(
                    "| {seq} | 成本核定 | {pid} | estimate={old} / confirmed={oldc} | confirmed={newc} | {reason}（{as_of}，{evidence}） |".format(
                        seq=entry["sequence"],
                        pid=entry["project_id"],
                        old=entry["previous"]["estimate"],
                        oldc=entry["previous"]["confirmed_cost"],
                        newc=entry["updated"]["confirmed_cost"],
                        reason=entry["reason"],
                        as_of=entry["as_of"],
                        evidence=entry["evidence"],
                    )
                )
            elif kind == "funding_cap":
                lines.append(
                    "| {seq} | 资金上限调整 | {f}/{y} | {old} | {new} | {src} |".format(
                        seq=entry["sequence"],
                        f=entry["fund_id"],
                        y=entry["year"],
                        old=_money(entry["previous"]["cap"]),
                        new=_money(entry["updated"]["cap"]),
                        src=_source(entry["updated"]["scenario_source"]),
                    )
                )
            else:
                lines.append(
                    "| {seq} | 新增资金来源 | {f}/{y} | — | cap={cap} | {src} |".format(
                        seq=entry["sequence"],
                        f=entry["fund_id"],
                        y=entry["year"],
                        cap=_money(entry["updated"]["cap"]),
                        src=_source(entry["updated"]["source"]),
                    )
                )
    else:
        lines.append("")
        lines.append("本情景未引入成本或资金修订，全部依据来自原始输入表。")
    lines.append("")
    lines.append("## 追溯信息")
    lines.append("")
    for item in analysis.snapshot.get("inputs", []):
        lines.append(f"- `{item['file']}`：sha256 `{item['sha256'][:16]}…`，{item['bytes']} 字节")
    lines.append("")
    return "\n".join(lines)


def analysis_snapshot_project(analysis: ScenarioAnalysis, pid: str) -> dict[str, Any]:
    return analysis.snapshot.get("projects", {}).get(pid, {})


def _source(source: dict[str, Any] | None) -> str:
    if not source:
        return "—"
    return f"{source.get('file')}:{source.get('row')}"


def render_json(analysis: ScenarioAnalysis) -> str:
    return json.dumps(analysis.as_dict(), ensure_ascii=False, indent=2, sort_keys=False)


def write_reports(analysis: ScenarioAnalysis, out_dir: str | Path) -> dict[str, Path]:
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    json_path = target / f"{analysis.scenario}.json"
    md_path = target / f"{analysis.scenario}.md"
    json_path.write_text(render_json(analysis) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(analysis) + "\n", encoding="utf-8")
    return {"json": json_path, "markdown": md_path}
