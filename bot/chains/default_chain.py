from __future__ import annotations

from bot.chains.adapters import attach_series_to_sku, build_fraud_result
from bot.formatter import format_report
from bot.tools import query_category, query_driver, query_kol, query_series, query_sku_list


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


def run_default_chain(brand: str, period: str = "618", on_progress=None) -> dict:
    if on_progress:
        on_progress("正在分析品类结构和生意质检…")
    category_result = query_category(brand=brand, period=period)
    if category_result.get("error"):
        return {"ok": False, "markdown": category_result.get("message", "品类分析失败"), "meta": {"brand": brand, "period": period}}

    selected_category = select_drilldown_target(category_result.get("categories", []))

    if on_progress:
        on_progress("已完成品类下钻，正在分析系列和Top链接…")
    series_result = query_series(brand=brand, period=period, category=selected_category)
    sku_result = query_sku_list(brand=brand, period=period, category=selected_category, top_n=20)
    sku_result = attach_series_to_sku(sku_result, series_result)

    selected_series = ""
    product_lines = sku_result.get("product_lines") or []
    if product_lines:
        selected_series = product_lines[0].get("product_line") or product_lines[0].get("series") or ""

    if on_progress:
        on_progress("正在分析渠道贡献和直播参考数据…")
    driver_result = query_driver(brand=brand, period=period)
    query_kol(brand=brand, period=period)  # Warm/validate the reference path; driver_result already carries reference summary.
    fraud_result = build_fraud_result(brand, period)

    markdown = format_report(
        category_result=category_result,
        driver_result=driver_result,
        sku_result=sku_result,
        selected_category=selected_category,
        selected_series=selected_series,
        fraud_result=fraud_result,
    )
    return {
        "ok": True,
        "markdown": markdown,
        "meta": {
            "brand": brand,
            "period": period,
            "selected_category": selected_category,
            "selected_series": selected_series,
            "last_result_cache": {
                "category_result": category_result,
                "driver_result": driver_result,
                "sku_result": sku_result,
                "series_result": series_result,
                "fraud_result": fraud_result,
            },
        },
    }
