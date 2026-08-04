# 追问 Skills V2

## 入口与隔离

- 宽泛 EC 问题继续走 `default_chain`。
- 宽泛 BET 问题继续走 `media_chain`，包括“那它媒体投资如何”。
- 按月、整理、表格、排名、对比、贡献、拖累、同步或背离等窄查询走 V2。
- EC 与 BET 分别保存 `ec_context` 和 `bet_context`，切换报告不会清除另一域上下文。

## Skill 与契约

- `bot/skills/followup/analysis-drill/`：表现、构成、变化贡献、对象比较及 EC×BET 月度信号。
- `bot/skills/followup/data-organizer/`：期间汇总、月度趋势、交叉表和排名。
- 每个目录中的 `contract.json` 是维度、模式和行数的执行白名单；千问只生成分析计划，不生成 SQL。

## Tool 文件

- `bot/tools/query_ec_followup_table.py`
- `bot/tools/query_bet_followup_table.py`
- `bot/tools/query_change_contribution.py`
- `bot/tools/query_ec_bet_monthly.py`

所有 Tool 统一返回 `query_meta`、`filters`、`totals`、`rows`、`coverage`、`missing` 和 `evidence`。缺月为 `—`，不补零或插值。

## 输出

- 一张表且不超过 20 行：聊天内直接回复。
- 超过 20 行或两张及以上表格：创建新的飞书文档。
- 分析型问题最多三条确定性 Evidence 结论；数据整理不调用语言润色。
- 系列结果始终带 AI 归纳误差说明。

## 灰度开关

```dotenv
FOLLOWUP_SKILL_V2_ENABLED=1
FOLLOWUP_SKILL_V2_SHADOW=0
FOLLOWUP_PLAN_TIMEOUT=12
```

- Shadow：`ENABLED=0`、`SHADOW=1`，保留旧回答，仅记录 V2 计划和验证结果。
- 正式：`ENABLED=1`、`SHADOW=0`。

部署后运行：

```bash
python -m unittest discover -s tests
```
