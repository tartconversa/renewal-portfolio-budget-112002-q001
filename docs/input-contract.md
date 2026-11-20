# Input material

All records are synthetic and refer to no real household, project, or city. A file is a source extract, not a precomputed answer. Preserve source identifiers and revisions when interpreting it.

- `funding.csv`: CSV; columns `fund_id, year, category, cap, allowed_work`. Dates use ISO 8601 where present; blank cells mean unknown or not supplied, not numeric zero.
- `projects.csv`: CSV; columns `project_id, district, work_type, estimate, confirmed_cost, year, contracted`. Dates use ISO 8601 where present; blank cells mean unknown or not supplied, not numeric zero.
- `dependencies.csv`: CSV; columns `predecessor, successor, shared_site, repeat_dig_cost`. Dates use ISO 8601 where present; blank cells mean unknown or not supplied, not numeric zero.
- `needs.csv`: CSV; columns `district, group, need_code, affected_households, confirmed`. Dates use ISO 8601 where present; blank cells mean unknown or not supplied, not numeric zero.
