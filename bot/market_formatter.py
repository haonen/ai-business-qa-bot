from __future__ import annotations


PLATFORM_LABELS = {"TTL": "三平台TTL", "TM": "天猫", "DY": "抖音", "JD": "京东"}
SEGMENT_LABELS = {"PURE MASS": "Pure Mass", "SELECTIVE": "Selective", "PROFESSIONAL": "Professional"}


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


def format_market_result(result: dict) -> dict:
    if result.get("error"):
        return {"ok": False, "markdown": result.get("message") or "大盘数据查询失败。",
                "meta": {"document_ready": False, "error": result.get("error")}}
    meta = result["query_meta"]
    segment = SEGMENT_LABELS.get(meta["segment"], meta["segment"].title())
    platform = PLATFORM_LABELS.get(meta["platform"], meta["platform"])
    current = "至".join(meta["current_period"])
    prior = "至".join(meta["prior_period"])
    if meta["tool"] == "query_market_top_brands":
        metric_label = "GMV涨幅" if meta["ranking_metric"] == "evol" else "GMV增长额"
        rows = result.get("rows") or []
        if not rows:
            markdown = f"{segment}{platform} Total Beauty大盘在该期间没有可比且正增长的品牌。"
        else:
            top = rows[0]
            markdown = (
                f"{segment}{platform} Total Beauty大盘中，{top['brand']}按{metric_label}排名第1，"
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
            markdown = f"{segment}{platform} Total Beauty大盘在该期间没有数据。"
        else:
            status_text = (
                "本期数据不完整" if ttl["comparison_status"] == "missing_current" else
                "同期无数据" if ttl["comparison_status"] == "missing_prior" else
                "同期基期为0" if ttl["comparison_status"] == "base_zero" else
                f"同比{_pct(ttl['evol'])}"
            )
            markdown = f"{segment}{platform} Total Beauty大盘本期GMV为{_money(ttl['gmv_actual'])}，{status_text}。"
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
        f"\n\n*数据口径：{segment}、Total Beauty、{platform}；本期{current}，同比期间{prior}。"
        f"数据源：ECIP MASS；{_source_summary(result.get('coverage') or [])}。*"
    )
    document_ready = meta.get("view") == "monthly_trend" and len(result.get("monthly_rows") or []) > 20
    return {"ok": True, "markdown": markdown, "meta": {
        "document_ready": document_ready, "domain": "market", "period": current,
        "document_title": f"{segment}{platform} Total Beauty大盘分析",
        "segment": meta["segment"], "platform": meta["platform"], "top_brands": top_brands,
        "market_result": result,
    }}
