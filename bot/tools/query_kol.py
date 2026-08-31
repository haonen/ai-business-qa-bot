from __future__ import annotations

from bot.tools.common import combine_two_years, filter_kol, split_years, tool


@tool
def query_kol(brand: str, period: str, kol_type: str | None = None) -> dict:
    """情报通直播参考数据。"""
    try:
        df = filter_kol(brand, period, kol_type=kol_type)
        if df.empty:
            return {"error": "no_data", "message": "指定条件下无KOL直播数据"}
        df25, df26 = split_years(df, "live_start_date", period)
        rows, total = combine_two_years(df25, df26, ["kol_type"], "live_sales_amount", "live_sales_unit")
        return {
            "brand": brand,
            "period": period,
            "kol_type": kol_type,
            "note": "以下数据来自情报通，仅含直播渠道，作为参考口径。",
            "total": total,
            "breakdown": rows,
        }
    except Exception as exc:
        return {"error": "execution_error", "message": str(exc)}

