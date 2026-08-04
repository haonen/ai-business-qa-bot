# 大盘趋势与Top品牌运行口径

## 路由

- `market_analysis`：大盘涨跌、平台结构和按月趋势。
- `market_brand_ranking`：按GMV增长额或Evol%返回Top 5品牌。
- 未写时间时追问；Segment默认`PURE MASS`。大盘趋势平台默认`TTL`（TM＋DY＋JD），月表Category使用`Total Beauty`，日表汇总所选Segment下的全部生意。
- Top品牌平台默认`TM`，即天猫Pure Mass Top 5；品牌榜月/日表中Pure Mass的数据口径为`SELECTIVITY IS NULL`。用户明确指定三平台、抖音、京东或其他Segment时才切换口径。

## Tool与数据源

| Tool | 月表 | 日表 |
|---|---|---|
| `query_market_trend` | `three_platforms_segmented_markets_monthly` | `three_platforms_segmented_markets_daily` |
| `query_market_top_brands` | `three_platform_store_rank_monthly` | `tmall_store_ranking_day_jiashicang` |

月表`bus_date`按`YYYY-01-MM`存储（日和月对调），查询时必须还原为业务月份`YYYY-MM-01`；日表日期无需转换。

完整自然月且月表有完整口径时使用月表，否则使用日表；本期和同期逐月独立选择，同一个月份只使用一个来源。

三平台Top 5仅支持品牌月表完整覆盖的自然月。非完整月只能查询天猫Top 5，不能用天猫日表冒充三平台。

## 指标

- Evol%＝本期GMV÷同期GMV－1。
- GMV增长额＝本期GMV－同期GMV。
- 平台Wgt%＝平台GMV÷三平台TTL GMV。
- Wgt Change＝本期平台Wgt%－同期平台Wgt%。
- “涨得最好”按GMV增长额；“涨幅/增速最高”按Evol%。

所有SQL和月日选择由确定性代码控制；千问只抽取意图和参数。
