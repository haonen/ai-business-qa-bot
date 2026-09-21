from __future__ import annotations

from bot.business_analysis_spec import normalize_business_category
from bot.entity_resolution import ResolvedPeriod


def _fmt_gmv(value) -> str:
    number = float(value or 0)
    absolute = abs(number)
    sign = "-" if number < 0 else ""
    if absolute >= 1_000_000:
        return f"{sign}{absolute / 1_000_000:,.1f}M"
    if absolute >= 1_000:
        return f"{sign}{absolute / 1_000:,.1f}K"
    return f"{number:,.1f}"


def _fmt_evol(value) -> str:
    return "—" if value is None else f"{float(value) * 100:+.1f}%"


def _business_total(result: dict, platform: str, subject_scope: str) -> dict:
    meta = dict(result.get("meta") or {})
    if subject_scope == "market":
        rows = ((meta.get("market_result") or {}).get("rows") or [])
        row = next(
            (item for item in rows if str(item.get("platform") or "").upper() == platform),
            rows[0] if rows else {},
        )
        total = {
            "gmv_current": row.get("gmv_actual"),
            "gmv_prior": row.get("gmv_prior"),
            "gmv_change": row.get("gmv_growth"),
            "evol": row.get("evol"),
        }
    else:
        cache = meta.get("last_result_cache") or {}
        if platform == "TM":
            total = dict(((cache.get("ttl_result") or {}).get("total") or {}))
        elif platform == "DY":
            total = dict(((cache.get("douyin_business_result") or {}).get("brand_result") or {}))
        elif platform == "JD":
            total = dict(((cache.get("jd_business_result") or {}).get("brand_result") or {}))
        else:
            rows = ((cache.get("three_platform_competitor_result") or {}).get("overall") or [])
            total = dict(next(
                (item for item in rows if str(item.get("platform") or "").upper() == "TTL"),
                {},
            ))
    if total and total.get("gmv_change") is None:
        total["gmv_change"] = float(total.get("gmv_current") or 0) - float(total.get("gmv_prior") or 0)
    return total


def _runner_for(
    *, subject_scope: str, platform: str, segment: str, category: str,
):
    if subject_scope == "market":
        from bot.chains.market_chain import run_market_chain
        from bot.market_plan import MarketPlan

        def run_market(_brand, period, brand_aliases=None, on_progress=None):
            del brand_aliases
            return run_market_chain(MarketPlan(
                intent="market_analysis", period=period, segment=segment,
                platform=platform, category=category, view="summary",
            ), on_progress=on_progress)

        return run_market
    if platform == "TM":
        from bot.chains.default_chain import run_default_chain
        return run_default_chain
    if platform == "DY":
        from bot.chains.douyin_business_chain import run_douyin_business_chain
        return run_douyin_business_chain
    if platform == "JD":
        from bot.chains.jd_business_chain import run_jd_business_chain
        return run_jd_business_chain
    if platform == "TTL":
        from bot.chains.three_platform_competitor_chain import run_three_platform_competitor_chain

        def run_three_platform(brand, period, brand_aliases=None, on_progress=None):
            del brand_aliases
            return run_three_platform_competitor_chain(brand, period, on_progress=on_progress)

        return run_three_platform
    return None


def run_multi_period_business_chain(
    brand: str,
    platform: str,
    comparison_spec: dict,
    *,
    brand_aliases: list[str] | tuple[str, ...] | None = None,
    subject_scope: str = "brand",
    segment: str = "PURE MASS",
    category: str = "TOTAL BEAUTY",
    runner=None,
    on_progress=None,
) -> dict:
    """Execute each observation with its aligned baseline, then compare YoY results."""
    try:
        category = normalize_business_category(category)
    except ValueError as exc:
        return {
            "ok": False, "markdown": str(exc),
            "meta": {"error_code": "invalid_business_category", "document_ready": False},
        }
    if subject_scope == "brand" and category != "TOTAL BEAUTY":
        return {
            "ok": False,
            "markdown": (
                f"已识别品牌分析Category为{category}，但当前品牌执行链仍只有TTL Beauty口径。"
                "为避免把未过滤的品牌数据误报为该Category，本次没有执行取数。"
            ),
            "meta": {
                "error_code": "unsupported_brand_macro_category",
                "category": category, "document_ready": False,
            },
        }
    analysis = list((comparison_spec or {}).get("analysis_periods") or [])
    baselines = list((comparison_spec or {}).get("baseline_periods") or [])
    if comparison_spec.get("mode") != "YOY_ALIGNED" or len(analysis) != len(baselines):
        return {
            "ok": False,
            "markdown": "多观察期计划无效：分析期与同比基期没有一一对应。",
            "meta": {"error_code": "invalid_multi_period_plan", "document_ready": False},
        }
    if not 2 <= len(analysis) <= 6:
        return {
            "ok": False,
            "markdown": "多观察期分析一次支持2—6个时间段，请缩小范围。",
            "meta": {"error_code": "multi_period_limit", "document_ready": False},
        }
    if subject_scope not in {"brand", "market"} or platform not in {"TM", "DY", "JD", "TTL"}:
        return {
            "ok": False,
            "markdown": "当前业务范围无法执行多观察期同比分析。",
            "meta": {"error_code": "invalid_multi_period_scope", "document_ready": False},
        }
    if runner is None:
        runner = _runner_for(
            subject_scope=subject_scope, platform=platform,
            segment=segment, category=category,
        )

    completed = []
    for index, (focus, baseline) in enumerate(zip(analysis, baselines), 1):
        period = ResolvedPeriod(
            focus.get("label") or f"观察期{index}",
            start_date=focus.get("start_date"), end_date=focus.get("end_date"),
            comparison_start=baseline.get("start_date"),
            comparison_end=baseline.get("end_date"),
        )
        if on_progress:
            on_progress(f"正在分析第{index}/{len(analysis)}个观察期：{period}…")
        result = runner(
            brand, period, brand_aliases=brand_aliases, on_progress=on_progress,
        )
        if not result.get("ok"):
            return {
                "ok": False,
                "markdown": f"{period}分析失败：{result.get('markdown') or '未知错误'}",
                "meta": {
                    **dict(result.get("meta") or {}),
                    "failed_observation": str(period),
                    "document_ready": False,
                },
            }
        total = _business_total(result, platform, subject_scope)
        if not total:
            return {
                "ok": False,
                "markdown": f"{period}没有返回可比较的品牌整体GMV。",
                "meta": {"error_code": "missing_multi_period_total", "document_ready": False},
            }
        completed.append({
            "label": str(period), "period": period, "baseline": baseline,
            "total": total, "markdown": result.get("markdown") or "",
            "meta": dict(result.get("meta") or {}),
        })

    rows = [
        "| 观察期 | 本期GMV | 去年同期GMV | 同比 | GMV增减额 |",
        "|---|---:|---:|---:|---:|",
    ]
    for item in completed:
        total = item["total"]
        rows.append(
            f"| {item['label']} | {_fmt_gmv(total.get('gmv_current'))} | "
            f"{_fmt_gmv(total.get('gmv_prior'))} | {_fmt_evol(total.get('evol'))} | "
            f"{_fmt_gmv(total.get('gmv_change'))} |"
        )
    first_evol = completed[0]["total"].get("evol")
    last_evol = completed[-1]["total"].get("evol")
    if first_evol is None or last_evol is None:
        comparison_line = "首尾观察期存在新增或缺失基期，无法计算同比变化百分点。"
    else:
        delta_pp = (float(last_evol) - float(first_evol)) * 100
        direction = "扩大" if delta_pp >= 0 else "收窄"
        comparison_line = (
            f"{completed[-1]['label']}同比较{completed[0]['label']}"
            f"{direction} {delta_pp:+.1f}pp。"
        )
    details = "\n\n".join(
        f"## {item['label']}详细分析\n\n{item['markdown']}" for item in completed
    )
    markdown = "\n".join([
        "# 多观察期同比比较", "", *rows, "", comparison_line, "", details,
    ])
    return {
        "ok": True,
        "markdown": markdown,
        "meta": {
            "brand": brand or None, "platform": platform,
            "subject_scope": subject_scope, "segment": segment,
            "category": category,
            "period": "、".join(item["label"] for item in completed),
            "comparison_spec": dict(comparison_spec),
            "document_ready": True,
            "last_result_cache": {
                "multi_period_business_results": [
                    {"label": item["label"], "total": item["total"]}
                    for item in completed
                ],
            },
        },
    }
