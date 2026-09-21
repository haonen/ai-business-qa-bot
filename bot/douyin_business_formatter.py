from __future__ import annotations

import json
import math
import os

from bot.utils import extract_json_object, llm_client, llm_model


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
        "| 类目 | 本期GMV | 同比 | 本期品牌GMV占比 | 占比变化 |",
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
        "| 产品系列 | 本期GMV | 同比 | 本期品牌商品GMV占比 | 占比变化 |",
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


def _product_category_table(rows: list[dict]) -> str:
    lines = [
        "| 品类 | 本期商品GMV | 同期GMV | GMV增减额 | 同比 | 商品GMV占比 | 占比变化 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row.get('category') or '未分类'} | {_fmt_gmv(row.get('gmv_current'))} | "
            f"{_fmt_gmv(row.get('gmv_prior'))} | {_fmt_gmv(row.get('gmv_change'))} | "
            f"{_fmt_evol(row.get('evol'), row.get('gmv_current'), row.get('gmv_prior'))} | "
            f"{_fmt_pct(row.get('weight'))} | {_fmt_pp(row.get('weight_change'))} |"
        )
    return "\n".join(lines)


def _driver_table(rows: list[dict]) -> str:
    lines = [
        "| Key Driver | 本期GMV | 同期GMV | GMV增减额 | 同比 | 商品GMV占比 | 占比变化 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row.get('key_driver')} | {_fmt_gmv(row.get('gmv_current'))} | "
            f"{_fmt_gmv(row.get('gmv_prior'))} | {_fmt_gmv(row.get('gmv_change'))} | "
            f"{_fmt_evol(row.get('evol'), row.get('gmv_current'), row.get('gmv_prior'))} | "
            f"{_fmt_pct(row.get('weight'))} | {_fmt_pp(row.get('weight_change'))} |"
        )
    return "\n".join(lines)


def _driver_links_table(rows: list[dict]) -> str:
    lines = [
        "| 排名 | 商品链接标题 | 品类 | 商品ID | 本期GMV | 同期GMV | 同比 | Driver内占比 |",
        "|---:|---|---|---|---:|---:|---:|---:|",
    ]
    for rank, row in enumerate(rows, 1):
        product_name = str(row.get("product_name") or "—").replace("|", "／")
        item_id = str(row.get("item_id") or "—").replace("|", "／")
        category = str(row.get("category") or "未分类").replace("|", "／")
        lines.append(
            f"| {rank} | {product_name} | {category} | {item_id} | "
            f"{_fmt_gmv(row.get('gmv_current'))} | {_fmt_gmv(row.get('gmv_prior'))} | "
            f"{_fmt_evol(row.get('evol'), row.get('gmv_current'), row.get('gmv_prior'))} | "
            f"{_fmt_pct(row.get('weight'))} |"
        )
    return "\n".join(lines)


def _title_fallback(rows: list[dict], scope: str) -> str:
    titles = " ".join(str(row.get("product_name") or "") for row in rows[:5])
    benefit_terms = [
        term for term in ("抗皱", "紧致", "美白", "淡斑", "提亮", "保湿", "补水", "修护", "防晒", "清洁")
        if term in titles
    ][:3]
    format_terms = [term for term in ("套装", "礼盒", "组合", "赠", "试用", "小样") if term in titles][:2]
    campaign_terms = [term for term in ("618", "双11", "狂欢", "会员", "达人专属", "直播专属") if term in titles][:2]
    signals = []
    if benefit_terms:
        signals.append("核心卖点集中在" + "、".join(benefit_terms))
    if format_terms:
        signals.append("货品表达以" + "、".join(format_terms) + "为主")
    if campaign_terms:
        signals.append("并突出" + "、".join(campaign_terms) + "场景")
    if not signals:
        signals.append("Top商品标题较分散，暂未形成可验证的集中卖点")
    return f"{scope}：" + "，".join(signals) + "。"


def _title_insights_map(scopes: dict[str, tuple[str, list[dict]]]) -> dict[str, str]:
    """Summarize all title scopes in one bounded model call with safe fallbacks."""
    fallbacks = {
        scope_id: "• " + _title_fallback(rows, label)
        for scope_id, (label, rows) in scopes.items()
        if rows
    }
    payload = []
    for scope_id, (label, rows) in scopes.items():
        if not rows:
            continue
        payload.append({
            "scope_id": scope_id,
            "scope": label,
            "top_titles": [
                {
                    "rank": index,
                    "title": str(row.get("product_name") or "")[:100],
                    "category": row.get("category") or "未分类",
                    "gmv": _fmt_gmv(row.get("gmv_current")),
                    "share": _fmt_pct(row.get("weight")),
                }
                for index, row in enumerate(rows[:5], 1)
            ],
        })
    if not payload:
        return {}
    if os.environ.get("LEGACY_PIPELINE_EMERGENCY", "0").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        return fallbacks
    prompt = f"""请分别总结以下多个电商Top商品链接标题。
数据：{json.dumps(payload, ensure_ascii=False)}

每个scope写1-2条短结论：从标题总结可验证的货品卖点、套装/礼赠形式和活动场景；形成业务判断，
但不推断人群、因果、行业或竞品。只返回JSON：
{{"insights":[{{"scope_id":"原scope_id","bullets":["结论1","结论2"]}}]}}。"""
    try:
        response = llm_client(max_retries=1).chat.completions.create(
            model=llm_model("summary"),
            messages=[{"role": "user", "content": prompt}],
            max_tokens=800,
            timeout=float(os.environ.get("DOUYIN_TITLE_LLM_TIMEOUT", "12")),
            response_format={"type": "json_object"},
            extra_body={"enable_thinking": False},
        )
        parsed = extract_json_object(response.choices[0].message.content or "")
        allowed = set(fallbacks)
        for item in parsed.get("insights") or []:
            scope_id = str(item.get("scope_id") or "")
            if scope_id not in allowed:
                continue
            bullets = [
                "• " + str(value).lstrip("-• ").strip()
                for value in (item.get("bullets") or [])[:2]
                if str(value).strip()
            ]
            if bullets:
                fallbacks[scope_id] = "\n".join(bullets)
    except Exception:
        pass
    return fallbacks


def _category_summary(rows: list[dict], selected_category: str) -> str:
    classified = [
        row for row in rows if str(row.get("category") or "").strip() != "未分类"
    ]
    if not classified:
        return "当前无可展示的品类数据。"
    largest = max(classified, key=lambda row: float(row.get("gmv_current") or 0))
    selected = next(
        (row for row in classified if row.get("category") == selected_category), largest
    )
    share_gainer = max(classified, key=lambda row: float(row.get("weight_change") or 0))
    selected_change = float(selected.get("gmv_change") or 0)
    selection_text = "GMV增长额最大" if selected_change >= 0 else "GMV减量最小"
    bullets = [
        (
            f"• “{largest.get('category')}”是最大已分类品类，占商品GMV"
            f"{_fmt_pct(largest.get('weight'))}，同比"
            f"{_fmt_evol(largest.get('evol'), largest.get('gmv_current'), largest.get('gmv_prior'))}。"
        )
    ]
    if share_gainer.get("category") != largest.get("category") or float(
        share_gainer.get("weight_change") or 0
    ) > 0:
        bullets.append(
            f"• “{share_gainer.get('category')}”占比变化最突出（"
            f"{_fmt_pp(share_gainer.get('weight_change'))}），同比"
            f"{_fmt_evol(share_gainer.get('evol'), share_gainer.get('gmv_current'), share_gainer.get('gmv_prior'))}。"
        )
    bullets.append(f"• 按{selection_text}选择“{selected.get('category')}”继续下钻。")
    return "\n".join(bullets)


def _series_summary(rows: list[dict], *, scope: str) -> str:
    active = [row for row in rows if float(row.get("gmv_current") or 0) > 0]
    if not active:
        return "当前无可展示的产品系列数据。"
    leader = max(active, key=lambda row: float(row.get("gmv_current") or 0))
    share_gainer = max(active, key=lambda row: float(row.get("weight_change") or 0))
    bullets = [
        f"• {scope}以“{leader.get('series') or '其他'}”为主，占比"
        f"{_fmt_pct(leader.get('weight'))}，同比"
        f"{_fmt_evol(leader.get('evol'), leader.get('gmv_current'), leader.get('gmv_prior'))}。"
    ]
    if share_gainer.get("series") != leader.get("series") or float(
        share_gainer.get("weight_change") or 0
    ) > 0:
        bullets.append(
            f"• “{share_gainer.get('series') or '其他'}”份额变化最突出（"
            f"{_fmt_pp(share_gainer.get('weight_change'))}），同比"
            f"{_fmt_evol(share_gainer.get('evol'), share_gainer.get('gmv_current'), share_gainer.get('gmv_prior'))}。"
        )
    return "\n".join(bullets)


def _driver_summary(rows: list[dict]) -> str:
    active = [row for row in rows if float(row.get("gmv_current") or 0) > 0]
    if not active:
        return "当前无可展示的Key Driver生意。"
    leader = max(active, key=lambda row: float(row.get("gmv_current") or 0))
    growth = max(active, key=lambda row: float(row.get("gmv_change") or 0))
    return (
        f"{leader.get('key_driver')}是最大生意来源（占比{_fmt_pct(leader.get('weight'))}）；"
        f"{growth.get('key_driver')}的GMV变化额最高（{_fmt_gmv(growth.get('gmv_change'))}）。"
    )


def _format_product_daily_v2(result: dict) -> str:
    brand = result.get("brand") or result.get("source_brand") or "品牌"
    period_meta = result.get("period_meta") or {}
    brand_result = result.get("brand_result") or {}
    product = result.get("product_analysis") or {}
    selected_category = product.get("selected_category") or "—"
    current = brand_result.get("gmv_current")
    prior = brand_result.get("gmv_prior")
    product_total = product.get("product_total") or {}
    categories = product.get("categories") or []
    analysis_categories = product.get("analysis_categories") or [
        row for row in categories if str(row.get("category") or "").strip() != "未分类"
    ]
    selected_links = product.get("selected_category_top_links") or []
    drivers = product.get("key_drivers") or []
    title_scopes = {"category": (f"{selected_category}品类", selected_links)}
    for index, drilldown in enumerate(product.get("driver_drilldowns") or []):
        title_scopes[f"driver_{index}"] = (
            str(drilldown.get("key_driver") or "Key Driver"),
            drilldown.get("top_links") or [],
        )
    title_insights = _title_insights_map(title_scopes)
    data_source = "\n".join([
        "数据来源：",
        "• 店铺GMV（品牌整体GMV）：百库驾驶舱；抖音_店铺排行_日表_仅品牌旗舰店、抖音_店铺排行_月表_仅品牌旗舰店。月表优先，日表补充非完整月份。",
        "• 品类及Key Driver分析（含系列与商品链接标题）：百库驾驶舱；抖音-集瓜-商品销售榜-抖音-日表。仅统计商品四级分类非空的记录；商品GMV占比也以此范围为分母。",
    ])
    sections = [
        data_source,
        "---",
        "# 整体生意",
        ((
            f"{brand}在{period_meta.get('current_label') or result.get('period')}的抖音整体GMV为"
            f"{_fmt_gmv(current)}，同比{_fmt_evol(brand_result.get('evol'), current, prior)}"
            f"（同期{_fmt_gmv(prior)}）。"
        ) if brand_result else
         "店铺表未覆盖完整请求时间，本次不展示整体GMV，也不使用商品GMV冒充店铺GMV。"),
        "---",
        "# 品类分析",
        _category_summary(analysis_categories, selected_category),
        "## 品类表现",
        _product_category_table(categories),
        f"## {selected_category}下钻",
        "### 产品系列分布",
        "> _产品系列由AI根据商品链接标题归纳总结，存在误差。_",
        _series_summary(product.get("selected_category_series") or [], scope=selected_category),
        _series_table(product.get("selected_category_series") or []),
        "### Top5商品链接及标题分析",
        title_insights.get("category", ""),
        _driver_links_table(selected_links),
        "---",
        "# Key Driver分析",
        _driver_summary(drivers),
        "## Key Driver表现",
        _driver_table(drivers),
        "## Key Driver下钻",
    ]
    for drilldown_index, drilldown in enumerate(product.get("driver_drilldowns") or []):
        driver = drilldown.get("key_driver") or "Key Driver"
        top_category = drilldown.get("top_category")
        sections.append(f"### {driver}")
        if top_category:
            sections.append(
                f"本期GMV最大的品类是“{top_category.get('category')}”，GMV为"
                f"{_fmt_gmv(top_category.get('gmv_current'))}，同比"
                f"{_fmt_evol(top_category.get('evol'), top_category.get('gmv_current'), top_category.get('gmv_prior'))}。"
            )
        else:
            sections.append("本期无可展示的品类和商品。")
        if drilldown.get("series"):
            sections.append("#### 主要产品系列")
            sections.append(_series_summary(drilldown.get("series") or [], scope=driver))
            sections.append(_series_table(drilldown.get("series") or []))
        if drilldown.get("top_links"):
            sections.append("#### Top5商品链接及标题分析")
            sections.append(title_insights.get(f"driver_{drilldown_index}", ""))
            sections.append(_driver_links_table(drilldown.get("top_links") or []))

    reconciliation = product.get("reconciliation") or {}
    quality = product.get("quality") or {}
    limitations = result.get("limitations") or []
    sections.extend([
        "---",
        "## 附录｜口径对账与数据边界",
        ((
            f"商品日表本期GMV为{_fmt_gmv(product_total.get('gmv_current'))}，"
            f"占店铺表整体GMV的{_fmt_pct(reconciliation.get('coverage_current'))}；"
            f"同期覆盖率为{_fmt_pct(reconciliation.get('coverage_prior'))}。"
        ) if reconciliation.get("coverage_current") is not None else
         f"商品表本期GMV为{_fmt_gmv(product_total.get('gmv_current'))}；"
         "店铺表覆盖不完整，未计算商品覆盖率。"),
        "- 品牌整体GMV使用店铺表；品类、系列、Key Driver及商品链接使用商品日表。",
        "- Key Driver依据销量字段打标，所有GMV均汇总商品日表的“销售额”。",
        (
            "- 质量闸门："
            f"空标签={quality.get('null_driver_rows', 0)}，"
            f"无法转数值={quality.get('invalid_numeric_rows', 0)}。"
        ),
        (
            f"- 本期：{period_meta.get('current_start')}至{period_meta.get('current_end')}；"
            f"同期：{period_meta.get('prior_start')}至{period_meta.get('prior_end')}。"
        ),
    ])
    suggestions = []
    if selected_category and selected_category != "—" and selected_links:
        suggestions.append(
            f"“继续分析{selected_category}的系列和Top商品”"
        )
    active_drivers = [row for row in drivers if float(row.get("gmv_current") or 0) > 0]
    if active_drivers:
        growth_driver = max(active_drivers, key=lambda row: float(row.get("gmv_change") or 0))
        suggestions.append(
            f"“继续分析{growth_driver.get('key_driver')}的Top品类和商品”"
        )
    if suggestions:
        sections.extend([
            "## 可继续分析",
            "你可以继续问" + "，或".join(suggestions) + "。",
        ])
    if limitations:
        sections.extend([
            "### 本次数据边界",
            "\n".join(f"- {item}" for item in limitations),
        ])
    return "\n\n".join(section for section in sections if section)


def format_douyin_business_report(result: dict) -> str:
    if result.get("product_analysis"):
        return _format_product_daily_v2(result)
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
        "## 2. 类目表现",
        _category_table(result.get("categories") or []),
        f"本期GMV排名第一的类目为“{selected_category}”。",
    ]

    if channel_result.get("rows"):
        sections.extend([
            "## 3. 渠道生意",
            _channel_table(channel_result.get("rows") or []),
        ])

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

    limitations = result.get("limitations") or []
    if limitations:
        sections.extend([
            "## 本次未展示的数据模块",
            "\n".join(f"- {item}" for item in limitations),
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
        "- 完整自然月的品牌及类目优先使用月表；仅部分自然月使用品牌日表。",
        "- KOL直播、品牌自营直播和短视频及其他构成品牌级渠道；短视频及其他采用支付GMV倒减计算。",
        f"- 数据表：{'、'.join(source_labels)}。",
    ])
    return "\n\n".join(section for section in sections if section)
