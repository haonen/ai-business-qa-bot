---
name: analysis-drill
description: Analyze focused EC, BET, or EC×BET follow-up questions about performance, composition, change contribution, same-brand comparison, and monthly trend alignment. Use when the user asks how a specific category, driver, series, media type, platform, KOL tier, or KOL type performed or why it changed.
---

# Analysis Drill

1. Resolve one brand and a current period from the request or the matching domain context.
2. Choose exactly one mode: performance, composition, change_attribution, comparison, or trend_alignment.
3. Use only combinations allowed by `contract.json`; never invent a dimension, KPI, SQL field, or brand.
4. Request the smallest evidence table that answers the question.
5. State two or three conclusions supported by returned rows, then show the table.
6. For an EC drill bundle, synthesize across the summary, Key Driver, series, and SKU tables. Do not write one disconnected "highest" statement per table.
7. Start with total performance, then identify the primary business source. If the top Key Driver reaches `insight_rules.dominant_share`, say the business is mainly driven by that Key Driver and quote both GMV and share.
8. Use the top series concentration defined in `insight_rules` to explain whether business is concentrated in the leading series. Treat Top SKU as supporting detail, not as overall change attribution.
9. Describe growth contribution using positive GMV/spend change shares and decline drag using absolute negative change shares.
10. For EC×BET alignment, describe same-direction or divergence signals only. Never claim causality.
11. Do not attribute overall change to an individual SKU. SKU is a performance/ranking endpoint only.
12. When series uses AI inference, append: `产品系列由AI根据产品链接归纳总结，存在误差。`
13. Render missing values as `—`; do not convert missing data to zero.
14. Treat media fee ratio, Take Rate, TR, and BET% as the same KPI. If media spend and fee ratio are both requested, retain both in the evidence table.
