# Market Analysis

用于回答 Total Beauty 大盘涨跌和 Top 品牌问题。

- 未指定 Segment 时使用 PURE MASS。大盘趋势未指定平台时使用 TM、DY、JD 三平台合计；月表筛选`Total Beauty`，日表汇总所选Segment下的全部生意。
- Top品牌问题未指定平台时，默认查天猫Pure Mass Top 5；品牌榜表中Pure Mass的数据口径为`SELECTIVITY IS NULL`。
- 完整自然月优先用月表；非完整月或月表缺失时使用日表，同一个月份不能重复计算。
- Evol%=本期GMV/去年同期GMV-1；Wgt%=平台GMV/三平台GMV；Wgt Change为同比百分点变化。
- “涨得最好”按GMV增长额排序；“涨幅/增速最高”按Evol%排序，且同期GMV必须大于0。
- 只陈述涨跌、平台结构和品牌排名，不推断因果。
- Top品牌输出后，提示用户可以继续分析该品牌的EC生意或BET媒体投资。
