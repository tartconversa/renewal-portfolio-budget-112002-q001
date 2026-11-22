# Renewal Portfolio Budget

城市更新年度组合测算工具。给定各片区项目的成本区间、资金来源及年度上限、工程类别、前置工程、共享施工范围和已确认民生需求，测算可执行的年度安排，并说明每个项目为何入选、延期或无法安排，同时输出可供执行部门和复核人员使用的依据链。

仅使用 Python 标准库，要求 Python 3.12 或更新版本。

## 快速开始

```bash
# 只校验输入，不生成方案（有错误时退出码 1）
PYTHONPATH=src python3 -m renewal_analytics.cli validate --data data

# 列出内置情景
PYTHONPATH=src python3 -m renewal_analytics.cli list-scenarios

# 测算一个情景，输出 Markdown 报告并保存
PYTHONPATH=src python3 -m renewal_analytics.cli run \
    --scenario next_year --out reports

# 自定义情景（成本修订、资金调整、新增资金）
PYTHONPATH=src python3 -m renewal_analytics.cli run \
    --scenario-file examples/scenario-next-year.json

# 比较两个情景；也可用冻结报告作为变更前基线
PYTHONPATH=src python3 -m renewal_analytics.cli compare \
    --before confirmed_costs --after next_year

PYTHONPATH=src python3 -m renewal_analytics.cli compare \
    --before-report reports/confirmed_costs.json --after next_year
```

安装为命令（可选）：

```bash
python3 -m pip install -e .
renewal-analytics run --scenario next_year
```

运行测试：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 退出码

| 码 | 含义 |
| --- | --- |
| 0 | 输入有效且批准方案可执行 |
| 1 | 输入存在错误，未给出可执行方案 |
| 2 | 输入有效，但方案暂定、搜索未完成或存在承诺冲突 |

搜索默认最多 2.5 秒 / 300000 节点；超限时状态为 `SEARCH_INCOMPLETE`，报告会说明方案未证明最优，不能直接执行。

## 内置情景

| 情景 | 用途 |
| --- | --- |
| `confirmed_costs` | 基准：按核定成本，前置工程须早于依赖工程；演示依赖阻断 |
| `estimates` | 按估算口径，存在未核定成本，结果为“暂定” |
| `upper_bound` | 每项取已知成本较高值的最坏情况 |
| `next_year` | 成本核定值更新后重排；新增 2028 年资金；演示跨年重复开挖 |
| `same_year_sequence` | 显式允许同年度“前置先完成、依赖后接续”，结果标记暂定 |
| `budget_cut` | 资金上限收紧；已签约项目无资金可容纳时显式报冲突 |

## 决定的业务规则

1. **空值不是零。** 成本、上限、`contracted`、`shared_site` 等关键字段为空即报错，不会按 0 或 `false` 静默处理。
2. **资金不可互换。** 项目只能使用 `allowed_work` 含其 `work_type` 的资金行，且占用该行年度上限；一个项目只能占一条资金行，不做跨来源拆分。
3. **前置关系。** 默认要求前置工程安排在更早年度；只有情景显式设置 `allow_same_year_predecessor` 才允许同年度接续，且结果标为“暂定”。前置未入选时，依赖工程不能计入可交付量。
4. **共享施工范围。** 共享施工面的工程若因前置/依赖被拆到不同年度，依赖工程所在资金行额外承担 `repeat_dig_cost`。
5. **已签约项目不静默消失。** 入选目标首先最大化已签约项目入选数；无法安排时状态为“冲突”，并给出成本、可用单条上限、缺口和候选资金行。
6. **成本修订留痕。** 情景中的成本修订必须带 `as_of`、`reason`、`source`；报告保留原估算/原核定值与当前值。比较时可用冻结 JSON 报告作为“当时依据”。
7. **输入不可信即不出方案。** 任何错误都使状态变为“不可执行”，此时不运行组合测算。
8. **受益口径。** 项目与具体民生需求没有直接映射，片区受益按“该片区是否有项目入选”计，户数取该片区已确认需求户数之和；需求重叠会被提示复核，缺少已确认需求的片区会使结果变为“暂定”。

## 输出内容

报告（Markdown 与 JSON）包含：

- 输入校验错误/警告及来源行列号；
- 批准确认方案：入选项目、年度、资金行、成本依据、重复开挖成本；
- 每个入选项目的入选理由，以及每个未采纳项目的具体原因码与说明；
- 依赖关系状态（满足/阻断/延期/均未安排）与共享施工面重复开挖成本；
- 各资金来源使用与剩余额度、是否超限、资金压力；
- 片区受益与均衡风险（如“有已确认需求的片区未覆盖，而其他片区安排多个小包”）；
- 冲突清单（已签约未安排、超限等）；
- 策略比较（受益/项目数/片区覆盖/依赖链完整度）；
- 变更与依据链，以及输入文件的 sha256 指纹。

## 目录

```
data/                     合成输入（见 docs/input-contract.md）
docs/method.md            测算口径与规则说明
docs/input-contract.md    输入字段契约与空值语义
docs/scenarios.md         内置情景与自定义情景格式
examples/                 自定义情景示例
src/renewal_analytics/
  io.py                   读取与校验
  scenarios.py            情景、成本修订、有效资金
  planner.py              组合搜索、依赖与均衡检查
  report.py               Markdown / JSON 报告
  compare.py              情景比较与冻结基线
  cli.py                  命令行入口
tests/                    校验、规则、报告与 CLI 测试
```
