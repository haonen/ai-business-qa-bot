from __future__ import annotations

import math


def _missing(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def _fmt_gmv(value) -> str:
    if _missing(value):
        return "—"
    return f"{float(value) / 1_000_000:,.1f}M"


def _fmt_evol(value, current=0, prior=0) -> str:
    if _missing(value):
        if float(current or 0) > 0 and float(prior or 0) == 0:
            return "新增"
        return "—"
    percent = float(value) * 100
    if abs(percent) < 0.5:
        return "0%"
    return f"{percent:+.0f}%"


def _fmt_pct(value) -> str:
    if _missing(value):
        return "—"
    percent = float(value) * 100
    if abs(percent) < 0.5:
        return "0%"
    return f"{percent:.0f}%"


def _fmt_pp(value) -> str:
    if _missing(value):
        return "—"
    points = float(value) * 100
    if abs(points) < 0.5:
        return "0pp"
    return f"{points:+.0f}pp"


def _fmt_pp_magnitude(value) -> str:
    if _missing(value):
        return "—"
    points = abs(float(value) * 100)
    if points < 0.5:
        return "0pp"
    return f"{points:.0f}pp"


def _share_change_text(row: dict) -> str:
    change = row.get("weight_change")
    current_share = _fmt_pct(row.get("weight"))
    if _missing(change) or abs(float(change) * 100) < 0.5:
        return f"占比较同期持平于{current_share}"
    if float(change) > 0:
        return f"占比较同期提升{_fmt_pp_magnitude(change)}至{current_share}"
    return f"占比较同期下降{_fmt_pp_magnitude(change)}至{current_share}"


def _category_table(rows: list[dict]) -> str:
    lines = [
        "| 三级类目 | 本期GMV | 同比 | 占比 | 占比变化 |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row.get('category') or '未分类'} | {_fmt_gmv(row.get('gmv_current'))} | "
            f"{_fmt_evol(row.get('evol'), row.get('gmv_current'), row.get('gmv_prior'))} | "
            f"{_fmt_pct(row.get('weight'))} | {_fmt_pp(row.get('weight_change'))} |"
        )
    return "\n".join(lines)


def format_jd_business_report(result: dict) -> str:
    brand = result.get("brand") or result.get("source_brand") or "品牌"
    period_meta = result.get("period_meta") or {}
    total = result.get("brand_result") or {}
    rows = result.get("categories") or []
    top = (result.get("all_categories") or rows or [{}])[0]
    current = total.get("gmv_current")
    prior = total.get("gmv_prior")
    total_evol = _fmt_evol(total.get("evol"), current, prior)
    current_label = period_meta.get("current_label") or result.get("period")
    prior_label = period_meta.get("prior_label") or "去年同期"
    top_category = top.get("category") or "—"
    top_evol = _fmt_evol(top.get("evol"), top.get("gmv_current"), top.get("gmv_prior"))

    sections = [
        "# 京东品牌生意分析",
        "## 品牌整体生意",
        (
            f"{brand}在{current_label}的京东自营旗舰店总GMV为{_fmt_gmv(current)}，"
            f"同比{total_evol}（{prior_label}为{_fmt_gmv(prior)}）。"
        ),
        "## 品类分析",
        (
            f"品牌整体生意同比{total_evol}，GMV规模最大的三级类目为"
            f"“{top_category}”（占比{_fmt_pct(top.get('weight'))}，同比{top_evol}）。"
        ),
        "## 品类表现",
        (
            f"• “{top_category}”为本期最大品类（占比{_fmt_pct(top.get('weight'))}），"
            f"GMV同比{top_evol}，{_share_change_text(top)}。"
        ),
        _category_table(rows),
    ]
    return "\n\n".join(sections)
