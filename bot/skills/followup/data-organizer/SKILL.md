---
name: data-organizer
description: Organize existing EC and BET KPIs into period summaries, monthly trends, cross tables, and rankings. Use when the user asks to list, arrange, tabulate, rank, compare, or show a metric by month or by an allowed business dimension.
---

# Data Organizer

1. Resolve one brand and a period from the request or domain context.
2. Choose period_summary, monthly_trend, cross_table, or ranking.
3. Use only dimensions and KPIs allowed by `contract.json`.
4. Default amount, GMV, NSO, search, and Engage to Actual plus Evol%.
5. Default Wgt% and media fee ratio to Actual plus year-on-year pp change. Media fee ratio, Take Rate, TR, and BET% are the same KPI.
6. When the user explicitly requests both media spend and fee ratio, keep both metric families: spend Actual/Evol% and fee-ratio Actual/pp change.
7. Show CPE Actual only; never show CPE Evol%.
8. Return a scope sentence and table. Do not add unsolicited interpretation.
9. Keep real source coverage. Show missing months as `—`; never interpolate or fill with zero.
10. Use a direct reply for one table with at most 20 rows. Request a Feishu document for more than 20 rows or multiple tables.
11. When series uses AI inference, append: `产品系列由AI根据产品链接归纳总结，存在误差。`
