from __future__ import annotations

from datetime import date

import pandas as pd

from bot.db.connection import fetch_df
from bot.tools.common import tool
from bot.utils import safe_evol


def _year_month(value: str) -> tuple[int, int]:
    parsed = date.fromisoformat(str(value)[:10])
    return parsed.year, parsed.month


@tool
def query_ec_nso(
    brand: str,
    focus_start: str,
    focus_end: str,
    prior_start: str,
    prior_end: str,
) -> dict:
    """查询EC Consolidation中platform=TTL的本期及同期NSO。"""
    try:
        focus_start_year, focus_start_month = _year_month(focus_start)
        focus_end_year, focus_end_month = _year_month(focus_end)
        prior_start_year, prior_start_month = _year_month(prior_start)
        prior_end_year, prior_end_month = _year_month(prior_end)
        df = fetch_df(
            """
            SELECT
                'current' AS period_type,
                year,
                month,
                SUM(Sales) AS nso,
                COUNT(*) AS row_count
            FROM top_brands_total_ec
            WHERE Brand = :brand
              AND platform = 'TTL'
              AND (
                    year > :focus_start_year
                    OR (year = :focus_start_year AND month >= :focus_start_month)
                  )
              AND (
                    year < :focus_end_year
                    OR (year = :focus_end_year AND month <= :focus_end_month)
                  )
            GROUP BY year, month

            UNION ALL

            SELECT
                'prior' AS period_type,
                year,
                month,
                SUM(Sales) AS nso,
                COUNT(*) AS row_count
            FROM top_brands_total_ec
            WHERE Brand = :brand
              AND platform = 'TTL'
              AND (
                    year > :prior_start_year
                    OR (year = :prior_start_year AND month >= :prior_start_month)
                  )
              AND (
                    year < :prior_end_year
                    OR (year = :prior_end_year AND month <= :prior_end_month)
                  )
            GROUP BY year, month
            """,
            {
                "brand": brand,
                "focus_start_year": focus_start_year,
                "focus_start_month": focus_start_month,
                "focus_end_year": focus_end_year,
                "focus_end_month": focus_end_month,
                "prior_start_year": prior_start_year,
                "prior_start_month": prior_start_month,
                "prior_end_year": prior_end_year,
                "prior_end_month": prior_end_month,
            },
        )
        values = {"current": 0.0, "prior": 0.0}
        row_counts = {"current": 0, "prior": 0}
        months = {"current": [], "prior": []}
        if not df.empty:
            df["year"] = pd.to_numeric(df["year"], errors="coerce")
            df["month"] = pd.to_numeric(df["month"], errors="coerce")
            df["nso"] = pd.to_numeric(df["nso"], errors="coerce").fillna(0.0)
            df["row_count"] = pd.to_numeric(
                df["row_count"], errors="coerce"
            ).fillna(0).astype(int)
            for _, row in df.iterrows():
                period_type = str(row.get("period_type") or "")
                if period_type not in values:
                    continue
                values[period_type] += float(row["nso"])
                row_counts[period_type] += int(row["row_count"])
                if not pd.isna(row["year"]) and not pd.isna(row["month"]):
                    months[period_type].append(
                        f"{int(row['year']):04d}-{int(row['month']):02d}"
                    )
        if row_counts["current"] == 0 and row_counts["prior"] == 0:
            return {
                "error": "no_data",
                "message": f"EC Consolidation中没有品牌“{brand}”的TTL NSO数据。",
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
            "platform": "TTL",
            "date_range": {
                "current": [focus_start, focus_end],
                "prior": [prior_start, prior_end],
            },
            "nso_actual": round(values["current"], 2),
            "nso_prior": round(values["prior"], 2),
            "evol": (
                safe_evol(values["current"], values["prior"])
                if status == "ok"
                else None
            ),
            "comparison_status": status,
            "coverage": {
                "current_months": sorted(set(months["current"])),
                "prior_months": sorted(set(months["prior"])),
                "current_rows": row_counts["current"],
                "prior_rows": row_counts["prior"],
            },
        }
    except Exception as exc:
        return {"error": "execution_error", "message": str(exc)}
