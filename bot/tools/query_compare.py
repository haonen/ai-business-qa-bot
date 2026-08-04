from __future__ import annotations

from bot.tools.common import combine_periods, filter_sku, split_periods, tool


@tool
def query_compare(
    brand_a: str,
    period_a: str,
    brand_b: str | None = None,
    period_b: str | None = None,
    dimension: str = "category",
) -> dict:
    """跨品牌/跨时段对比。dimension支持category/driver。"""
    try:
        group_map = {
            "category": ["category_cn"],
            "driver": ["key_driver"],
        }
        group_cols = group_map.get(dimension, ["category_cn"])

        def one(label: str, brand: str, period: str):
            df = filter_sku(brand, period)
            if df.empty:
                return {"label": label, "brand": brand, "period": period, "rows": [], "total": {}}
            prior_df, current_df = split_periods(df, "bus_date")
            rows, total = combine_periods(prior_df, current_df, group_cols)
            return {
                "label": label,
                "brand": df.attrs.get("ec_context", {}).get("source_brand", brand),
                "period": period,
                "period_meta": df.attrs.get("ec_context", {}),
                "rows": rows,
                "total": total,
            }

        return {
            "dimension": dimension,
            "a": one("A", brand_a, period_a),
            "b": one("B", brand_b or brand_a, period_b or period_a),
        }
    except Exception as exc:
        return {"error": "execution_error", "message": str(exc)}
