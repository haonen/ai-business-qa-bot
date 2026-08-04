---
name: media-analysis
description: 生成品牌BET媒体投资飞书报告。用于媒体投资、BET、BKFS/BKFST、Social Search、KSI、KOL Performance、Engage或CPE相关问题。
---

# BET媒体分析

按用户指定期间生成完整飞书报告；单月问题的媒体与KOL看单月，Social Search看当年1月至该月。

## 固定流程

1. 解析2026年分析月份，并生成去年同期。
2. 在Search、Topline、KSI和EC Consolidation NSO数据源中分别解析品牌。
3. 调用结构化查询Tool，不生成或执行任意SQL。
4. 固定依次输出Media Investment、KOL Performance、Social Search三个部分。
5. 使用规则模板生成结论，确保每个数字、实体和排名均来自紧邻表格。

某个数据源没有对应品牌或报告期数据时，不得阻断其他来源；在对应章节明确说明
缺失状态，并继续输出其余可用数据。全部来源均无法匹配时才停止报告。

## 文档格式

- 飞书文档标题由创建文档接口设置，正文不得重复报告标题。
- 不在正文开头集中罗列数据来源；每个H1标题下使用灰色小字写该部分的数据来源
  和核心KPI公式。
- Social Search的数据来源统一写为“小红书”。
- Media Investment的Wgt%口径必须分开说明：AIT Wgt%是对应AIT类型
  占TTL媒体花费的比重；交易平台Wgt%是对应平台花费占Transaction
  花费的比重，不得用“本层/父层”等抽象话术代替。
- TTL、AIT和交易平台在首列使用树状连接符表示层级，不使用会被
  Markdown或飞书清理的前导空格。
- Media Investment、KOL Performance、Social Search使用H1，并依次编号1、2、3。
- 各平台或一级分析主题使用H2；表格分析维度使用H3。
- Category粒度明细必须使用Social Search下面的H2，不得与Social Search平级。
- 不在正文展示内部品牌映射方式、数据覆盖月份或查询技术信息。

## 数据规则

- 3、4月Category搜索数据不可加总成Brand；品牌累计只使用Brand粒度月份。
- Social Search表格不展示排名和环比。Category搜索用于识别搜索量更集中的类目。
- TTL花费以Topline的`spend_million`汇总并转换为人民币元。
- 金额沿用生意分析格式：大于等于1,000,000元显示为一位小数`M`，其余显示为
  一位小数`K`，不使用“万/亿”。
- AIT按Topline的`ait_roe`拆分为Awareness、Influencer和Transaction。
- AIT花费Wgt%使用AIT花费除以TTL花费；Wgt Change为本期Wgt%减上年同期Wgt%。
- Transaction下的平台媒体花费按以下互斥口径汇总：
  - 天猫：`Media=Tmall`，加上直播媒体中`Submedia`包含`Austin`的记录；直播媒体
    同时兼容`Live Stream`和`Live Streaming`。
  - 抖音：`Media=Douyin Qianchuan`，加上直播媒体中
    `Submedia=Douyin KOL Live`的记录。
  - 京东：`Media=JD`。
- 平台Wgt%使用平台交易花费除以Transaction花费；未覆盖的其他交易媒体仍保留在
  Transaction总额内，因此三个已拆分平台不要求合计为100%。
- NSO读取`top_brands_total_ec`，按品牌和报告月份筛选`platform=TTL`后汇总
  `Sales`；TTL记录已经是TM、DY、JD三平台合计，不再重复加总平台行。
- TTL媒体费比使用TTL花费乘1,000,000后除以TTL NSO；三个AIT类型分别使用
  对应花费乘1,000,000后除以同一个TTL NSO。
- NSO Actual和NSO Evol%只在TTL行展示；AIT行只展示费比。
- Transaction下的天猫、抖音和京东只展示花费、Evol%、Wgt%和Wgt Change，
  不展示NSO或媒体费比。
- 费比变化为本期费比减上年同期费比，单位为百分点`pp`。
- 20%–30%作为媒体费比的业务参考；超过该范围可表述为“投入相对较重”。
- BKFS只展示当前Wgt%和相对上年同期的Wgt Change。
- BKFST表格保留B/K/F/S/T代码，洞察必须翻译成业务语言：
  B=品牌专区，K=达人，F=信息流投放，S=搜索投放，T=交易类投资。
- KOL花费使用`SUM(big_v_cost)`，Engage使用`SUM(ttl_engagement)`。
- CPE使用聚合花费除以聚合Engage；Engage为零时留空。
- Tier从高到低为T1、T2、T3、T4、T5、KOC：T1写“头部达人”，T2/T3写
  “中腰部达人”，T4/T5写“长尾达人”，KOC写“素人达人”。
- KOL Type统一翻译：Beauty=美妆垂类、Seeding=种草类、Life=生活类、
  Fashion=时尚类、Sitcom=情景剧类、Others=其他、
  Gossip&Entertainment=八卦娱乐类。
- 2025年没有对应数据时统一显示“2025年无数据”，不得显示“基期为0”。
- Top KOL先按KOL聚合，再按Engage降序。
- 不把同期变化描述为因果关系。

## 洞察写作

使用比较句和转折句写结论，禁止逐项套用“占比XX%、为最高类型、同比XXpp”的
填空模板。只使用上述业务术语和“而、但、相比之下、同期、同时”等普通连接词；
不得发明“收割型打法、种草蓄水、广泛曝光”等营销概念。

### Social Search

- 输出累计搜索量及同比；有多个可比月份时，同时指出同比最高的月份。
- Category明细至少比较两个类目；只有一个类目时才允许单独陈述。

### Media Investment

- 第一张表最多输出三条结论：TTL花费、NSO和费比变化；AIT中占比最高及
  Wgt Change最明显的类型；Transaction中占比最高及花费Evol%变化最明显的平台。
- 不计算或描述单个平台费比，不再引用天猫或抖音商品链接GMV。
- NSO缺失时继续输出花费和结构，NSO与费比显示`—`，其余章节不得中断。
- Overall、RED、DOUYIN的BKFST分别选择：
  - 事实A：当前Wgt%最高的类型；
  - 事实B：Wgt Change绝对值最大的类型。
- A与B不同时，在同一条结论中用“相比之下”对比；相同时只写一次，并说明其
  同时是占比最高和变化幅度最大的类型。
- RED没有T，不得在RED结论中提交易类；DOUYIN可分析交易类。

### KOL Performance

- By Tier和By KOL Type均选择当前Wgt%最高及Wgt Change绝对值最大的对象；
  两者不同时放在同一条中比较，相同时只写一次。
- 比较所选对象的CPE，明确指出哪一类互动成本更低；禁止孤立只报一个CPE。
- T1–T3合计占比下降、T4/T5/KOC合计占比上升时，写“达人结构正从头部及
  中腰部向长尾和素人达人转移”；反向变化时使用相反表述。
- 没有明显迁移时，总结当前主要侧重的前两类达人及第三类辅助布局。
- Top 10部分写Engage第一名、花费和CPE；若其CPE低于所属Tier平均CPE，
  同时写明低出的百分比。
