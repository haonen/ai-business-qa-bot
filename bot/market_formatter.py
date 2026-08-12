from __future__ import annotations


PLATFORM_LABELS = {"TTL": "三平台TTL", "TM": "天猫", "DY": "抖音", "JD": "京东"}
SEGMENT_LABELS = {
    "BEAUTY MARKET": "Total Beauty Market",
    "PURE MASS": "Pure Mass",
    "SELECTIVE": "Selective",
    "PROFESSIONAL": "Professional",
}


def _money(value: float | None) -> str:
    if value is None:
        return "—"
    absolute = abs(value)
    if absolute >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if absolute >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:,.0f}"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:+.1%}"


def _pp(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:+.1f}pp"


def _source_summary(coverage: list[dict]) -> str:
    pieces = []
    for period_key, label in (("current", "本期"), ("prior", "同期")):
        monthly = sorted({r["month"] for r in coverage if r.get("period_key") == period_key and r.get("source") == "monthly"})
        daily = sorted({r["month"] for r in coverage if r.get("period_key") == period_key and r.get("source") == "daily"})
        detail = []
        if monthly:
            detail.append(f"月表{','.join(monthly)}")
        if daily:
            detail.append(f"日表{','.join(daily)}")
        pieces.append(f"{label}{'；'.join(detail) or '无覆盖'}")
    return "；".join(pieces)


def _actual_cell(row: dict) -> str:
    return "—" if row.get("comparison_status") == "missing_current" else _money(row.get("gmv_actual"))


def _prior_cell(row: dict) -> str:
    return "—" if row.get("comparison_status") == "missing_prior" else _money(row.get("gmv_prior"))


def _deep_dive_platform_table(rows: list[dict]) -> str:
    return (
        "| 平台 | GMV | 同比 | 平台占比 | GMV增长额 | 对品牌增量贡献 |\n"
        "|---|---:|---:|---:|---:|---:|\n" +
        "\n".join(
            f"| {PLATFORM_LABELS.get(row['platform'], row['platform'])} | {_money(row.get('gmv_actual'))} | "
            f"{_pct(row.get('evol'))} | {_pct(row.get('weight')).replace('+', '')} | "
            f"{_money(row.get('gmv_growth'))} | {_pct(row.get('growth_contribution')).replace('+', '')} |"
            for row in rows
        )
    )


def _deep_dive_month_table(rows: list[dict]) -> str:
    return (
        "| 月份 | GMV | 同比 | 环比 | GMV增长额 | 当月规模主平台 | 节点对照 |\n"
        "|---|---:|---:|---:|---:|---|---|\n" +
        "\n".join(
            f"| {row.get('month')} | {_money(row.get('gmv_actual'))} | {_pct(row.get('evol'))} | "
            f"{_pct(row.get('mom'))} | {_money(row.get('gmv_growth'))} | "
            f"{PLATFORM_LABELS.get(row.get('leading_platform'), row.get('leading_platform'))} | "
            f"{'、'.join(row.get('event_context') or []) or '—'} |"
            for row in rows
        )
    )


def _product_table(block: dict) -> str:
    platform = PLATFORM_LABELS.get(block.get("platform"), block.get("platform"))
    if block.get("status") != "ok":
        reason = {
            "no_product_table": "当前无商品级数据表",
            "no_data": "该品牌在商品表中无可验证数据",
            "query_error": "商品数据查询未通过字段校验",
        }.get(block.get("status"), "无可验证数据")
        return f"**{platform}：** {reason}，不输出选品或价格结论。"
    rows = block.get("products") or []
    if not rows:
        return f"**{platform}：** 无可展示商品。"
    lines = [
        f"**{platform}增长贡献商品**",
        "| 商品 | GMV | GMV增长额 | 同比 | 成交均价代理 | 月度代理区间 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in rows[:3]:
        proxy = row.get("paid_unit_value_proxy")
        low, high = row.get("proxy_month_min"), row.get("proxy_month_max")
        interval = "—" if low is None or high is None else f"¥{low:,.0f}–¥{high:,.0f}"
        lines.append(
            f"| {str(row.get('product_name') or '')[:36]} | {_money(row.get('gmv_actual'))} | "
            f"{_money(row.get('gmv_growth'))} | {_pct(row.get('evol'))} | "
            f"{'—' if proxy is None else f'¥{proxy:,.0f}'} | {interval} |"
        )
    return "\n".join(lines)


def _learning_point(brand: dict) -> str:
    platforms = brand.get("platforms") or []
    positive = [row for row in platforms if float(row.get("gmv_growth") or 0) > 0]
    products = [
        {**row, "platform": block.get("platform")}
        for block in (brand.get("product_evidence") or []) if block.get("status") == "ok"
        for row in (block.get("products") or []) if float(row.get("gmv_growth") or 0) > 0
    ]
    pieces = []
    if positive:
        lead = max(positive, key=lambda row: float(row.get("gmv_growth") or 0))
        pieces.append(
            f"可验证的最大平台增量来自{PLATFORM_LABELS.get(lead['platform'], lead['platform'])}"
            f"（{_money(lead.get('gmv_growth'))}）"
        )
    if products:
        top = max(products, key=lambda row: float(row.get("gmv_growth") or 0))
        pieces.append(
            f"商品证据中增量最大的是{str(top.get('product_name') or '')[:28]}"
            f"（{PLATFORM_LABELS.get(top.get('platform'), top.get('platform'))}，{_money(top.get('gmv_growth'))}）"
        )
    return "；".join(pieces) + "。" if pieces else "当前没有足够的平台或商品增量证据，不生成学习点。"


def _format_brand_deep_dive(result: dict) -> tuple[str, list[str]]:
    meta = result["query_meta"]
    current = "至".join(meta["current_period"])
    events = result.get("event_context") or []
    sections = [
        f"# Pure Mass Top品牌跨三平台深度分析｜{current}",
        "## 选牌逻辑",
        (
            "按本期GMV规模选择Top品牌；同比和增长额仅用于解释其表现，不参与Top名次。"
            if meta.get("ranking_metric") == "gmv_actual" else
            "同时覆盖规模、GMV增长额和同比增速，去重后选2–3个代表品牌。"
        ),
    ]
    if events:
        sections.append(f"本期覆盖的关键节点：{'、'.join(events)}。节点只用作时间对照，不直接当作增长原因。")
    brands = result.get("brands") or []
    for index, brand in enumerate(brands, 1):
        sections.extend([
            f"## {index}. {brand.get('brand')}｜{'、'.join(brand.get('selection_reasons') or [])}",
            f"本期TTL GMV {_money(brand.get('gmv_actual'))}，同比{_pct(brand.get('evol'))}，GMV增长额{_money(brand.get('gmv_growth'))}。",
            "### 平台结构与增长来源",
            _deep_dive_platform_table(brand.get("platforms") or []),
            "### 生意节奏",
            _deep_dive_month_table(brand.get("months") or []),
            "### 选品与价格证据",
            *[_product_table(block) for block in (brand.get("product_evidence") or [])],
            "### 值得学习的点",
            _learning_point(brand),
        ])
    sections.extend([
        "## 数据边界",
        *[f"- {item}" for item in (result.get("limitations") or [])],
    ])
    return "\n\n".join(sections), [str(row.get("brand")) for row in brands]


def format_market_result(result: dict) -> dict:
    if result.get("error"):
        return {"ok": False, "markdown": result.get("message") or "大盘数据查询失败。",
                "meta": {"document_ready": False, "error": result.get("error")}}
    meta = result["query_meta"]
    if meta["tool"] == "query_market_brand_deep_dive":
        markdown, top_brands = _format_brand_deep_dive(result)
        return {"ok": True, "markdown": markdown, "meta": {
            "document_ready": True, "domain": "market",
            "period": "至".join(meta["current_period"]),
            "document_title": "Pure Mass Top品牌跨三平台深度分析",
            "segment": meta["segment"], "platform": "TTL",
            "top_brands": top_brands, "market_result": result,
        }}
    segment = SEGMENT_LABELS.get(meta["segment"], meta["segment"].title())
    platform = PLATFORM_LABELS.get(meta["platform"], meta["platform"])
    current = "至".join(meta["current_period"])
    prior = "至".join(meta["prior_period"])
    category = str(meta.get("category") or "").strip()
    scope = segment if not category else f"{segment} {category}"
    market_label = f"{segment}{platform}" + (f" {category}" if category else "")
    if meta["tool"] == "query_market_top_brands":
        metric_label = {
            "evol": "GMV涨幅",
            "gmv_growth": "GMV增长额",
            "gmv_actual": "本期GMV规模",
        }.get(meta["ranking_metric"], "本期GMV规模")
        rows = result.get("rows") or []
        if not rows:
            markdown = f"{market_label}大盘在该期间没有可用品牌数据。"
        else:
            top = rows[0]
            markdown = (
                f"{market_label}大盘中，{top['brand']}按{metric_label}排名第1，"
                f"本期GMV {_money(top['gmv_actual'])}，同比{_pct(top['evol'])}。\n\n"
                "| 排名 | 品牌 | GMV Actual | 同期GMV | Evol% | GMV增长额 |\n"
                "|---:|---|---:|---:|---:|---:|\n" +
                "\n".join(
                    f"| {r['rank']} | {r['brand']} | {_money(r['gmv_actual'])} | {_money(r['gmv_prior'])} | {_pct(r['evol'])} | {_money(r['gmv_growth'])} |"
                    for r in rows
                )
            )
            new_brands = result.get("new_brands") or []
            if new_brands:
                markdown += "\n\n同期无可比数据、未进入排名的品牌包括：" + "、".join(r["brand"] for r in new_brands[:5]) + "。"
            markdown += "\n\n你可以继续问“第1名的生意怎么样？”或“第1名的媒体投资如何？”。"
        top_brands = [r["brand"] for r in rows]
    else:
        rows = result.get("rows") or []
        ttl = next((r for r in rows if r["platform"] == "TTL"), rows[0] if rows else None)
        if not ttl:
            markdown = f"{market_label}大盘在该期间没有数据。"
        else:
            status_text = (
                "本期数据不完整" if ttl["comparison_status"] == "missing_current" else
                "同期无数据" if ttl["comparison_status"] == "missing_prior" else
                "同期基期为0" if ttl["comparison_status"] == "base_zero" else
                f"同比{_pct(ttl['evol'])}"
            )
            markdown = f"{market_label}大盘本期GMV为{_money(ttl['gmv_actual'])}，{status_text}。"
            platform_rows = [r for r in rows if r["platform"] != "TTL"]
            if platform_rows:
                lead = max(platform_rows, key=lambda r: r.get("wgt") or -1)
                markdown += f" {PLATFORM_LABELS.get(lead['platform'], lead['platform'])}占比最高，为{_pct(lead['wgt']).replace('+', '')}。"
            markdown += (
                "\n\n| 平台 | GMV Actual | 同期GMV | Evol% | GMV增长额 | Wgt% | Wgt Change |\n"
                "|---|---:|---:|---:|---:|---:|---:|\n" +
                "\n".join(
                    f"| {PLATFORM_LABELS.get(r['platform'], r['platform'])} | {_actual_cell(r)} | {_prior_cell(r)} | {_pct(r['evol'])} | {_money(r['gmv_growth'])} | {_pct(r['wgt']).replace('+', '')} | {_pp(r['wgt_change'])} |"
                    for r in rows
                )
            )
            if meta.get("view") == "monthly_trend":
                monthly_rows = result.get("monthly_rows") or []
                markdown += (
                    "\n\n| 月份 | 平台 | GMV Actual | 同期GMV | Evol% | GMV增长额 |\n"
                    "|---|---|---:|---:|---:|---:|\n" +
                    "\n".join(
                        f"| {r['month']} | {PLATFORM_LABELS.get(r['platform'], r['platform'])} | {_actual_cell(r)} | {_prior_cell(r)} | {_pct(r['evol'])} | {_money(r['gmv_growth'])} |"
                        for r in monthly_rows
                    )
                )
        top_brands = []
    markdown += (
        f"\n\n*数据口径：{scope}、{platform}；本期{current}，同比期间{prior}。"
        f"数据源：ECIP MASS；{_source_summary(result.get('coverage') or [])}。*"
    )
    document_ready = meta.get("view") == "monthly_trend" and len(result.get("monthly_rows") or []) > 20
    return {"ok": True, "markdown": markdown, "meta": {
        "document_ready": document_ready, "domain": "market", "period": current,
        "document_title": f"{market_label}大盘分析",
        "segment": meta["segment"], "platform": meta["platform"], "top_brands": top_brands,
        "market_result": result,
    }}
