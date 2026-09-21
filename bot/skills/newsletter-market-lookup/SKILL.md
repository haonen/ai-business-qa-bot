---
name: newsletter-market-lookup
description: 查询Newsletter 01整体市场Total Beauty与Pure Mass的GMV、去年同期GMV及同比。
---

# 整体市场原子查数

入口：`bot.market_lookup.try_lookup`。执行：`bot.market_metrics.query_market_metrics`。
功能开关：`NEWSLETTER_MARKET_LOOKUP_ENABLED=1`，默认关闭。

## 范围

- 平台：TTL（三平台合计）、TM、JD、DY。
- 市场口径：BEAUTY MARKET（Total Beauty）、PURE MASS；未指定时同时展示两者，不相加。
- 指标：GMV、去年同期GMV、同比；GMV默认附带Evol%（同比）。另支持CPD金额、同比、Pure Mass份额与同比份额变化。金额原始单位元，M=百万元。
- 日期：明确起止日；相对日期按消息接收日期的北京时间解析，上周=上一完整周一至周日。缺日期只补问日期。
- 品牌经营、BET、产品渠道和诊断请求交回原Bot入口；品类大盘、非CPD整体份额、榜单、环比本阶段未接入，明确告知。

## 执行约束

模型只理解参数，禁止自由生成SQL和计算金额。经过枚举及日期校验后调用固定参数SQL。
数据来自`three_platforms_segmented_markets_daily`，日期字段`bus_date`，平台字段`platform`，口径字段`global_segment`，金额字段`gmv`。
同比为本期/去年同日历期间-1；使用Newsletter的`periods`与`date_set`。
缺日、无效金额不能当0；三平台缺一个平台不能当完整总计；去年同期缺失或非正数不输出同比。
不生成长报告，不发送消息，不运行Newsletter全量生产。

## 上下文

使用独立且可持久化的`market_lookup_context`。同一查询更改平台、口径、指标、日期时仅更新明确字段。
新问题不继承旧品牌日期；切换品牌/BET等请求即退出本入口。市场请求清除旧品牌待确认状态。
未明确的“去年呢”先澄清，不能猜测重查去年还是取去年同期。

## 验证

`tests/test_market_lookup.py`覆盖计算、缺数、状态持久化、原入口隔离。
`tests/fixtures/market_lookup_questions.json`为真实模型验证语料，模型结果需单独记录；单元测试模拟输出不代表自然语言已通过。

## CPD扩展（2026-09-16）

调用`bot.market_cpd_metrics.query_cpd_metrics`；沿用Newsletter `OWNED`四品牌集合、`TABLES`三店铺榜单表和`weight_sql('TTL')`一级生意范围，不重复加入天猫/抖音独立男士小计。不筛SELECTIVITY，不走品牌映射，不改Newsletter。
CPD MS%=CPD GMV/Pure Mass GMV；MS%+/-=本期份额-去年同期份额，以百分点显示，原始精度计算。
混合询问市场Evol%和CPD gain share须同时返回。CPD源失败仍返回已成功的市场结果；缺源不当0，缺分母不能判断gain/lose。
未支持的问题不清空已确认范围，但标记unsupported_request。随后模糊追问需澄清，明确返回Pure Mass等问题可继承原范围。
