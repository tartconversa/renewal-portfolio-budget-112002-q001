# 合成输入材料

这里的记录均为合成数据，不对应真实住户、项目或城市。每个文件表示一个来源数据表，不包含分析结论；解释记录时应保留来源标识和修订关系。

- `funding.csv`：CSV 表格，字段为 `fund_id, year, category, cap, allowed_work`。日期字段使用 ISO 8601 格式；空单元格表示未知或未提供，不代表数值零。
- `projects.csv`：CSV 表格，字段为 `project_id, district, work_type, estimate, confirmed_cost, year, contracted`。日期字段使用 ISO 8601 格式；空单元格表示未知或未提供，不代表数值零。
- `dependencies.csv`：CSV 表格，字段为 `predecessor, successor, shared_site, repeat_dig_cost`。日期字段使用 ISO 8601 格式；空单元格表示未知或未提供，不代表数值零。
- `needs.csv`：CSV 表格，字段为 `district, group, need_code, affected_households, confirmed`。日期字段使用 ISO 8601 格式；空单元格表示未知或未提供，不代表数值零。
- `dependencies.csv`：`shared_site` 为 `true` 时 `repeat_dig_cost` 必须给出；为 `false` 时若给出非零重复开挖成本会提示矛盾。
- `dependencies.csv` 在没有依赖记录时允许只有表头；`needs.csv` 在没有需求记录时允许只有表头，但相关片区会被标记为需求口径未知。

## 空值与错误

- `estimate` 与 `confirmed_cost` 至少一个非空；两者都为空的项目无法判断可执行性，报错且不进入组合测算。
- 关键标志（`contracted`、`shared_site`）为空时报错，不默认为 `false`。
- 同一 `project_id`、同一 `(fund_id, year)`、同一 `(district, group, need_code)` 重复均报错，避免预算或受益重复计算。
