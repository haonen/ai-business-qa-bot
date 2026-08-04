from __future__ import annotations

from collections import defaultdict

from bot.tools.common import filter_sku, match_scene_tags, split_periods, tool
from bot.utils import safe_div


@tool
def query_scene_tag(
    brand: str,
    period: str,
    key_driver: str | None = None,
    category: str | None = None,
    brand_aliases: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """从链接标题中提取场景标签的GMV分布。"""
    try:
        df = filter_sku(
            brand,
            period,
            category=category,
            key_driver=key_driver,
            brand_aliases=brand_aliases,
        )
        if df.empty:
            return {"error": "no_data", "message": "指定条件下无SKU数据"}
        _, current_df = split_periods(df, "bus_date")
        total = float(current_df["gmv"].sum()) if not current_df.empty else 0.0
        buckets = defaultdict(lambda: {"gmv": 0.0, "unit": 0.0, "link_count": 0})
        for _, row in current_df.iterrows():
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
            "brand": df.attrs.get("ec_context", {}).get("input_brand", brand),
            "source_brand": df.attrs.get("ec_context", {}).get("source_brand"),
            "period": period,
            "period_meta": df.attrs.get("ec_context", {}),
            "key_driver": key_driver,
            "category": category,
            "scene_tags": sorted(rows, key=lambda x: x["gmv"], reverse=True),
        }
    except Exception as exc:
        return {"error": "execution_error", "message": str(exc)}
