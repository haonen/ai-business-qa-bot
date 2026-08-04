# BET媒体分析运行说明

## 入口

命中媒体投资、BET、BKFS/BKFST、Social Search、KSI、KOL Performance、
Engage或CPE等意图时，Bot执行`media_analysis`链路并生成飞书文档。
该判断每轮独立执行；上一轮为天猫报告时，可以继承品牌和期间。

V1只支持2026年，Evol%使用2025年同期。

## 期间

- 单月问题：Topline、NSO、KSI统计该月；Search展示1月至该月。
- 月份区间：Topline、NSO、KSI统计指定区间；Search展示1月至结束月。
- 未写期间：继承会话期间；无上下文时取已匹配数据源的最新共同月份。
- 日级期间或618等活动期间会转换为其覆盖的自然月，因为媒体表为月粒度。

## Tool与数据源

| Tool | 数据表 | 关键字段 |
|---|---|---|
| `query_social_search` | `ai_bot_media_search_index` | `report_month`, `grain_level`, `brand` |
| `query_media_investment` | `ai_bot_media_topline_investment` | `period_month`, `brand_r`, `spend_million`, `ait_roe`, BKFS标签 |
| `query_ec_nso` | `top_brands_total_ec` | `Brand`, `year`, `month`, `Sales`, `platform` |
| `query_kol_performance` | `ai_bot_media_ksi_performance` | `period_month`, `platform`, `big_v_cost`, `ttl_engagement` |

所有查询使用固定SQL和绑定参数。BET品牌会在Search、Topline、KSI、NSO四个来源
分别解析，禁止用用户原始输入静默回退。某一来源没有对应品牌值时跳过该来源查询，
并在对应报告章节说明“无对应品牌或无相关数据”；其他可用来源继续生成报告。
只有全部来源均无法匹配时才停止报告。

运行时品牌解析只读取`ai_bot_brand_resolution_cache`、
`ai_bot_brand_dictionary`和`ai_bot_tmall_brand_index`，不会用店铺名扫描事实表。
EC分析优先将用户输入与`store_CN`、`store_EN`和`brand_name`形成的天猫品牌索引做
精确匹配，再用匹配到的真实`brand_name`查询商品数据；只有索引出现多个真实候选时
才调用LLM消歧。BET仍保留LLM，用于跨Search、Topline、KSI、NSO来源补全
品牌候选。
LLM不能返回候选列表以外的值。高置信度来源映射会写入持久缓存；缺失来源不做
长期负缓存，避免品牌在后续月度数据中新增来源后仍被错误判定为缺失。

首次部署先创建缓存表和查询索引，再刷新品牌字典：

```bash
python -m data_import.media_runtime_migration
python -m data_import.brand_dictionary_pipeline
python -m data_import.tmall_brand_index_pipeline
```

其中迁移会创建或补齐品牌缓存表，为Tmall和Douyin增加品牌+日期组合索引，并为
`top_brands_total_ec`增加`(Brand, platform, year, month)`组合索引。大表加索引建议
在低峰期执行。每次完成月度数据
更新后，再运行`brand_dictionary_pipeline`和`tmall_brand_index_pipeline`刷新字典与
天猫店铺品牌索引。后一个脚本按品牌分批读取事实表，默认每批25个品牌，避免大查询
超时；如数据库负载较高可使用`--batch-size 10`。

天猫品牌索引不是GMV事实表的替代品。它只保存以下解析关系：

```text
用户中文/英文品牌名 → store_CN/store_EN/brand_name别名 → 真实brand_name
```

最终GMV、品类、系列、SKU及渠道查询仍使用已有的
`(brand_name, bus_date)`组合索引精确过滤。

Topline、KSI和NSO均返回实际命中品牌、当前期/同期行数、覆盖月份和聚合值。
当前期有数据但2025年同期无可比数据时显示“2025年无数据”，不计算Evol%。
Search、Topline、NSO、RED KSI和DOUYIN KSI五路查询并行执行。

## 指标

- TTL媒体花费：`SUM(spend_million) * 1,000,000`
- 金额格式：大于等于1,000,000元显示为一位小数`M`，其余显示为一位小数`K`
- TTL按`spend_million`汇总，AIT按`ait_roe`拆分Awareness、Influencer、Transaction。
- Transaction下天猫媒体花费：`Media=Tmall` + 直播媒体且`Submedia`包含Austin；
  直播媒体兼容`Live Stream`与`Live Streaming`。
- Transaction下抖音媒体花费：`Media=Douyin Qianchuan` +
  直播媒体且`Submedia=Douyin KOL Live`。
- Transaction下京东媒体花费：`Media=JD`。
- NSO来自`top_brands_total_ec`中对应品牌、月份、`platform=TTL`的`Sales`汇总。
- TTL和AIT媒体费比均使用媒体花费乘1,000,000后除以同一个TTL NSO；平台行
  不计算费比。
- 费比变化：当前费比 - 2025年同期费比，单位`pp`
- AIT Wgt%：类型花费/TTL花费；交易平台Wgt%：平台花费/Transaction花费
- Wgt Change：2026 Wgt% - 2025同期Wgt%
- Engage：`SUM(ttl_engagement)`
- CPE：`SUM(big_v_cost) / SUM(ttl_engagement)`
- Top KOL：先按KOL聚合，再按Engage降序取前10

3、4月的Category搜索值不加总成Brand；缺少Brand粒度的月份不参与品牌累计。
Social Search表格不展示排名和环比。

## 部署检查

上传代码并安装根目录`requirements.txt`后运行：

```bash
python -m unittest discover -s tests -v
python -m bot.main
```

建议用以下消息验收：

```text
分析2026年3月某品牌的媒体投资
分析2026年1-4月某品牌BET
那它媒体投资如何？
```
