from __future__ import annotations

import pandas as pd

from bot.db.connection import fetch_df
from bot.tools.common import tool
from bot.utils import safe_evol


@tool
def query_tmall_gmv(
    brand: str,
    focus_start: str,
    focus_end: str,
    prior_start: str,
    prior_end: str,
) -> dict:
    """按bus_date和brand_name聚合Tmall GMV，返回当前期间与上年同期。"""
    try:
        df = fetch_df(
            """
            SELECT
                'current' AS period_type,
                SUM(gmv) AS gmv,
                COUNT(*) AS row_count,
                MIN(bus_date) AS min_date,
                MAX(bus_date) AS max_date
            FROM ai_bot_tmall_product_link FORCE INDEX (idx_tmall_brand_date)
            WHERE brand_name = :brand
              AND bus_date BETWEEN :focus_start AND :focus_end

            UNION ALL

            SELECT
                'prior' AS period_type,
                SUM(gmv) AS gmv,
                COUNT(*) AS row_count,
                MIN(bus_date) AS min_date,
                MAX(bus_date) AS max_date
            FROM ai_bot_tmall_product_link FORCE INDEX (idx_tmall_brand_date)
            WHERE brand_name = :brand
              AND bus_date BETWEEN :prior_start AND :prior_end
            """,
            {
                "brand": brand,
                "focus_start": focus_start,
                "focus_end": focus_end,
                "prior_start": prior_start,
                "prior_end": prior_end,
            },
        )
        values = {"current": 0.0, "prior": 0.0}
        row_counts = {"current": 0, "prior": 0}
        coverage = {"current": None, "prior": None}
        if not df.empty:
            for _, row in df.iterrows():
                key = str(row["period_type"])
                values[key] = 0.0 if pd.isna(row["gmv"]) else float(row["gmv"] or 0)
                row_counts[key] = int(row["row_count"] or 0)
                if not pd.isna(row["min_date"]) and not pd.isna(row["max_date"]):
                    coverage[key] = [
                        str(row["min_date"])[:10],
                        str(row["max_date"])[:10],
                    ]
        if not row_counts["current"] and not row_counts["prior"]:
            return {
                "error": "no_data",
                "message": f"Tmall GMV中没有找到品牌“{brand}”在指定期间的数据。",
                "brand": brand,
            }
        if row_counts["current"] == 0:
            status = "missing_current"
        elif row_counts["prior"] == 0:
            status = "missing_prior"
        elif values["prior"] == 0:
            status = "base_zero"
        else:
            status = "ok"
        return {
            "brand": brand,
            "matched_brand": brand,
            "date_range": {
                "current": [focus_start, focus_end],
                "prior": [prior_start, prior_end],
            },
            "gmv_actual": round(values["current"], 2),
            "gmv_prior": round(values["prior"], 2),
            "evol": safe_evol(values["current"], values["prior"]) if status == "ok" else None,
            "comparison_status": status,
            "coverage": {
                **coverage,
                "current_rows": row_counts["current"],
                "prior_rows": row_counts["prior"],
            },
        }
    except Exception as exc:
        return {"error": "execution_error", "message": str(exc)}
