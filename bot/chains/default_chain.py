from __future__ import annotations

from bot.chains.adapters import attach_series_to_sku, build_fraud_result
from bot.formatter import format_report
from bot.tools import (
    query_category,
    query_driver,
    query_ecip_tmall_gmv,
    query_series,
    query_sku_list,
)


def select_drilldown_target(categories: list[dict]) -> str:
    if not categories:
        return ""
    candidates = [
        c for c in categories
        if (c.get("weight") or 0) >= 0.03
    ] or categories
    top_weight = max((c.get("weight") or 0) for c in candidates)
    core = [c for c in candidates if (c.get("weight") or 0) >= top_weight * 0.8] or candidates

    def yoy_abs(row: dict) -> float:
        evol = row.get("evol")
        if evol is None:
            return 0.0
        return abs(float(evol))

    return max(core, key=lambda c: ((c.get("weight") or 0), yoy_abs(c))).get("category_cn", "")


def run_default_chain(
    brand: str,
    period: str,
    brand_aliases: list[str] | tuple[str, ...] | None = None,
    on_progress=None,
) -> dict:
    if not period:
        return {
            "ok": False,
            "markdown": "请明确需要分析的时间段。",
            "meta": {"brand": brand, "period": None, "document_ready": False},
        }
    if on_progress:
        on_progress("正在分析品类结构和生意质检…")
    aliases = list(brand_aliases or [])
    category_result = query_category(
        brand=brand,
        period=period,
        brand_aliases=aliases,
    )
    if category_result.get("error"):
        return {
            "ok": False,
            "markdown": category_result.get("message", "品类分析失败"),
            "meta": {
                "brand": brand,
                "period": period,
                "document_ready": False,
            },
        }

    ttl_result = query_ecip_tmall_gmv(
        brand=brand,
        period=period,
        brand_aliases=aliases,
    )
    if ttl_result.get("error"):
        return {
            "ok": False,
            "markdown": ttl_result.get("message", "ECIP MASS TTL GMV查询失败"),
            "meta": {
                "brand": brand,
                "period": period,
                "document_ready": False,
            },
        }
    category_result["overall_total"] = ttl_result.get("total") or {}

    selected_category = select_drilldown_target(category_result.get("categories", []))

    if on_progress:
        on_progress("已完成品类下钻，正在分析系列和Top链接…")
    series_result = query_series(
        brand=brand,
        period=period,
        category=selected_category,
        brand_aliases=aliases,
    )
    sku_result = query_sku_list(
        brand=brand,
        period=period,
        category=selected_category,
        top_n=20,
        brand_aliases=aliases,
    )
    sku_result = attach_series_to_sku(sku_result, series_result)

    selected_series = ""
    product_lines = sku_result.get("product_lines") or []
    if product_lines:
        selected_series = product_lines[0].get("product_line") or product_lines[0].get("series") or ""

    if on_progress:
        on_progress("正在分析渠道贡献和渠道货品结构…")
    driver_result = query_driver(brand=brand, period=period, brand_aliases=aliases)
    fraud_result = build_fraud_result(brand, period, brand_aliases=aliases)

    markdown = format_report(
        category_result=category_result,
        driver_result=driver_result,
        sku_result=sku_result,
        selected_category=selected_category,
        selected_series=selected_series,
        fraud_result=fraud_result,
        ttl_result=ttl_result,
    )
    return {
        "ok": True,
        "markdown": markdown,
        "meta": {
            "brand": category_result.get("brand", brand),
            "tmall_brand": category_result.get("source_brand"),
            "brand_aliases": aliases,
            "input_brand": brand,
            "period": period,
            "period_meta": category_result.get("period_meta", {}),
            "document_ready": True,
            "selected_category": selected_category,
            "selected_series": selected_series,
            "last_result_cache": {
                "category_result": category_result,
                "driver_result": driver_result,
                "sku_result": sku_result,
                "series_result": series_result,
                "fraud_result": fraud_result,
                "ttl_result": ttl_result,
            },
        },
    }
