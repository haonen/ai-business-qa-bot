from __future__ import annotations

import pandas as pd

from bot.db.connection import fetch_df
from bot.tools.common import tool
from bot.utils import safe_evol


def _month_key(value) -> str:
    return pd.Timestamp(value).strftime("%Y-%m")


def _requested_months(start_month: str, end_month: str) -> list[str]:
    return [p.strftime("%Y-%m") for p in pd.period_range(start_month, end_month, freq="M")]


@tool
def query_social_search(brand: str, start_month: str, end_month: str) -> dict:
    """查询指定品牌从年初到结束月的Social Search品牌月度和Category明细。"""
    try:
        df = fetch_df(
            """
            SELECT
                report_month,
                grain_level,
                brand,
                category,
                current_search_index,
                previous_search_index,
                calculated_yoy_rate
            FROM ai_bot_media_search_index
            WHERE brand = :brand
              AND report_month BETWEEN :start_month AND :end_month
            ORDER BY report_month, grain_level, current_search_index DESC
            """,
            {"brand": brand, "start_month": start_month, "end_month": end_month},
        )
        if df.empty:
            return {
                "error": "no_data",
                "message": f"Social Search中没有找到品牌“{brand}”在指定期间的数据。",
                "brand": brand,
            }

        requested = _requested_months(start_month, end_month)
        brand_df = df[df["grain_level"] == "brand"].copy()
        category_df = df[df["grain_level"] == "brand_category"].copy()
        duplicate_months = [
            month
            for month, count in brand_df.groupby(brand_df["report_month"].map(_month_key)).size().items()
            if count > 1
        ]
        if duplicate_months:
            return {
                "error": "duplicate_brand_rows",
                "message": f"品牌粒度搜索数据存在重复月份：{', '.join(duplicate_months)}。",
            }

        brand_lookup = {
            _month_key(row["report_month"]): row
            for _, row in brand_df.iterrows()
        }
        category_months = {
            _month_key(value) for value in category_df["report_month"].dropna()
        }
        monthly = []
        for month in requested:
            row = brand_lookup.get(month)
            if row is None:
                monthly.append({
                    "month": month,
                    "actual": None,
                    "previous_actual": None,
                    "evol": None,
                    "grain": "Category only" if month in category_months else "No data",
                })
                continue
            actual = float(row["current_search_index"] or 0)
            prior = float(row["previous_search_index"] or 0)
            monthly.append({
                "month": month,
                "actual": round(actual),
                "previous_actual": round(prior),
                "evol": safe_evol(actual, prior),
                "grain": "Brand",
            })

        categories = []
        for _, row in category_df.iterrows():
            actual = float(row["current_search_index"] or 0)
            prior = float(row["previous_search_index"] or 0)
            categories.append({
                "month": _month_key(row["report_month"]),
                "category": str(row["category"] or "其他"),
                "actual": round(actual),
                "previous_actual": round(prior),
                "evol": safe_evol(actual, prior),
            })

        return {
            "brand": brand,
            "date_range": {"start": start_month, "end": end_month},
            "monthly": monthly,
            "categories": categories,
            "coverage": {
                "requested_months": requested,
                "brand_months": [row["month"] for row in monthly if row["grain"] == "Brand"],
                "category_only_months": [row["month"] for row in monthly if row["grain"] == "Category only"],
                "missing_months": [row["month"] for row in monthly if row["grain"] == "No data"],
            },
        }
    except Exception as exc:
        return {"error": "execution_error", "message": str(exc)}
