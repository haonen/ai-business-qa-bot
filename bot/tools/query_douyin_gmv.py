from __future__ import annotations

from bot.db.connection import fetch_df
from bot.tools.common import tool
from bot.utils import safe_evol


@tool
def query_douyin_gmv(
    brand: str,
    focus_start: str,
    focus_end: str,
    prior_start: str,
    prior_end: str,
) -> dict:
    """按业务日期和商品品牌聚合抖音销售额，返回当前期间与上年同期。"""
    try:
        df = fetch_df(
            """
            SELECT
                CASE WHEN `业务日期` BETWEEN :focus_start AND :focus_end
                     THEN 'current' ELSE 'prior' END AS period_type,
                SUM(`销售额`) AS gmv,
                COUNT(*) AS row_count,
                MIN(`业务日期`) AS min_date,
                MAX(`业务日期`) AS max_date
            FROM ai_bot_dy_product_link
            WHERE `商品品牌` = :brand
              AND (
                `业务日期` BETWEEN :focus_start AND :focus_end
                OR `业务日期` BETWEEN :prior_start AND :prior_end
              )
            GROUP BY period_type
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
                values[key] = float(row["gmv"] or 0)
                row_counts[key] = int(row["row_count"] or 0)
                coverage[key] = [
                    str(row["min_date"])[:10],
                    str(row["max_date"])[:10],
                ]
        if not row_counts["current"] and not row_counts["prior"]:
            return {
                "error": "no_data",
                "message": f"抖音GMV中没有找到品牌“{brand}”在指定期间的数据。",
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
            "evol": (
                safe_evol(values["current"], values["prior"])
                if status == "ok" else None
            ),
            "comparison_status": status,
            "coverage": {
                **coverage,
                "current_rows": row_counts["current"],
                "prior_rows": row_counts["prior"],
            },
        }
    except Exception as exc:
        return {"error": "execution_error", "message": str(exc)}
