from __future__ import annotations

import pandas as pd

from bot.db.connection import fetch_df
from bot.tools.common import tool
from bot.tools.market_common import (
    comparison_status,
    evol,
    expected_platforms,
    month_slices,
    monthly_business_date_sql,
    validate_scope,
)
from bot.utils import parse_ec_period


def _lookup(df: pd.DataFrame) -> dict[tuple[str, str, str], dict]:
    result: dict[tuple[str, str, str], dict] = {}
    if df.empty:
        return result
    for _, row in df.iterrows():
        key = (str(row["period_key"]), str(row["source_month"]), str(row["platform"]).upper())
        result[key] = {"gmv": float(row.get("gmv") or 0), "row_count": int(row.get("row_count") or 0)}
    return result


@tool
def query_market_trend(period: str, segment: str = "PURE MASS", platform: str = "TTL", view: str = "summary") -> dict:
    """按月表优先、日表补边界的规则查询Total Beauty大盘及同比。"""
    try:
        parsed = parse_ec_period(period, 2026)
        segment, platform = validate_scope(segment, platform)
        platforms = expected_platforms(platform)
        params = {
            "segment": segment, "current_start": parsed["current_start"], "current_end": parsed["current_end"],
            "prior_start": parsed["prior_start"], "prior_end": parsed["prior_end"],
        }
        platform_sql = ", ".join(f"'{value}'" for value in platforms)
        all_slices = (
            month_slices(parsed["current_start"], parsed["current_end"])
            + month_slices(parsed["prior_start"], parsed["prior_end"])
        )
        if any(item["full_month"] for item in all_slices):
            monthly_date = monthly_business_date_sql("bus_date")
            monthly = fetch_df(f"""
                SELECT CASE WHEN {monthly_date} BETWEEN :current_start AND :current_end THEN 'current' ELSE 'prior' END period_key,
                       DATE_FORMAT({monthly_date}, '%Y-%m') source_month, UPPER(TRIM(platform)) platform,
                       COUNT(*) row_count, COALESCE(SUM(gmv), 0) gmv
                FROM three_platforms_segmented_markets_monthly
                WHERE UPPER(TRIM(global_segment)) = :segment AND category_EN = 'Total Beauty'
                  AND UPPER(TRIM(platform)) IN ({platform_sql})
                  AND ({monthly_date} BETWEEN :current_start AND :current_end
                       OR {monthly_date} BETWEEN :prior_start AND :prior_end)
                GROUP BY period_key, source_month, UPPER(TRIM(platform))
            """, params)
        else:
            monthly = pd.DataFrame()
        daily = fetch_df(f"""
            SELECT CASE WHEN bus_date BETWEEN :current_start AND :current_end THEN 'current' ELSE 'prior' END period_key,
                   DATE_FORMAT(bus_date, '%Y-%m') source_month, UPPER(TRIM(platform)) platform,
                   COUNT(*) row_count, COALESCE(SUM(gmv), 0) gmv
            FROM three_platforms_segmented_markets_daily
            WHERE UPPER(TRIM(global_segment)) = :segment
              AND UPPER(TRIM(platform)) IN ({platform_sql})
              AND (bus_date BETWEEN :current_start AND :current_end OR bus_date BETWEEN :prior_start AND :prior_end)
            GROUP BY period_key, source_month, UPPER(TRIM(platform))
        """, params)
        monthly_lookup, daily_lookup = _lookup(monthly), _lookup(daily)
        selected: list[dict] = []
        missing: list[dict] = []
        for period_key, start, end in (
            ("current", parsed["current_start"], parsed["current_end"]),
            ("prior", parsed["prior_start"], parsed["prior_end"]),
        ):
            for slice_ in month_slices(start, end):
                monthly_complete = slice_["full_month"] and all(
                    monthly_lookup.get((period_key, slice_["month"], p), {}).get("row_count", 0) > 0 for p in platforms
                )
                source = "monthly" if monthly_complete else "daily"
                lookup = monthly_lookup if monthly_complete else daily_lookup
                for p in platforms:
                    value = lookup.get((period_key, slice_["month"], p), {"gmv": 0.0, "row_count": 0})
                    row = {"period_key": period_key, "month": slice_["month"], "range_start": slice_["start"],
                           "range_end": slice_["end"], "platform": p, "source": source, **value}
                    selected.append(row)
                    if value["row_count"] <= 0:
                        missing.append({"period": period_key, "month": slice_["month"], "platform": p, "source": source})
        totals = {key: {p: {"gmv": 0.0, "row_count": 0} for p in platforms} for key in ("current", "prior")}
        for row in selected:
            target = totals[row["period_key"]][row["platform"]]
            target["gmv"] += row["gmv"]
            target["row_count"] += row["row_count"]
        ttl = {
            key: {"gmv": sum(v["gmv"] for v in totals[key].values()),
                  "row_count": sum(v["row_count"] for v in totals[key].values())}
            for key in ("current", "prior")
        }
        rows = []
        labels = ("TTL",) + platforms if platform == "TTL" else platforms
        for label in labels:
            cur = ttl["current"] if label == "TTL" else totals["current"][label]
            pri = ttl["prior"] if label == "TTL" else totals["prior"][label]
            label_platforms = platforms if label == "TTL" else (label,)
            current_incomplete = any(m["period"] == "current" and m["platform"] in label_platforms for m in missing)
            prior_incomplete = any(m["period"] == "prior" and m["platform"] in label_platforms for m in missing)
            status = "missing_current" if current_incomplete else "missing_prior" if prior_incomplete else comparison_status(cur["row_count"], pri["row_count"], pri["gmv"])
            current_ttl, prior_ttl = ttl["current"]["gmv"], ttl["prior"]["gmv"]
            current_wgt = (
                cur["gmv"] / current_ttl if platform == "TTL" and label != "TTL" and current_ttl
                else 1.0 if label == "TTL" else None
            )
            prior_wgt = (
                pri["gmv"] / prior_ttl if platform == "TTL" and label != "TTL" and prior_ttl
                else 1.0 if label == "TTL" else None
            )
            rows.append({"platform": label, "gmv_actual": cur["gmv"], "gmv_prior": pri["gmv"],
                         "evol": evol(cur["gmv"], pri["gmv"]) if status == "ok" else None,
                         "gmv_growth": cur["gmv"] - pri["gmv"] if status == "ok" else None,
                         "wgt": current_wgt, "wgt_change": current_wgt - prior_wgt if current_wgt is not None and prior_wgt is not None else None,
                         "comparison_status": status})
        monthly_rows = []
        current_months = sorted({r["month"] for r in selected if r["period_key"] == "current"})
        for month in current_months:
            prior_month = f"{int(month[:4]) - 1:04d}{month[4:]}"
            for label in labels:
                label_platforms = platforms if label == "TTL" else (label,)
                current_parts = [r for r in selected if r["period_key"] == "current" and r["month"] == month and r["platform"] in label_platforms]
                prior_parts = [r for r in selected if r["period_key"] == "prior" and r["month"] == prior_month and r["platform"] in label_platforms]
                cur_value, pri_value = sum(r["gmv"] for r in current_parts), sum(r["gmv"] for r in prior_parts)
                complete_current = len(current_parts) == len(label_platforms) and all(r["row_count"] > 0 for r in current_parts)
                complete_prior = len(prior_parts) == len(label_platforms) and all(r["row_count"] > 0 for r in prior_parts)
                status = "missing_current" if not complete_current else "missing_prior" if not complete_prior else "base_zero" if pri_value == 0 else "ok"
                monthly_rows.append({"month": month, "platform": label, "gmv_actual": cur_value, "gmv_prior": pri_value,
                                     "evol": evol(cur_value, pri_value) if status == "ok" else None,
                                     "gmv_growth": cur_value - pri_value if status == "ok" else None,
                                     "comparison_status": status})
        return {"query_meta": {"tool": "query_market_trend", "segment": segment, "category": "Total Beauty", "platform": platform, "view": view,
                               "monthly_category_rule": "category_EN=Total Beauty", "daily_category_rule": "all categories within segment",
                               "current_period": [parsed["current_start"], parsed["current_end"]], "prior_period": [parsed["prior_start"], parsed["prior_end"]]},
                "totals": ttl, "rows": rows, "monthly_rows": monthly_rows, "coverage": selected, "missing": missing,
                "evidence": [{"metric": r["platform"], "value": r["gmv_actual"]} for r in rows]}
    except Exception as exc:
        return {"error": "execution_error", "message": str(exc)}
