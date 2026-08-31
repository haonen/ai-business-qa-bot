from __future__ import annotations

from typing import Iterable

import pandas as pd

from bot.db.connection import fetch_df, fetch_one
from bot.media_brand import normalize_brand, resolve_source_brand
from bot.platforms import platform_filter_sql
from bot.tools.common import tool
from bot.tools.market_common import month_slices
from bot.utils import parse_ec_period, safe_div, safe_evol


JD_DAILY_TABLE = "jd_store_ranking_selfrun_day_jiashicang"
MONTHLY_TABLE = "three_platform_store_rank_monthly"
DISPLAY_CATEGORY_LIMIT = 6


def _amount_sql(column: str) -> str:
    return (
        "COALESCE(CAST(REPLACE(NULLIF(TRIM(" + column + "), ''), ',', '') "
        "AS DECIMAL(24,4)), 0)"
    )


def _source_slices(start: str, end: str) -> list[dict]:
    """Use JD self-run daily rows so total GMV reconciles to level-3 categories."""
    return [
        {
            **item,
            "source": "daily",
            "table": JD_DAILY_TABLE,
        }
        for item in month_slices(start, end)
    ]


def _brand_values(
    brand: str,
    brand_aliases: Iterable[str] | None,
) -> list[str]:
    values: list[str] = []
    for value in (brand, *(brand_aliases or ())):
        value = str(value or "").strip()
        if value and value not in values:
            values.append(value)
    try:
        resolved = resolve_source_brand(brand, "tmall", brand_aliases=list(values))
    except Exception:
        resolved = {}
    resolved_brand = str(resolved.get("brand") or "").strip()
    if resolved_brand and resolved_brand not in values:
        values.append(resolved_brand)
    return values


def _resolve_jd_brand(
    brand: str,
    brand_aliases: Iterable[str] | None = None,
) -> dict:
    values = _brand_values(brand, brand_aliases)
    if not values:
        return {"error": "missing_brand", "message": "请提供需要分析的品牌。"}
    params = {f"brand_{index}": value for index, value in enumerate(values)}
    placeholders = ", ".join(f":brand_{index}" for index in range(len(values)))
    daily = fetch_df(
        f"""
        SELECT DISTINCT TRIM(brand_name) AS source_brand
        FROM {JD_DAILY_TABLE}
        WHERE brand_name IN ({placeholders})
        """,
        params,
    )
    monthly = fetch_df(
        f"""
        SELECT DISTINCT TRIM(brand_name) AS source_brand
        FROM {MONTHLY_TABLE}
        WHERE brand_name IN ({placeholders})
          AND {platform_filter_sql(MONTHLY_TABLE, 'JD')}
        """,
        params,
    )
    candidates = []
    for frame in (daily, monthly):
        for value in frame.get("source_brand", []):
            value = str(value or "").strip()
            if value and value not in candidates:
                candidates.append(value)
    if not candidates:
        return {
            "error": "no_data",
            "message": f"京东数据中没有找到品牌“{brand}”。",
        }
    if len(candidates) == 1:
        return {"brand": candidates[0]}
    supplied_normalized = {normalize_brand(value) for value in values}
    exact = [value for value in candidates if normalize_brand(value) in supplied_normalized]
    if len(exact) == 1:
        return {"brand": exact[0]}
    return {
        "error": "ambiguous_brand",
        "message": f"品牌“{brand}”对应多个京东品牌值，请确认具体品牌。",
        "candidates": candidates[:5],
    }


def _fetch_category_slice(
    *,
    table: str,
    brand: str,
    period_key: str,
    start: str,
    end: str,
    monthly: bool,
) -> pd.DataFrame:
    platform_clause = (
        "AND " + platform_filter_sql(MONTHLY_TABLE, "JD") if monthly else ""
    )
    return fetch_df(
        f"""
        SELECT
          :period_key AS period_key,
          COALESCE(NULLIF(TRIM(category_level_3), ''), '未分类') AS category_level_3,
          SUM({_amount_sql('gmv')}) AS gmv,
          COUNT(*) AS row_count
        FROM {table}
        WHERE TRIM(brand_name) = :brand
          {platform_clause}
          AND CAST(bus_date AS DATE) BETWEEN :slice_start AND :slice_end
        GROUP BY COALESCE(NULLIF(TRIM(category_level_3), ''), '未分类')
        """,
        {
            "period_key": period_key,
            "brand": brand,
            "slice_start": start,
            "slice_end": end,
        },
    )


def _query_category_frames(
    brand: str,
    period_meta: dict,
) -> tuple[pd.DataFrame, list[dict], list[dict]]:
    business_date = "CAST(bus_date AS DATE)"
    frame = fetch_df(
        f"""
        SELECT
          CASE
            WHEN {business_date} BETWEEN :current_start AND :current_end THEN 'current'
            ELSE 'prior'
          END AS period_key,
          DATE_FORMAT({business_date}, '%Y-%m') AS source_month,
          COALESCE(NULLIF(TRIM(category_level_3), ''), '未分类') AS category_level_3,
          SUM({_amount_sql('gmv')}) AS gmv,
          COUNT(*) AS row_count
        FROM {JD_DAILY_TABLE}
        WHERE TRIM(brand_name) = :brand
          AND (
            {business_date} BETWEEN :current_start AND :current_end
            OR {business_date} BETWEEN :prior_start AND :prior_end
          )
        GROUP BY period_key, source_month,
                 COALESCE(NULLIF(TRIM(category_level_3), ''), '未分类')
        """,
        {
            "brand": brand,
            **{key: period_meta[key] for key in (
                "current_start", "current_end", "prior_start", "prior_end",
            )},
        },
    )
    sources = []
    missing = []
    present = set()
    if not frame.empty:
        present = {
            (str(row["period_key"]), str(row["source_month"]))
            for _, row in frame[["period_key", "source_month"]].drop_duplicates().iterrows()
        }
    for period_key, start_key, end_key in (
        ("current", "current_start", "current_end"),
        ("prior", "prior_start", "prior_end"),
    ):
        for item in _source_slices(period_meta[start_key], period_meta[end_key]):
            source_row = {
                "period": period_key, "month": item["month"],
                "source": "daily", "table": JD_DAILY_TABLE,
                "start": item["start"], "end": item["end"],
            }
            sources.append(source_row)
            if (period_key, item["month"]) not in present:
                missing.append(source_row)
    return frame, sources, missing


def _paired_category_rows(frame: pd.DataFrame) -> tuple[dict, list[dict]]:
    totals = {
        period: float(frame.loc[frame["period_key"] == period, "gmv"].sum())
        for period in ("current", "prior")
    }
    grouped = (
        frame.groupby(["period_key", "category_level_3"], dropna=False)["gmv"]
        .sum()
        .reset_index()
    )
    current = grouped[grouped["period_key"] == "current"].drop(columns="period_key").rename(
        columns={"gmv": "gmv_current"}
    )
    prior = grouped[grouped["period_key"] == "prior"].drop(columns="period_key").rename(
        columns={"gmv": "gmv_prior"}
    )
    merged = current.merge(prior, on="category_level_3", how="outer").fillna(
        {"gmv_current": 0, "gmv_prior": 0}
    )
    rows: list[dict] = []
    for _, row in merged.iterrows():
        current_gmv = float(row["gmv_current"] or 0)
        prior_gmv = float(row["gmv_prior"] or 0)
        weight = safe_div(current_gmv, totals["current"])
        prior_weight = safe_div(prior_gmv, totals["prior"])
        rows.append({
            "category": str(row["category_level_3"] or "未分类"),
            "gmv_current": current_gmv,
            "gmv_prior": prior_gmv,
            "evol": safe_evol(current_gmv, prior_gmv),
            "weight": weight,
            "weight_prior": prior_weight,
            "weight_change": (
                weight - prior_weight
                if weight is not None and prior_weight is not None
                else None
            ),
        })
    rows.sort(key=lambda value: value["gmv_current"], reverse=True)
    total = {
        "gmv_current": totals["current"],
        "gmv_prior": totals["prior"],
        "evol": safe_evol(totals["current"], totals["prior"]),
    }
    return total, rows


def _combine_other(rows: list[dict], total: dict) -> list[dict]:
    if len(rows) <= DISPLAY_CATEGORY_LIMIT:
        return list(rows)
    displayed = list(rows[:DISPLAY_CATEGORY_LIMIT])
    remainder = rows[DISPLAY_CATEGORY_LIMIT:]
    current = sum(float(row["gmv_current"] or 0) for row in remainder)
    prior = sum(float(row["gmv_prior"] or 0) for row in remainder)
    weight = safe_div(current, total["gmv_current"])
    prior_weight = safe_div(prior, total["gmv_prior"])
    label = "其他" if all(row.get("category") != "其他" for row in displayed) else "其余品类"
    displayed.append({
        "category": label,
        "gmv_current": current,
        "gmv_prior": prior,
        "evol": safe_evol(current, prior),
        "weight": weight,
        "weight_prior": prior_weight,
        "weight_change": (
            weight - prior_weight
            if weight is not None and prior_weight is not None
            else None
        ),
    })
    return displayed


@tool
def query_jd_business(
    brand: str,
    period: str,
    brand_aliases: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """Return brand GMV and level-3 category metrics for a JD self-run report."""
    try:
        resolved = _resolve_jd_brand(brand, brand_aliases)
        if resolved.get("error"):
            return resolved
        source_brand = str(resolved["brand"])
        daily_max = fetch_one(
            f"SELECT MAX(CAST(bus_date AS DATE)) AS max_date FROM {JD_DAILY_TABLE} "
            "WHERE TRIM(brand_name) = :brand",
            {"brand": source_brand},
        ).get("max_date")
        monthly_max = fetch_one(
            f"SELECT MAX(CAST(bus_date AS DATE)) AS max_date FROM {MONTHLY_TABLE} "
            "WHERE TRIM(brand_name) = :brand AND "
            + platform_filter_sql(MONTHLY_TABLE, "JD"),
            {"brand": source_brand},
        ).get("max_date")
        latest_values = [str(value)[:10] for value in (daily_max, monthly_max) if value]
        if not latest_values:
            return {"error": "no_data", "message": f"京东数据中没有找到品牌“{brand}”。"}
        source_max_date = max(latest_values)
        period_meta = parse_ec_period(period, int(source_max_date[:4]))
        frame, sources, missing = _query_category_frames(source_brand, period_meta)
        if missing:
            detail = "、".join(
                f"{row['period']} {row['month']}({row['source']})" for row in missing
            )
            return {"error": "incomplete_coverage", "message": f"京东品牌/品类数据存在缺口：{detail}。"}
        if frame.empty:
            return {"error": "no_data", "message": f"品牌“{source_brand}”在指定期间没有京东数据。"}
        total, all_categories = _paired_category_rows(frame)
        displayed = _combine_other(all_categories, total)
        display_current = sum(row["gmv_current"] for row in displayed)
        display_prior = sum(row["gmv_prior"] for row in displayed)
        current_tolerance = max(0.01, abs(total["gmv_current"]) * 1e-10)
        prior_tolerance = max(0.01, abs(total["gmv_prior"]) * 1e-10)
        if (
            abs(display_current - total["gmv_current"]) > current_tolerance
            or abs(display_prior - total["gmv_prior"]) > prior_tolerance
        ):
            return {"error": "reconciliation_error", "message": "京东品类GMV与品牌总GMV未对齐。"}
        return {
            "brand": brand,
            "source_brand": source_brand,
            "period": period,
            "period_meta": {**period_meta, "source_max_date": source_max_date},
            "brand_result": total,
            "categories": displayed,
            "all_categories": all_categories,
            "selected_category": all_categories[0]["category"] if all_categories else "",
            "sources": sources,
            "reconciliation": {
                "brand_current": total["gmv_current"],
                "displayed_categories_current": display_current,
                "brand_prior": total["gmv_prior"],
                "displayed_categories_prior": display_prior,
            },
        }
    except Exception as exc:
        return {"error": "execution_error", "message": str(exc)}
