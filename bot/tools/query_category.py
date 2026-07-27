from __future__ import annotations

from bot.tools.common import combine_two_years, filter_sku, split_years, tool
from bot.utils import parse_period


@tool
def query_category(
    brand: str,
    period: str,
    kol_driver: str | None = None,
    link_type: str | None = None,
    series: str | None = None,
    function_tag: str | None = None,
) -> dict:
    """品类GMV/unit/ATV聚合，两年对比。"""
    try:
        dr = parse_period(period)
        df = filter_sku(brand, period, kol_driver=kol_driver, link_type=link_type, series=series, function_tag=function_tag)
        if df.empty:
            return {"error": "no_data", "message": "指定条件下无SKU数据"}
        df25, df26 = split_years(df, "bus_date", period)
        rows, total = combine_two_years(df25, df26, ["category_cn"])
        for row in rows:
            row["category_cn"] = row.pop("category_cn")
        return {
            "brand": brand,
            "period": period,
            "kol_driver": kol_driver,
            "link_type": link_type,
            "series": series,
            "function_tag": function_tag,
            "date_range": {"y2026": list(dr["y2026"]), "y2025": list(dr["y2025"])},
            "total": total,
            "categories": rows,
        }
    except Exception as exc:
        return {"error": "execution_error", "message": str(exc)}

