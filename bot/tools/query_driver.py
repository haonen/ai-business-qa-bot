from __future__ import annotations

from bot.tools.common import combine_periods, filter_sku, split_periods, tool
from bot.tools.query_series import (
    _infer_series_from_title,
    _llm_series_fallback,
    _series_keywords_for_brand,
)
from bot.utils import safe_div, safe_evol


@tool
def query_driver(
    brand: str,
    period: str,
    category: str | None = None,
    series: str | None = None,
    function_tag: str | None = None,
    brand_aliases: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """按key_driver动态拆分渠道，并返回各渠道系列和Top SKU。"""
    try:
        df = filter_sku(
            brand,
            period,
            category=category,
            series=series,
            function_tag=function_tag,
            brand_aliases=brand_aliases,
        )
        if df.empty:
            return {"error": "no_data", "message": "指定条件下无SKU数据"}
        context = dict(df.attrs.get("ec_context") or {})
        prior_df, current_df = split_periods(df, "bus_date")
        rows, total = combine_periods(prior_df, current_df, ["key_driver"])
        drivers = sorted(rows, key=lambda row: row.get("gmv_current") or 0, reverse=True)

        analysis_brand = context.get("input_brand", brand)
        low_coverage = not _series_keywords_for_brand(analysis_brand)
        llm_candidates = _llm_series_fallback(
            analysis_brand,
            current_df.sort_values("gmv", ascending=False)["product_title"].dropna().head(50).tolist(),
        ) if low_coverage else []

        driver_top_skus = []
        for driver in drivers:
            driver_name = driver.get("key_driver") or "其他"
            current_driver = current_df[current_df["key_driver"].fillna("其他") == driver_name]
            prior_driver = prior_df[prior_df["key_driver"].fillna("其他") == driver_name]
            total_current = float(current_driver["gmv"].sum()) if not current_driver.empty else 0.0
            total_prior = float(prior_driver["gmv"].sum()) if not prior_driver.empty else 0.0
            if total_current <= 0:
                continue

            buckets: dict[str, dict] = {}
            for suffix, frame in (("prior", prior_driver), ("current", current_driver)):
                for _, row in frame.iterrows():
                    product_line, tag, source = _infer_series_from_title(
                        row.get("product_title"),
                        analysis_brand,
                        llm_candidates,
                    )
                    item = buckets.setdefault(product_line, {
                        "product_line": product_line,
                        "series": product_line,
                        "function_tag": tag,
                        "source": source,
                        "gmv_current": 0.0,
                        "gmv_prior": 0.0,
                        "unit_current": 0.0,
                        "unit_prior": 0.0,
                        "item_ids": set(),
                    })
                    item[f"gmv_{suffix}"] += float(row.get("gmv") or 0)
                    item[f"unit_{suffix}"] += float(row.get("unit") or 0)
                    if suffix == "current":
                        item["item_ids"].add(str(row.get("item_id")))

            product_lines = []
            for item in buckets.values():
                g_current = item["gmv_current"]
                g_prior = item["gmv_prior"]
                u_current = item["unit_current"]
                item.update({
                    "gmv_current": round(g_current),
                    "gmv_prior": round(g_prior),
                    "unit_current": round(u_current),
                    "unit_prior": round(item["unit_prior"]),
                    "atv_current": round(g_current / u_current, 2) if u_current else None,
                    "weight": safe_div(g_current, total_current),
                    "evol": safe_evol(g_current, g_prior),
                    "share_delta": (
                        round(g_current / total_current - g_prior / total_prior, 4)
                        if total_current and total_prior else None
                    ),
                    "item_ids": sorted(item["item_ids"]),
                })
                product_lines.append(item)

            sku_agg = (
                current_driver.groupby("item_id", dropna=False)
                .agg(
                    gmv_current=("gmv", "sum"),
                    unit=("unit", "sum"),
                    product_title=("product_title", "first"),
                    category_cn=("category_cn", "first"),
                )
                .sort_values("gmv_current", ascending=False)
                .head(10)
                .reset_index()
            )
            top_skus = []
            for _, row in sku_agg.iterrows():
                gmv = float(row["gmv_current"] or 0)
                unit = float(row["unit"] or 0)
                top_skus.append({
                    "item_id": str(row["item_id"]),
                    "product_title": row["product_title"],
                    "category_cn": row["category_cn"],
                    "key_driver": driver_name,
                    "gmv_current": round(gmv),
                    "weight": safe_div(gmv, total_current),
                    "unit": round(unit),
                    "atv": round(gmv / unit, 2) if unit else None,
                })

            driver_top_skus.append({
                "key_driver": driver_name,
                "total_gmv_current": round(total_current),
                "total_gmv_prior": round(total_prior),
                "product_lines": sorted(
                    product_lines,
                    key=lambda item: item.get("gmv_current") or 0,
                    reverse=True,
                ),
                "top_skus": top_skus,
            })

        return {
            "brand": context.get("input_brand", brand),
            "input_brand": brand,
            "source_brand": context.get("source_brand"),
            "period": period,
            "period_meta": context,
            "category": category,
            "series": series,
            "function_tag": function_tag,
            "driver_summary": {
                "total_gmv_current": total["gmv_current"],
                "total_gmv_prior": total["gmv_prior"],
                "total_unit_current": total["unit_current"],
                "total_unit_prior": total["unit_prior"],
                "drivers": drivers,
            },
            "driver_top_skus": driver_top_skus,
        }
    except Exception as exc:
        return {"error": "execution_error", "message": str(exc)}
