# Market Analysis

用于回答 Total Beauty 大盘涨跌和 Top 品牌问题。

- 未指定 Segment 时使用 PURE MASS。大盘趋势未指定平台时使用 TM、DY、JD 三平台合计。
- 用户说`Total Beauty Market`时使用`global_segment='Beauty Market'`，不使用`category_EN='Total Beauty'`代替。
- Pure Mass/Selective/Professional的完整月使用各自Segment下的`category_EN='Total Beauty'`；日表汇总所选Segment下的全部生意。
- 单纯Top品牌排名未指定平台时，默认查天猫Pure Mass Top 5；品牌榜表中Pure Mass的数据口径为`SELECTIVITY IS NULL`。
- 用户要求“跨三平台＋选品＋生意节奏＋价格/促销＋学习点”时，必须进入品牌深度分析，不得用单张Top 5榜单回答。深度分析至少选择2—3个代表品牌，分别覆盖规模领先、GMV增长额领先和增速领先（去重后不足再按增长额补足）。
- 品牌深度分析必须拆出TM/DY/JD的GMV、同比、品牌内平台占比和增长贡献，并至少使用by month数据识别峰值、增长月份及当月主导平台。
- 商品结论必须有商品级GMV/同比证据；缺少京东商品表时明确标注缺口。`GMV/销量`只能写作成交单价代理值，不能冒充吊牌价、到手价或促销折扣；没有价格历史、优惠券和促销标识时，不得判断价格优势或把节点与增长写成因果。
- “值得学习”只保留可追溯到平台增长额、商品增长额或节奏变化的数据事实，不输出泛化建议。
- 完整自然月优先用月表；非完整月或月表缺失时使用日表，同一个月份不能重复计算。
- 品牌榜只有在月表对应月份缺失或请求包含非完整月时才查询天猫日表；完整月份覆盖齐全时禁止额外扫描日表。日表日期条件直接使用原始`YYYY/MM/DD`字符串范围以命中索引，不在WHERE中对日期列做`CAST`。
- `three_platform_store_rank_monthly.bus_date`是`YYYY-01-MM`月度编码，先恢复成`YYYY-MM-01`；禁止直接当自然日期筛选。
- Evol%=本期GMV/去年同期GMV-1；Wgt%=平台GMV/三平台GMV；Wgt Change为同比百分点变化。
- “涨得最好”按GMV增长额排序；“涨幅/增速最高”按Evol%排序，且同期GMV必须大于0。
- 用户只说`Top N品牌`时按本期GMV规模排序，不得解释成增长Top N；只有明确出现增长、增量、拉动、涨幅或增速时才切换增长指标。`Top N`中的N必须作为返回数量。
- 品牌月表在汇总前按业务月、平台、店铺、品牌、一级/二级品类和Segment业务键去重；批次或状态差异造成的重复记录不得重复累计GMV。
- 只陈述涨跌、平台结构和品牌排名，不推断因果。
- Top品牌输出后，提示用户可以继续分析该品牌的EC生意或BET媒体投资。
