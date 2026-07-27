from __future__ import annotations

from collections import defaultdict

from bot.tools.common import filter_sku, match_scene_tags, split_years, tool
from bot.utils import safe_div


@tool
def query_scene_tag(
    brand: str,
    period: str,
    kol_driver: str | None = None,
    category: str | None = None,
) -> dict:
    """从链接标题中提取场景标签的GMV分布。"""
    try:
        df = filter_sku(brand, period, category=category, kol_driver=kol_driver)
        if df.empty:
            return {"error": "no_data", "message": "指定条件下无SKU数据"}
        _, df26 = split_years(df, "bus_date", period)
        total = float(df26["gmv"].sum()) if not df26.empty else 0.0
        buckets = defaultdict(lambda: {"gmv": 0.0, "unit": 0.0, "link_count": 0})
        for _, row in df26.iterrows():
            tags = match_scene_tags(row.get("product_title"))
            if not tags:
                tags = ["未识别场景"]
            for tag in tags:
                buckets[tag]["gmv"] += float(row.get("gmv") or 0)
                buckets[tag]["unit"] += float(row.get("unit") or 0)
                buckets[tag]["link_count"] += 1
        rows = []
        for tag, item in buckets.items():
            g = item["gmv"]
            u = item["unit"]
            rows.append({
                "tag": tag,
                "gmv": round(g),
                "unit": round(u),
                "atv": round(g / u, 2) if u else None,
                "weight": safe_div(g, total),
                "link_count": item["link_count"],
            })
        return {
            "brand": brand,
            "period": period,
            "kol_driver": kol_driver,
            "category": category,
            "scene_tags": sorted(rows, key=lambda x: x["gmv"], reverse=True),
        }
    except Exception as exc:
        return {"error": "execution_error", "message": str(exc)}

