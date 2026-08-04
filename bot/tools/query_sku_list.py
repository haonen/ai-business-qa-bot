from __future__ import annotations

import pandas as pd

from bot.tools.common import filter_sku, split_periods, tool
from bot.utils import safe_div, safe_evol


@tool
def query_sku_list(
    brand: str,
    period: str,
    category: str | None = None,
    key_driver: str | None = None,
    series: str | None = None,
    function_tag: str | None = None,
    top_n: int = 20,
    brand_aliases: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """Top链接明细，至少指定一个下钻筛选。"""
    if not any([category, key_driver, series, function_tag]):
        return {"error": "missing_filter", "message": "请至少指定品类、渠道、系列或功能线之一"}
    try:
        df = filter_sku(
            brand,
            period,
            category=category,
            key_driver=key_driver,
            series=series,
            function_tag=function_tag,
            brand_aliases=brand_aliases,
        )
        if df.empty:
            return {"error": "no_data", "message": "指定条件下无SKU数据"}
        context = dict(df.attrs.get("ec_context") or {})
        prior_df, current_df = split_periods(df, "bus_date")
        if current_df.empty:
            return {"error": "no_data", "message": "本期指定条件下无SKU数据"}
        total_current = float(current_df["gmv"].sum())
        total_prior = float(prior_df["gmv"].sum())
        current_agg = current_df.groupby("item_id", dropna=False).agg(
            gmv_current=("gmv", "sum"),
            unit_current=("unit", "sum"),
            product_title=("product_title", "first"),
            key_driver=("key_driver", "first"),
            category_cn=("category_cn", "first"),
        )
        prior_agg = prior_df.groupby("item_id", dropna=False).agg(gmv_prior=("gmv", "sum"), unit_prior=("unit", "sum"))
        merged = current_agg.join(prior_agg, how="left").fillna({"gmv_prior": 0, "unit_prior": 0})
        merged = merged.sort_values("gmv_current", ascending=False).head(int(top_n)).reset_index()
        top_skus = []
        for _, row in merged.iterrows():
            g26 = float(row["gmv_current"] or 0)
            g25 = float(row["gmv_prior"] or 0)
            u26 = float(row["unit_current"] or 0)
            u25 = float(row["unit_prior"] or 0)
            top_skus.append({
                "item_id": str(row["item_id"]),
                "product_title": row["product_title"],
                "category_cn": row["category_cn"],
                "key_driver": row["key_driver"] or "其他",
                "gmv_current": round(g26),
                "gmv_prior": round(g25),
                "unit_current": round(u26),
                "unit_prior": round(u25),
                "atv_current": round(g26 / u26, 2) if u26 else None,
                "atv_prior": round(g25 / u25, 2) if u25 else None,
                "unit": round(u26),
                "atv": round(g26 / u26, 2) if u26 else None,
                "weight": safe_div(g26, total_current),
                "evol": safe_evol(g26, g25),
            })
        return {
            "brand": context.get("input_brand", brand),
            "input_brand": brand,
            "source_brand": context.get("source_brand"),
            "period": period,
            "period_meta": context,
            "category": category,
            "series": series,
            "key_driver": key_driver,
            "function_tag": function_tag,
            "category_total": {
                "gmv_current": round(total_current),
                "gmv_prior": round(total_prior),
                "evol": safe_evol(total_current, total_prior),
            },
            "top_skus": top_skus,
        }
    except Exception as exc:
        return {"error": "execution_error", "message": str(exc)}
