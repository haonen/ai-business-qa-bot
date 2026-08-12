# 大盘趋势与Top品牌运行口径

## 路由

- `market_analysis`：大盘涨跌、平台结构和按月趋势。
- `market_brand_ranking`：按GMV增长额或Evol%返回Top 5品牌。
- `market_brand_deep_dive`：同时要求跨三平台、选品、生意节奏、价格/促销或学习点的Top品牌深度分析。该路由不能降级为单张品牌榜。
- 未写时间时追问；Segment默认`PURE MASS`。大盘趋势平台默认`TTL`（TM＋DY＋JD）。
- `Total Beauty Market`是Segment加总口径：月表和日表都直接使用`global_segment='Beauty Market'`，不得用`category_EN='Total Beauty'`代替。
- `Pure Mass`/`Selective`/`Professional`的完整月仍使用各自`global_segment`下的`category_EN='Total Beauty'`品类总计；日表汇总所选Segment下的全部生意。
- 品牌表`three_platform_store_rank_monthly`和`tmall_store_ranking_day_jiashicang`中，`SELECTIVITY IS NULL`已确认为Pure Mass权威口径。
- Top品牌平台默认`TM`，即天猫Pure Mass Top 5；品牌榜月/日表中Pure Mass的数据口径为`SELECTIVITY IS NULL`。用户明确指定三平台、抖音、京东或其他Segment时才切换口径。

## Tool与数据源

| Tool | 月表 | 日表 |
|---|---|---|
| `query_market_trend` | `three_platforms_segmented_markets_monthly` | `three_platforms_segmented_markets_daily` |
| `query_market_top_brands` | `three_platform_store_rank_monthly` | `tmall_store_ranking_day_jiashicang` |
| `query_market_brand_deep_dive` | 品牌榜月表＋`ai_bot_tmall_product_link`＋`ai_bot_dy_product_link` | 当前至少按月输出节奏；京东商品级数据源待补 |

品牌/月度市场表`three_platform_store_rank_monthly.bus_date`按`YYYY-01-MM`存储，必须把日字段恢复为业务月份`YYYY-MM-01`后再筛选；日表日期才按正常自然日期解析。不得对月表直接`CAST(bus_date AS DATE)`，否则4—6月会被误读为1月4—6日。

完整自然月且月表有完整口径时使用月表，否则使用日表；本期和同期逐月独立选择，同一个月份只使用一个来源。

品牌榜先查询月表覆盖情况。仅对缺失月份或非完整月份发起天猫日表查询；完整的1—6月等范围不得再无条件扫描日表。天猫日表`bus_date`按`YYYY/MM/DD`存储，WHERE直接用原始字符串范围以使用日期索引，禁止`CAST(bus_date AS DATE)`后再过滤。

三平台Top 5仅支持品牌月表完整覆盖的自然月。非完整月只能查询天猫Top 5，不能用天猫日表冒充三平台。

## 指标

- Evol%＝本期GMV÷同期GMV－1。
- GMV增长额＝本期GMV－同期GMV。
- 平台Wgt%＝平台GMV÷三平台TTL GMV。
- Wgt Change＝本期平台Wgt%－同期平台Wgt%。
- “涨得最好”按GMV增长额；“涨幅/增速最高”按Evol%。
- 单说`Top N品牌`＝按本期GMV规模降序取N个品牌；不附加正增长过滤。明确说“增长Top N”才按GMV增长额，“增速Top N”才按Evol%。
- `three_platform_store_rank_monthly`先按月份、平台、店铺、品牌、一级/二级品类和Segment业务键折叠重复行，再聚合品牌GMV，禁止把重复批次累计两次。

所有SQL和月日选择由确定性代码控制；千问只抽取意图和参数。

## 品牌深度分析最低交付标准

- 代表品牌：至少2—3个，优先覆盖规模最大、GMV增长额最大、Evol%最高三个角色；角色重复时补充下一名增长品牌。
- 平台：每个品牌输出TM/DY/JD本期GMV、同期GMV、Evol%、品牌内占比、GMV增长额及对品牌增长的贡献。
- 节奏：至少输出by month的品牌GMV、同比、环比和当月主导平台；214、38、520、618等仅作为时间窗口对照，除非另有促销字段，否则不能归因为活动。
- 选品：输出商品GMV、同期GMV、增长额和Evol%；仅在销量可用时补充`GMV/销量`成交单价代理值。
- 价格和促销：当前数据没有统一吊牌价、到手价、优惠券、促销标签和京东商品明细，因此不得声称价格优势或折扣幅度。补齐这些字段后才能做同类目价格带、调价和促销效果分析。
- 学习点：只能由上述平台、商品或节奏证据生成，并明确指出对应增长额；数据缺口必须作为边界输出。
