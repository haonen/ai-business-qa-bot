from __future__ import annotations

from bot.tools.common import combine_periods, filter_sku, split_periods, tool


@tool
def query_category(
    brand: str,
    period: str,
    key_driver: str | None = None,
    series: str | None = None,
    function_tag: str | None = None,
    brand_aliases: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """品类GMV/unit/ATV聚合，两年对比。"""
    try:
        df = filter_sku(
            brand,
            period,
            key_driver=key_driver,
            series=series,
            function_tag=function_tag,
            brand_aliases=brand_aliases,
        )
        if df.empty:
            return {"error": "no_data", "message": "指定条件下无SKU数据"}
        context = dict(df.attrs.get("ec_context") or {})
        prior_df, current_df = split_periods(df, "bus_date")
        rows, total = combine_periods(prior_df, current_df, ["category_cn"])
        for row in rows:
            row["category_cn"] = row.pop("category_cn")
        return {
            "brand": context.get("input_brand", brand),
            "input_brand": brand,
            "source_brand": context.get("source_brand"),
            "period": period,
            "key_driver": key_driver,
            "series": series,
            "function_tag": function_tag,
            "period_meta": context,
            "total": total,
            "categories": rows,
        }
    except Exception as exc:
        return {"error": "execution_error", "message": str(exc)}
