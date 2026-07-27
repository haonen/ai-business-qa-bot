from __future__ import annotations

import pandas as pd

from bot.tools.common import filter_sku, split_years, tool
from bot.utils import safe_div, safe_evol


@tool
def query_sku_list(
    brand: str,
    period: str,
    category: str | None = None,
    kol_driver: str | None = None,
    link_type: str | None = None,
    series: str | None = None,
    function_tag: str | None = None,
    top_n: int = 20,
) -> dict:
    """Top链接明细，至少指定一个下钻筛选。"""
    if not any([category, kol_driver, link_type, series, function_tag]):
        return {"error": "missing_filter", "message": "请至少指定品类、渠道、链接类型、系列或功能线之一"}
    try:
        df = filter_sku(brand, period, category=category, kol_driver=kol_driver, link_type=link_type, series=series, function_tag=function_tag)
        if df.empty:
            return {"error": "no_data", "message": "指定条件下无SKU数据"}
        df25, df26 = split_years(df, "bus_date", period)
        if df26.empty:
            return {"error": "no_data", "message": "2026指定时间段内无SKU数据"}
        total_26 = float(df26["gmv"].sum())
        total_25 = float(df25["gmv"].sum())
        agg26 = df26.groupby("item_id", dropna=False).agg(
            gmv_2026=("gmv", "sum"),
            unit_2026=("unit", "sum"),
            product_title=("product_title", "first"),
            kol_driver=("kol_driver", "first"),
            link_type=("link_type", "first"),
            category_cn=("category_cn", "first"),
        )
        agg25 = df25.groupby("item_id", dropna=False).agg(gmv_2025=("gmv", "sum"), unit_2025=("unit", "sum"))
        merged = agg26.join(agg25, how="left").fillna({"gmv_2025": 0, "unit_2025": 0})
        merged = merged.sort_values("gmv_2026", ascending=False).head(int(top_n)).reset_index()
        top_skus = []
        for _, row in merged.iterrows():
            g26 = float(row["gmv_2026"] or 0)
            g25 = float(row["gmv_2025"] or 0)
            u26 = float(row["unit_2026"] or 0)
            u25 = float(row["unit_2025"] or 0)
            top_skus.append({
                "item_id": str(row["item_id"]),
                "product_title": row["product_title"],
                "category_cn": row["category_cn"],
                "kol_driver": row["kol_driver"] or "Non-KOL",
                "link_type": row["link_type"] or "其他",
                "gmv_2026": round(g26),
                "gmv_2025": round(g25),
                "unit_2026": round(u26),
                "unit_2025": round(u25),
                "atv_2026": round(g26 / u26, 2) if u26 else None,
                "atv_2025": round(g25 / u25, 2) if u25 else None,
                "unit": round(u26),
                "atv": round(g26 / u26, 2) if u26 else None,
                "weight": safe_div(g26, total_26),
                "evol": safe_evol(g26, g25),
            })
        link_summary = []
        lt = df26.groupby("link_type", dropna=False).agg(gmv=("gmv", "sum"), unit=("unit", "sum")).reset_index()
        for _, row in lt.sort_values("gmv", ascending=False).iterrows():
            g = float(row["gmv"] or 0)
            u = float(row["unit"] or 0)
            link_summary.append({
                "link_type": row["link_type"] or "其他",
                "gmv_2026": round(g),
                "unit_2026": round(u),
                "atv_2026": round(g / u, 2) if u else None,
                "weight": safe_div(g, total_26),
            })
        return {
            "brand": brand,
            "period": period,
            "category": category,
            "series": series,
            "kol_driver": kol_driver,
            "link_type": link_type,
            "function_tag": function_tag,
            "category_total": {
                "gmv_2026": round(total_26),
                "gmv_2025": round(total_25),
                "evol": safe_evol(total_26, total_25),
            },
            "top_skus": top_skus,
            "link_type_summary": link_summary,
        }
    except Exception as exc:
        return {"error": "execution_error", "message": str(exc)}
