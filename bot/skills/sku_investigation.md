# Skill：SKU/链接层分析

## 适用场景
用户追问具体哪个产品、哪条链接、某系列或某渠道下的Top链接表现。

## 必须调用的工具
1. query_sku_list
2. query_series（需要系列口径时）
3. query_scene_tag（需要场景/人群信号时）

## 分析步骤
1. 按DrilldownContext组合筛选：brand、period、category、series、kol_driver、link_type、function_tag。
2. Top链接只描述工具返回的GMV、unit、atv、占比和link_type。
3. link_type分布用占比呈现，不把标题词直接上升为战略判断。

## 结论生成三层结构（通用框架）
L1 现状：Top链接GMV、unit、atv、占比。
L2 变化：只有工具返回两年对比时才说同比。
L3 打法信号：需要场景标签或系列功能线双信号支撑，且必须引用数字。

## 数据边界
不能用单条标题推断品牌战略或真实人群，只能说“标题/场景词呈现”。

