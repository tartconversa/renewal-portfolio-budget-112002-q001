# 合成输入材料

这里的记录均为合成数据，不对应真实住户、项目或城市。每个文件表示一个来源数据表，不包含分析结论；解释记录时应保留来源标识和修订关系。

- `funding.csv`：CSV 表格，字段为 `fund_id, year, category, cap, allowed_work`。日期字段使用 ISO 8601 格式；空单元格表示未知或未提供，不代表数值零。
- `projects.csv`：CSV 表格，字段为 `project_id, district, work_type, estimate, confirmed_cost, year, contracted`。日期字段使用 ISO 8601 格式；空单元格表示未知或未提供，不代表数值零。
- `dependencies.csv`：CSV 表格，字段为 `predecessor, successor, shared_site, repeat_dig_cost`。日期字段使用 ISO 8601 格式；空单元格表示未知或未提供，不代表数值零。
- `needs.csv`：CSV 表格，字段为 `district, group, need_code, affected_households, confirmed`。日期字段使用 ISO 8601 格式；空单元格表示未知或未提供，不代表数值零。
