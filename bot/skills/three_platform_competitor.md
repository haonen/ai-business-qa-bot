# 三平台竞品生意分析

- 仅在用户明确要求“竞品三平台生意分析”“三平台生意分析”“三平台竞品分析”，或同时要求天猫、抖音、京东的品牌生意报告时触发。
- 每个月份×平台切片先按用户输入的`UPPER(TRIM(brand_name))`取数；仅对没有数据的切片，才使用`brand_CN`→`brand_name_DH`的唯一中英文参考映射补查。原名已命中的切片不再查英文，避免重复累加；一中文对应多个英文的冲突项不自动转换。月表平台只接受标准值`TM`/`DY`/`JD`；京东含义固定为京东自营。
- 完整自然月使用`three_platform_store_rank_monthly`；非完整自然月分别使用天猫、抖音和京东日表。同一月份不得同时使用月表和日表。
- 月表的品牌GMV、类目GMV及抖音渠道GMV均限定`clear_category_status`为`female skincare`/`Makeup`/`Hair`/`male skincare`四类；月表和日表按品牌及类目直接汇总GMV，真实多店铺行必须相加。
- 完整自然月的三平台类目均使用月表`category_CN`。天猫和京东取第3段（第2个`-`后的值）作为三级类目；抖音取第4段（第3个`-`后的值）作为四级类目。非完整月日表中天猫、京东使用`category_level_3`，抖音使用`category_level_4`；分别展示本期GMV Top 5。
- 大盘金额使用`gmv`，平台使用`platform`。大盘月表中Beauty Market和Pure Mass均限定`category_EN='Total Beauty'`，再分别按`global_segment='Beauty Market'`和`global_segment='pure mass'`汇总。
- 大盘月表`bus_date`是正常自然日期，按`CAST(bus_date AS DATE)`读取；禁止用`DAY(bus_date)`解释月份。
- `selectivity`为空或NULL的品牌为Pure Mass品牌；月表和日表大盘口径均使用`global_segment='pure mass'`。
- 同比＝本期GMV÷同期GMV－1；同期为0且本期大于0显示“新增长”，两期均为0显示“N/A”。
- 份额＝品牌GMV÷对应平台/三平台大盘GMV；份额变化＝本期份额－同期份额，单位pp。
- 抖音渠道：KOL直播=`kol_gmv`；品牌自营直播=`storelive_gmv`；短视频及其他=`gmv-kol_gmv-storelive_gmv`。倒减为负时停止报告。
- 闰年本期截止2月29日时，同期截止去年2月28日。
- 输出结构固定为：三条核心结论、品牌整体生意、三平台Top 5类目、抖音渠道表现、数据口径。
