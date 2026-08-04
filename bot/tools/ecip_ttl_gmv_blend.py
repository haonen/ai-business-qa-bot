from __future__ import annotations

import calendar
from datetime import date
from typing import Callable

import pandas as pd


_TTL_CATEGORY_SQL = "'Skincare', 'Hair', 'Makeup + Fragrance', 'Makeup+Fragrance'"


def _month_slices(start: str, end: str) -> list[dict]:
    start_date = date.fromisoformat(str(start)[:10])
    end_date = date.fromisoformat(str(end)[:10])
    rows = []
    year, month = start_date.year, start_date.month
    while (year, month) <= (end_date.year, end_date.month):
        month_start = date(year, month, 1)
        month_end = date(year, month, calendar.monthrange(year, month)[1])
        rows.append({
            "month": f"{year:04d}-{month:02d}",
            "start": max(start_date, month_start).isoformat(),
            "end": min(end_date, month_end).isoformat(),
            "full_month": start_date <= month_start and end_date >= month_end,
        })
        month += 1
        if month == 13:
            year += 1
            month = 1
    return rows


def _to_lookup(df: pd.DataFrame) -> dict[tuple[str, str], dict]:
    lookup = {}
    if df.empty:
        return lookup
    for _, row in df.iterrows():
        key = (str(row.get("period_key") or ""), str(row.get("source_month") or ""))
        lookup[key] = {
            "gmv": float(row.get("gmv") or 0),
            "row_count": int(row.get("row_count") or 0),
        }
    return lookup


def query_blended_tmall_ttl_gmv(
    fetcher: Callable,
    *,
    brand: str,
    current_start: str,
    current_end: str,
    prior_start: str,
    prior_end: str,
) -> dict:
    """Use monthly data for complete available months and daily data elsewhere."""
    monthly_sql = f"""
        SELECT
          CASE WHEN bus_date BETWEEN :current_start_iso AND :current_end_iso
               THEN 'current' ELSE 'prior' END AS period_key,
          DATE_FORMAT(bus_date, '%Y-%m') AS source_month,
          COUNT(*) AS row_count,
          COALESCE(SUM(gmv), 0) AS gmv
        FROM three_platform_store_rank_monthly
        WHERE brand_name = :brand
          AND UPPER(TRIM(platform)) IN ('TM', 'TMALL')
          AND (
            bus_date BETWEEN :current_start_iso AND :current_end_iso
            OR bus_date BETWEEN :prior_start_iso AND :prior_end_iso
          )
          AND category_EN_level_1 IN ({_TTL_CATEGORY_SQL})
        GROUP BY period_key, DATE_FORMAT(bus_date, '%Y-%m')
    """
    daily_sql = f"""
        SELECT
          CASE WHEN bus_date BETWEEN :current_start_slash AND :current_end_slash
               THEN 'current' ELSE 'prior' END AS period_key,
          DATE_FORMAT(STR_TO_DATE(bus_date, '%Y/%m/%d'), '%Y-%m') AS source_month,
          COUNT(*) AS row_count,
          COALESCE(SUM(CAST(REPLACE(NULLIF(TRIM(gmv), ''), ',', '')
            AS DECIMAL(24, 4))), 0) AS gmv
        FROM tmall_store_ranking_day_jiashicang
        WHERE brand_name = :brand
          AND (
            bus_date BETWEEN :current_start_slash AND :current_end_slash
            OR bus_date BETWEEN :prior_start_slash AND :prior_end_slash
          )
          AND category_EN_level_1 IN ({_TTL_CATEGORY_SQL})
        GROUP BY period_key,
          DATE_FORMAT(STR_TO_DATE(bus_date, '%Y/%m/%d'), '%Y-%m')
    """
    params = {
        "brand": brand,
        "current_start_iso": current_start,
        "current_end_iso": current_end,
        "prior_start_iso": prior_start,
        "prior_end_iso": prior_end,
        "current_start_slash": current_start.replace("-", "/"),
        "current_end_slash": current_end.replace("-", "/"),
        "prior_start_slash": prior_start.replace("-", "/"),
        "prior_end_slash": prior_end.replace("-", "/"),
    }
    monthly_lookup = _to_lookup(fetcher(monthly_sql, params))
    daily_lookup = _to_lookup(fetcher(daily_sql, params))
    period_ranges = {
        "current": (current_start, current_end),
        "prior": (prior_start, prior_end),
    }
    rows = []
    monthly_used, daily_used, missing = [], [], []
    for period_key, (start, end) in period_ranges.items():
        for item in _month_slices(start, end):
            lookup_key = (period_key, item["month"])
            monthly_value = monthly_lookup.get(lookup_key)
            daily_value = daily_lookup.get(lookup_key)
            if item["full_month"] and monthly_value and monthly_value["row_count"] > 0:
                selected = monthly_value
                source = "monthly"
                monthly_used.append(item["month"])
            else:
                selected = daily_value or {"gmv": 0.0, "row_count": 0}
                source = "daily"
                if selected["row_count"] > 0:
                    daily_used.append(item["month"])
                else:
                    missing.append(item["month"])
            rows.append({
                "period_key": period_key,
                "source_month": item["month"],
                "range_start": item["start"],
                "range_end": item["end"],
                "full_month": item["full_month"],
                "source": source,
                "gmv": selected["gmv"],
                "row_count": selected["row_count"],
            })
    totals = {}
    for period_key in ("current", "prior"):
        period_rows = [row for row in rows if row["period_key"] == period_key]
        totals[period_key] = {
            "gmv": sum(row["gmv"] for row in period_rows),
            "row_count": sum(row["row_count"] for row in period_rows),
        }
    return {
        "rows": rows,
        "totals": totals,
        "coverage": {
            "monthly_used": sorted(set(monthly_used)),
            "daily_used": sorted(set(daily_used)),
            "missing": sorted(set(missing)),
        },
        "sql_meta": {
            "monthly_table": "three_platform_store_rank_monthly",
            "daily_table": "tmall_store_ranking_day_jiashicang",
            "monthly_platform": ["TM", "TMALL"],
        },
    }
