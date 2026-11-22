# 情景格式

情景是建立在原始输入表之上的、具名且可复现的一组假设。它不会修改 CSV；成本修订、资金调整和新增资金都记录在报告中。

## 内置情景

```
renewal-analytics list-scenarios
```

| 名称 | 成本口径 | 要点 |
| --- | --- | --- |
| `confirmed_costs` | confirmed | 基准；依赖工程因前置未在更早年度完成而被阻断 |
| `estimates` | estimate | 与核定值口径对照；未核定成本使结果暂定 |
| `upper_bound` | upper_bound | 已知成本取高值的最坏情况 |
| `next_year` | confirmed | 成本核定重排、新增 2028 资金、跨年重复开挖 |
| `same_year_sequence` | confirmed | 显式允许同年度接续，结果暂定 |
| `budget_cut` | confirmed | 资金收紧后已签约项目无法安排，显式冲突 |

## 自定义情景文件

```json
{
  "name": "next_year_review",
  "description": "说明这次修订的背景",
  "cost_basis": "confirmed",
  "strategies": ["benefit", "cost", "district", "sequence"],
  "approved_strategy": "benefit",
  "allow_same_year_predecessor": false,
  "search_node_limit": 300000,
  "search_time_limit_seconds": 2.5,
  "cost_revisions": [
    {
      "project_id": "P-02",
      "confirmed_cost": 380000,
      "as_of": "2027-02-15",
      "reason": "施工图预算核定，高于原估算",
      "source": "cost-review-2027-02/P-02"
    }
  ],
  "funding_adjustments": [
    {"fund_id": "F-01", "year": 2027, "cap": 1000000}
  ],
  "funding_additions": [
    {
      "fund_id": "F-04",
      "year": 2028,
      "category": "drainage",
      "cap": 500000,
      "allowed_work": "pipe;street"
    }
  ]
}
```

### 字段规则

- `cost_basis`：`confirmed` / `estimate` / `upper_bound`。
- `strategies`：非空；取值 `benefit` / `cost` / `district` / `sequence`。
- `approved_strategy`：必须是 `strategies` 之一。
- `cost_revisions`：每条必须含 `project_id`、`confirmed_cost`、`as_of`、`reason`、`source`；缺失即拒绝加载。
- `funding_adjustments`：`(fund_id, year)` 必须唯一匹配现有资金记录，只调整 `cap`（也可覆盖 `allowed_work`）。
- `funding_additions`：新增资金不能与现有 `(fund_id, year)` 重复。
- `search_node_limit` / `search_time_limit_seconds`：搜索预算，任一触发即标为 `SEARCH_INCOMPLETE`。
- `allow_same_year_predecessor`：默认 `false`；设为 `true` 时方案至少为“暂定”。

## 与“当时依据”比较

```bash
# 先保存一份批准方案
renewal-analytics run --scenario confirmed_costs --out reports

# 成本或资金后来变化后，与冻结报告比较
renewal-analytics compare --before-report reports/confirmed_costs.json --after next_year
```

冻结报告包含输入文件 sha256、有效成本、修订日志和逐项理由，因此比较结果可以回溯到当时的预算口径、项目依赖和需求记录。
