from __future__ import annotations

import math


def _fmt_gmv(value) -> str:
    if value is None:
        return "—"
    number = float(value)
    sign = "-" if number < 0 else ""
    absolute = abs(number)
    if absolute >= 1_000_000:
        return f"{sign}{absolute / 1_000_000:,.1f}M"
    if absolute >= 1_000:
        return f"{sign}{absolute / 1_000:,.1f}K"
    return f"{number:,.0f}"


def _fmt_evol(value, current=0, prior=0) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        if float(current or 0) > 0 and float(prior or 0) == 0:
            return "新增"
        return "—"
    return f"{float(value) * 100:+.1f}%"


def _fmt_pct(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "—"
    return f"{float(value) * 100:.1f}%"


def _fmt_pp(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "—"
    return f"{float(value) * 100:+.1f}pp"


def _category_table(rows: list[dict]) -> str:
    lines = [
        "| 四级类目 | 本期GMV | 同比 | 本期品牌GMV占比 | 占比变化 |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row.get('category') or '未分类'} | {_fmt_gmv(row.get('gmv_current'))} | "
            f"{_fmt_evol(row.get('evol'), row.get('gmv_current'), row.get('gmv_prior'))} | "
            f"{_fmt_pct(row.get('weight'))} | {_fmt_pp(row.get('weight_change'))} |"
        )
    return "\n".join(lines)


def _series_table(rows: list[dict]) -> str:
    lines = [
        "| 产品系列 | 本期GMV | 同比 | 本期类目内占比 | 占比变化 |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row.get('series') or '其他'} | {_fmt_gmv(row.get('gmv_current'))} | "
            f"{_fmt_evol(row.get('evol'), row.get('gmv_current'), row.get('gmv_prior'))} | "
            f"{_fmt_pct(row.get('weight'))} | {_fmt_pp(row.get('weight_change'))} |"
        )
    return "\n".join(lines)


def _channel_table(rows: list[dict]) -> str:
    lines = [
        "| 渠道 | 本期GMV | 同比 | GMV增减额 | 本期品牌GMV占比 | 占比变化 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row.get('channel')} | {_fmt_gmv(row.get('gmv_current'))} | "
            f"{_fmt_evol(row.get('evol'), row.get('gmv_current'), row.get('gmv_prior'))} | "
            f"{_fmt_gmv(row.get('gmv_change'))} | {_fmt_pct(row.get('weight'))} | "
            f"{_fmt_pp(row.get('weight_change'))} |"
        )
    return "\n".join(lines)


def _top_series_table(row: dict | None) -> str:
    if not row:
        return "本期无可展示的产品系列数据。"
    return "\n".join([
        "| GMV最高产品系列 | 本期GMV | 同比 | 本期渠道内占比 | 占比变化 |",
        "|---|---:|---:|---:|---:|",
        (
            f"| {row.get('series') or '其他'} | {_fmt_gmv(row.get('gmv_current'))} | "
            f"{_fmt_evol(row.get('evol'), row.get('gmv_current'), row.get('gmv_prior'))} | "
            f"{_fmt_pct(row.get('weight'))} | {_fmt_pp(row.get('weight_change'))} |"
        ),
    ])


def _top_links_table(rows: list[dict]) -> str:
    lines = [
        "| 排名 | 商品名称 | 商品url | 产品系列 | 本期GMV | 同比 | 本期渠道内占比 |",
        "|---:|---|---|---|---:|---:|---:|",
    ]
    for rank, row in enumerate(rows, 1):
        product_name = str(row.get("product_name") or "—").replace("|", "／")
        product_url = str(row.get("product_url") or "—").replace("|", "%7C")
        series = str(row.get("series") or "其他").replace("|", "／")
        lines.append(
            f"| {rank} | {product_name} | {product_url} | {series} | "
            f"{_fmt_gmv(row.get('gmv_current'))} | "
            f"{_fmt_evol(row.get('evol'), row.get('gmv_current'), row.get('gmv_prior'))} | "
            f"{_fmt_pct(row.get('weight'))} |"
        )
    return "\n".join(lines)


def format_douyin_business_report(result: dict) -> str:
    brand = result.get("brand") or result.get("source_brand") or "品牌"
    period_meta = result.get("period_meta") or {}
    brand_result = result.get("brand_result") or {}
    selected_category = result.get("selected_category") or "—"
    channel_result = result.get("channel_result") or {}

    current = brand_result.get("gmv_current")
    prior = brand_result.get("gmv_prior")
    sections = [
        "# 抖音品牌生意分析",
        "## 1. 品牌整体GMV",
        (
            f"{brand}在{period_meta.get('current_label') or result.get('period')}的品牌整体GMV为"
            f"{_fmt_gmv(current)}，同比"
            f"{_fmt_evol(brand_result.get('evol'), current, prior)}。"
        ),
        "## 2. 四级类目",
        _category_table(result.get("categories") or []),
        f"本期GMV排名第一的四级类目为“{selected_category}”，仅对该类目进行产品系列下钻。",
        f"## 3. {selected_category}产品系列",
        _series_table(result.get("product_series") or []),
        "## 4. 渠道生意",
        _channel_table(channel_result.get("rows") or []),
    ]

    business = channel_result.get("business_leader")
    growth = channel_result.get("growth_leader")
    if business:
        sections.append(
            f"本期生意贡献最大的渠道为{business.get('channel')}，GMV为"
            f"{_fmt_gmv(business.get('gmv_current'))}，占品牌GMV"
            f"{_fmt_pct(business.get('weight'))}。"
        )
    if growth:
        if channel_result.get("all_declining"):
            sections.append(
                f"本期GMV减量最小的渠道为{growth.get('channel')}，GMV变化为"
                f"{_fmt_gmv(growth.get('gmv_change'))}。"
            )
        else:
            sections.append(
                f"本期GMV增量最大的渠道为{growth.get('channel')}，GMV变化为"
                f"{_fmt_gmv(growth.get('gmv_change'))}。"
            )

    channel_products = result.get("channel_products") or {}
    for index, key in enumerate(("kol_live", "store_live", "short_other"), 5):
        channel = channel_products.get(key) or {}
        label = channel.get("channel") or key
        sections.extend([
            f"## {index}. {label} × 货品",
            "### GMV最高产品系列",
            _top_series_table(channel.get("top_series")),
            "### Top 5商品链接",
            f"Top 5集中度：{_fmt_pct(channel.get('top5_concentration'))}",
            _top_links_table(channel.get("top_links") or []),
        ])

    sources = result.get("sources") or []
    source_labels: list[str] = []
    for row in sources:
        table = row.get("table")
        if table and table not in source_labels:
            source_labels.append(table)
    sections.extend([
        "## 数据口径",
        f"- 本期：{period_meta.get('current_start')}至{period_meta.get('current_end')}；同期："
        f"{period_meta.get('prior_start')}至{period_meta.get('prior_end')}。",
        "- 完整自然月的品牌及四级类目使用月表；部分月份使用日表；商品、产品系列和链接始终使用日表。",
        "- KOL直播、品牌自营直播和短视频及其他构成品牌级渠道；短视频及其他采用支付GMV倒减计算。",
        "- 产品系列由商品名称统一归纳，同一映射同时应用于本期和同期。",
        f"- 数据表：{'、'.join(source_labels)}。",
    ])
    return "\n\n".join(section for section in sections if section)
