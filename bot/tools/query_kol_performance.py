from __future__ import annotations

import pandas as pd

from bot.db.connection import fetch_df
from bot.tools.common import tool
from bot.utils import clean_label, safe_div, safe_evol


def _aggregate_dimension(df: pd.DataFrame, dimension: str) -> list[dict]:
    current_df = df[df["year"] == 2026]
    prior_df = df[df["year"] == 2025]
    current_total = float(current_df["cost"].sum())
    prior_total = float(prior_df["cost"].sum())
    current_total_rows = int(current_df["row_count"].sum())
    prior_total_rows = int(prior_df["row_count"].sum())
    if current_total_rows == 0:
        weight_status = "missing_current"
    elif prior_total_rows == 0:
        weight_status = "missing_prior"
    elif prior_total == 0:
        weight_status = "base_zero"
    else:
        weight_status = "ok"
    labels = sorted(
        set(current_df[dimension].dropna().astype(str))
        | set(prior_df[dimension].dropna().astype(str))
    )
    rows = []
    for label in labels:
        current_rows = current_df[current_df[dimension].astype(str) == label]
        prior_rows = prior_df[prior_df[dimension].astype(str) == label]
        current_cost = float(current_rows["cost"].sum())
        prior_cost = float(prior_rows["cost"].sum())
        current_engage = float(current_rows["engage"].sum())
        prior_engage = float(prior_rows["engage"].sum())
        current_count = int(current_rows["row_count"].sum())
        prior_count = int(prior_rows["row_count"].sum())
        if current_total_rows == 0:
            status = "missing_current"
        elif prior_total_rows == 0:
            status = "missing_prior"
        elif prior_cost == 0:
            status = "base_zero"
        else:
            status = "ok"
        if current_total_rows == 0:
            engage_status = "missing_current"
        elif prior_total_rows == 0:
            engage_status = "missing_prior"
        elif prior_engage == 0:
            engage_status = "base_zero"
        else:
            engage_status = "ok"
        weight = safe_div(current_cost, current_total)
        prior_weight = safe_div(prior_cost, prior_total)
        rows.append({
            "name": clean_label(label, "未分类"),
            "cost": round(current_cost, 2),
            "cost_prior": round(prior_cost, 2),
            "cost_evol": safe_evol(current_cost, prior_cost) if status == "ok" else None,
            "weight": weight,
            "weight_prior": prior_weight,
            "weight_change": (
                round(weight - prior_weight, 6)
                if (
                    weight_status == "ok"
                    and weight is not None
                    and prior_weight is not None
                )
                else None
            ),
            "weight_comparison_status": weight_status,
            "engage": round(current_engage),
            "engage_prior": round(prior_engage),
            "engage_evol": safe_evol(current_engage, prior_engage) if engage_status == "ok" else None,
            "cpe": round(current_cost / current_engage, 4) if current_engage else None,
            "comparison_status": status,
            "engage_comparison_status": engage_status,
            "current_rows": current_count,
            "prior_rows": prior_count,
        })
    if dimension == "tier":
        tier_order = {
            "T1": 0, "T2": 1, "T3": 2, "T4": 3, "T5": 4, "KOC": 5,
        }
        return sorted(
            rows,
            key=lambda row: (
                tier_order.get(str(row["name"]).upper(), 99),
                -float(row["cost"]),
            ),
        )
    return sorted(rows, key=lambda row: row["cost"], reverse=True)


@tool
def query_kol_performance(
    brand: str,
    platform: str,
    focus_start: str,
    focus_end: str,
    prior_start: str,
    prior_end: str,
    top_n: int = 10,
) -> dict:
    """查询RED或Douyin的Tier、KOL Type汇总及按Engage排序的Top KOL。"""
    try:
        platform = str(platform or "").strip().lower()
        if platform not in {"red", "douyin"}:
            return {"error": "invalid_platform", "message": "platform只支持red或douyin。"}
        if not 1 <= int(top_n) <= 50:
            return {"error": "invalid_top_n", "message": "top_n必须在1到50之间。"}

        summary = fetch_df(
            """
            SELECT
                year,
                period_month,
                tier,
                kol_type,
                SUM(big_v_cost) AS cost,
                SUM(COALESCE(ttl_engagement, 0)) AS engage,
                COUNT(*) AS row_count
            FROM ai_bot_media_ksi_performance
            WHERE brand = :brand
              AND LOWER(platform) = :platform
              AND (
                period_month BETWEEN :focus_start AND :focus_end
                OR period_month BETWEEN :prior_start AND :prior_end
              )
            GROUP BY year, period_month, tier, kol_type
            """,
            {
                "brand": brand,
                "platform": platform,
                "focus_start": focus_start,
                "focus_end": focus_end,
                "prior_start": prior_start,
                "prior_end": prior_end,
            },
        )
        if summary.empty:
            brand_coverage = fetch_df(
                """
                SELECT
                    year,
                    COUNT(*) AS row_count
                FROM ai_bot_media_ksi_performance
                WHERE brand = :brand
                  AND (
                    period_month BETWEEN :focus_start AND :focus_end
                    OR period_month BETWEEN :prior_start AND :prior_end
                  )
                GROUP BY year
                """,
                {
                    "brand": brand,
                    "focus_start": focus_start,
                    "focus_end": focus_end,
                    "prior_start": prior_start,
                    "prior_end": prior_end,
                },
            )
            if brand_coverage.empty:
                error = "period_data_missing"
                message = f"KSI中品牌“{brand}”在当前及同期均无数据。"
            else:
                error = "no_platform_investment"
                message = f"KSI中品牌“{brand}”在指定期间没有{platform}投放。"
            return {
                "error": error,
                "message": message,
                "brand": brand,
                "matched_brand": brand,
                "platform": platform,
            }
        summary["year"] = pd.to_numeric(summary["year"], errors="coerce")
        summary["period_month"] = pd.to_datetime(summary["period_month"])
        summary["cost"] = pd.to_numeric(summary["cost"], errors="coerce").fillna(0.0)
        summary["engage"] = pd.to_numeric(summary["engage"], errors="coerce").fillna(0.0)
        summary["row_count"] = pd.to_numeric(summary["row_count"], errors="coerce").fillna(0).astype(int)
        summary["tier"] = summary["tier"].fillna("未分类").astype(str)
        summary["kol_type"] = summary["kol_type"].fillna("未分类").astype(str)
        current_summary = summary[summary["year"] == 2026]
        prior_summary = summary[summary["year"] == 2025]
        current_rows = int(current_summary["row_count"].sum())
        prior_rows = int(prior_summary["row_count"].sum())
        current_cost = float(current_summary["cost"].sum())
        prior_cost = float(prior_summary["cost"].sum())
        current_engage = float(current_summary["engage"].sum())
        prior_engage = float(prior_summary["engage"].sum())
        if current_rows == 0:
            cost_status = engage_status = "missing_current"
        elif prior_rows == 0:
            cost_status = engage_status = "missing_prior"
        else:
            cost_status = "base_zero" if prior_cost == 0 else "ok"
            engage_status = "base_zero" if prior_engage == 0 else "ok"

        top = fetch_df(
            """
            SELECT
                COALESCE(
                    MAX(NULLIF(TRIM(nickname), '')),
                    MAX(NULLIF(TRIM(kol_id_front), '')),
                    '未知KOL'
                ) AS nickname,
                MAX(tier) AS tier,
                MAX(kol_type) AS kol_type,
                SUM(big_v_cost) AS cost,
                SUM(COALESCE(ttl_engagement, 0)) AS engage
            FROM ai_bot_media_ksi_performance
            WHERE brand = :brand
              AND LOWER(platform) = :platform
              AND period_month BETWEEN :focus_start AND :focus_end
            GROUP BY COALESCE(
                NULLIF(TRIM(kol_id_front), ''),
                CONCAT('nickname:', COALESCE(NULLIF(TRIM(nickname), ''), '未知KOL'))
            )
            ORDER BY engage DESC, cost DESC
            LIMIT :top_n
            """,
            {
                "brand": brand,
                "platform": platform,
                "focus_start": focus_start,
                "focus_end": focus_end,
                "top_n": int(top_n),
            },
        )
        top_rows = []
        for rank, (_, row) in enumerate(top.iterrows(), 1):
            cost = float(row["cost"] or 0)
            engage = float(row["engage"] or 0)
            top_rows.append({
                "rank": rank,
                "nickname": clean_label(row["nickname"], "未知KOL"),
                "tier": clean_label(row["tier"], "未分类"),
                "kol_type": clean_label(row["kol_type"], "未分类"),
                "cost": round(cost, 2),
                "engage": round(engage),
                "cpe": round(cost / engage, 4) if engage else None,
            })

        return {
            "brand": brand,
            "matched_brand": brand,
            "platform": platform,
            "date_range": {
                "current": [focus_start, focus_end],
                "prior": [prior_start, prior_end],
            },
            "totals": {
                "cost_actual": round(current_cost, 2),
                "cost_prior": round(prior_cost, 2),
                "cost_evol": (
                    safe_evol(current_cost, prior_cost) if cost_status == "ok" else None
                ),
                "cost_comparison_status": cost_status,
                "engage_actual": round(current_engage),
                "engage_prior": round(prior_engage),
                "engage_evol": (
                    safe_evol(current_engage, prior_engage)
                    if engage_status == "ok" else None
                ),
                "engage_comparison_status": engage_status,
                "cpe": (
                    round(current_cost / current_engage, 4)
                    if current_engage else None
                ),
            },
            "by_tier": _aggregate_dimension(summary, "tier"),
            "by_kol_type": _aggregate_dimension(summary, "kol_type"),
            "top_kol": top_rows,
            "coverage": {
                "current_months": sorted(
                    summary.loc[summary["year"] == 2026, "period_month"].dt.strftime("%Y-%m").unique().tolist()
                ),
                "prior_months": sorted(
                    summary.loc[summary["year"] == 2025, "period_month"].dt.strftime("%Y-%m").unique().tolist()
                ),
                "current_rows": current_rows,
                "prior_rows": prior_rows,
            },
        }
    except Exception as exc:
        return {"error": "execution_error", "message": str(exc)}
