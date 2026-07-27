from __future__ import annotations

from bot.tools.common import combine_two_years, filter_sku, split_years, tool


@tool
def query_compare(
    brand_a: str,
    period_a: str,
    brand_b: str | None = None,
    period_b: str | None = None,
    dimension: str = "category",
) -> dict:
    """跨品牌/跨时段对比。dimension支持category/driver/link_type。"""
    try:
        group_map = {
            "category": ["category_cn"],
            "driver": ["kol_driver"],
            "link_type": ["link_type"],
        }
        group_cols = group_map.get(dimension, ["category_cn"])

        def one(label: str, brand: str, period: str):
            df = filter_sku(brand, period)
            if df.empty:
                return {"label": label, "brand": brand, "period": period, "rows": [], "total": {}}
            df25, df26 = split_years(df, "bus_date", period)
            rows, total = combine_two_years(df25, df26, group_cols)
            return {"label": label, "brand": brand, "period": period, "rows": rows, "total": total}

        return {
            "dimension": dimension,
            "a": one("A", brand_a, period_a),
            "b": one("B", brand_b or brand_a, period_b or period_a),
        }
    except Exception as exc:
        return {"error": "execution_error", "message": str(exc)}

