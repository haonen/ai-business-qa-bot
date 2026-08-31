# 抖音品牌生意分析运行口径

## 触发方式

明确包含“抖音”或“DY”，并包含“生意分析、经营分析、生意复盘、GMV复盘、品牌复盘”等完整报告意图时，进入`douyin_business_analysis`。

推荐问法：

```text
生成韩束2026年6月抖音生意分析报告
帮我做韩束2026年618抖音生意复盘
```

品牌或期间缺失时先追问，不静默补值。

## 数据源

| 模块 | 完整自然月 | 单日或部分月份 |
|---|---|---|
| 品牌整体GMV、四级类目 | `three_platform_store_rank_monthly`，`platform='DY'` | `dy_store_ranking_BFSS_day_jiashicang` |
| 品牌级渠道及渠道占比 | `dy_store_ranking_BFSS_day_jiashicang` | `dy_store_ranking_BFSS_day_jiashicang` |
| 商品、产品系列、渠道内系列、Top 5链接 | `dy_goodssales_rank_day_jiashicang` | `dy_goodssales_rank_day_jiashicang` |

本期和同期逐月独立选择日表或月表；同一个自然月不能重复使用两个来源。
月表`three_platform_store_rank_monthly.bus_date`是正常自然日期，年月按`YEAR(bus_date)`和`MONTH(bus_date)`读取，不得用`DAY(bus_date)`代表月份。常用美妆业务口径筛选`clear_category_status IN ('female skincare','Makeup','Hair','male skincare')`。`clear_category_status='TTL'`还包含其他范围，不能当成这四类的简单合计。

## 字段

品牌/类目表使用：

- `bus_date`
- `platform`（月表固定`DY`）
- `brand_name`
- `gmv`
- `category_level_4`
- `KOL_gmv`
- `storelive_gmv`

商品日表使用：

- `业务日期`
- `商品品牌`
- `商品ID`
- `商品名称`
- `商品url`
- `商品四级分类`
- `销售额`
- `达人推广直播GMV`
- `品牌自营直播GMV`

上述金额字段均按支付GMV解释。

## 公式

- 同比＝本期GMV÷同期GMV－1。
- 本期品牌GMV占比＝分组本期GMV÷本期品牌整体GMV。
- 占比变化＝本期占比－同期占比，单位为pp。
- 重点四级类目＝本期GMV排名第一的`category_level_4`。
- KOL直播GMV＝`SUM(KOL_gmv)`；品牌级渠道分析使用日表。
- 品牌自营直播GMV＝`SUM(storelive_gmv)`；品牌级渠道分析使用日表。
- 短视频及其他GMV＝`SUM(gmv)-SUM(KOL_gmv)-SUM(storelive_gmv)`。
- 商品级KOL直播GMV＝`SUM(达人推广直播GMV)`。
- 商品级品牌自营直播GMV＝`SUM(品牌自营直播GMV)`。
- 商品级短视频及其他GMV＝`SUM(销售额)-SUM(达人推广直播GMV)-SUM(品牌自营直播GMV)`。
- Top 5链接占比＝链接渠道GMV÷对应渠道全部GMV。
- Top 5集中度＝Top 5链接渠道GMV合计÷对应渠道全部GMV。

短视频及其他倒减为负数时停止报告并提示检查源数据，禁止静默归零。

## 产品系列

产品系列不读取现成字段。系统合并本期和同期的全部`商品名称`，统一归纳出“商品名称→产品系列”映射，并将同一映射用于两个期间。无法可靠判断的商品归入“其他”。系列归纳只用于分组，不生成营销效果、人群、卖点或策略结论。

## 固定报告结构

1. 品牌整体GMV。
2. 所有四级类目：本期GMV、同比、本期品牌GMV占比、占比变化pp。
3. 商品模块不按月表类目下钻；直接在品牌全部商品范围内展示各系列本期GMV、同比、品牌商品GMV占比和占比变化pp。
4. 三渠道生意：KOL直播、品牌自营直播、短视频及其他。
5. 每个渠道的GMV最高产品系列。
6. 每个渠道的Top 5商品链接及Top 5集中度。

报告不包含Total Market、大盘、营销效果、收割人群、核心卖点或其他无法由指定字段验证的定性结论。
