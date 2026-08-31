# 三平台竞品生意分析运行口径

## 推荐触发语

> 请按竞品三平台生意分析模板，分析【品牌】，本期【时间】，同比去年同期。

也支持：

- 帮我跑一下【品牌】【时间】的三平台生意分析。
- 看一下【品牌】【时间】在天猫、抖音、京东的GMV、份额、类目和渠道表现。

## 固定输出

1. 三条核心结论。
2. 三平台、天猫、抖音、京东的品牌GMV、同比、Beauty Market份额和份额变化。
3. Pure Mass品牌额外输出Pure Mass份额和份额变化。
4. 天猫三级类目、抖音四级类目、京东三级类目的本期GMV Top 5。
5. 抖音KOL直播、品牌自营直播、短视频及其他渠道表现。

## 数据源和字段

| 模块 | 完整自然月 | 非完整自然月 |
|---|---|---|
| 天猫品牌及三级类目 | `three_platform_store_rank_monthly` | `tmall_store_ranking_day_jiashicang` |
| 抖音品牌及四级类目 | `three_platform_store_rank_monthly` | `dy_store_ranking_BFSS_day_jiashicang` |
| 京东自营品牌及三级类目 | `three_platform_store_rank_monthly` | `jd_store_ranking_selfrun_day_jiashicang` |
| Beauty Market大盘 | `global_segment='Beauty Market' AND category_EN='Total Beauty'` | `global_segment='Beauty Market'` |
| Pure Mass大盘 | `global_segment='pure mass' AND category_EN='Total Beauty'` | `global_segment='pure mass'` |

大盘月表`bus_date`按正常自然日期读取，例如2026年2月对应`2026-02-01`；禁止用`DAY(bus_date)`解释月份。

- 品牌：`brand_name`。
- 每个月份×平台切片先使用用户原始品牌名查询；仅对没有结果的切片，才用
  `bot/data/brand_cn_en_reference.json`中的无歧义中文名→英文名映射补查。
  原名已命中的切片不再查英文，避免重复累加；冲突映射不自动转换，报告标题
  仍显示用户输入的品牌名。
- GMV：`gmv`，单位人民币元。
- 平台：`platform`。
- 天猫/京东日表类目：`category_level_3`；抖音日表类目：`category_level_4`。
- 三平台月表类目均使用`category_CN`。天猫和京东通过`SUBSTRING_INDEX(SUBSTRING_INDEX(TRIM(category_CN), '-', 3), '-', -1)`取第3段作为三级类目；抖音通过`SUBSTRING_INDEX(SUBSTRING_INDEX(TRIM(category_CN), '-', 4), '-', -1)`取第4段作为四级类目。
- Pure Mass品牌：`selectivity IS NULL OR TRIM(selectivity)=''`；大盘使用`global_segment='pure mass' AND category_EN='Total Beauty'`。
- 抖音渠道：`kol_gmv`、`storelive_gmv`及倒减后的短视频及其他。

完整月使用月表，部分月使用日表。同一个月份只选一个来源。品牌GMV按品牌对应全部真实店铺行相加。

## 公式

- GMV同比＝本期GMV÷同期GMV－1。
- Beauty Market份额＝品牌GMV÷`global_segment='Beauty Market' AND category_EN='Total Beauty'`大盘GMV。
- Pure Mass份额＝品牌GMV÷Pure Mass大盘GMV。
- 份额变化＝本期份额－同期份额，单位pp。
- 类目占比＝类目GMV÷该平台品牌整体GMV。
- 类目占比变化＝本期类目占比－同期类目占比。
- 短视频及其他＝`gmv-kol_gmv-storelive_gmv`，结果为负时停止报告。
- 同期为0、本期大于0时显示“新增长”；两期均为0时显示“N/A”。
- 本期含2月29日时，同期截止去年2月28日。
