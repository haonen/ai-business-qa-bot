from __future__ import annotations

import pandas as pd

from bot.db.connection import fetch_df
from bot.tools.common import tool
from bot.tools.market_common import (
    TTL_CATEGORIES,
    expected_platforms,
    month_slices,
    monthly_business_date_sql,
    validate_scope,
)
from bot.utils import parse_ec_period


def _rows(df: pd.DataFrame) -> dict[tuple[str, str, str, str], dict]:
    result = {}
    if df.empty:
        return result
    for _, row in df.iterrows():
        key = (str(row["period_key"]), str(row["source_month"]), str(row["platform"]).upper(), str(row["brand_name"]).strip())
        result[key] = {"gmv": float(row.get("gmv") or 0), "row_count": int(row.get("row_count") or 0)}
    return result


@tool
def query_market_top_brands(period: str, segment: str = "PURE MASS", platform: str = "TM", ranking_metric: str = "gmv_growth", limit: int = 5) -> dict:
    """查询大盘中可比品牌Top 5；三平台非完整月份不以天猫日表代替。"""
    try:
        parsed = parse_ec_period(period, 2026)
        segment, platform = validate_scope(segment, platform)
        slices = month_slices(parsed["current_start"], parsed["current_end"])
        prior_slices = month_slices(parsed["prior_start"], parsed["prior_end"])
        if platform != "TM" and any(not item["full_month"] for item in slices + prior_slices):
            return {"error": "unsupported_partial_platform_coverage",
                    "message": "三平台及抖音/京东品牌榜目前只有月表；非完整月无法与天猫日表混算。请改看完整月份，或切换为天猫Top 5。"}
        platforms = expected_platforms(platform)
        platform_sql = ", ".join(f"'{p}'" for p in platforms)
        categories_sql = ", ".join(f"'{c}'" for c in TTL_CATEGORIES)
        # 品牌榜数据中 Pure Mass 以 SELECTIVITY IS NULL 表示；
        # Selective/Professional 使用字段中的显式标签。
        monthly_segment_clause = (
            "AND SELECTIVITY IS NULL" if segment == "PURE MASS"
            else "AND UPPER(TRIM(SELECTIVITY)) = :segment"
        )
        params = {"segment": segment, "current_start": parsed["current_start"], "current_end": parsed["current_end"],
                  "prior_start": parsed["prior_start"], "prior_end": parsed["prior_end"],
                  "current_start_slash": parsed["current_start"].replace("-", "/"), "current_end_slash": parsed["current_end"].replace("-", "/"),
                  "prior_start_slash": parsed["prior_start"].replace("-", "/"), "prior_end_slash": parsed["prior_end"].replace("-", "/")}
        monthly_date = monthly_business_date_sql("bus_date")
        monthly = fetch_df(f"""
            SELECT CASE WHEN {monthly_date} BETWEEN :current_start AND :current_end THEN 'current' ELSE 'prior' END period_key,
                   DATE_FORMAT({monthly_date}, '%Y-%m') source_month, UPPER(TRIM(platform)) platform,
                   TRIM(brand_name) brand_name, COUNT(*) row_count, COALESCE(SUM(gmv), 0) gmv
            FROM three_platform_store_rank_monthly
            WHERE UPPER(TRIM(platform)) IN ({platform_sql})
              {monthly_segment_clause}
              AND category_EN_level_1 IN ({categories_sql}) AND brand_name IS NOT NULL AND TRIM(brand_name) <> ''
              AND gmv >= 0
              AND ({monthly_date} BETWEEN :current_start AND :current_end
                   OR {monthly_date} BETWEEN :prior_start AND :prior_end)
            GROUP BY period_key, source_month, UPPER(TRIM(platform)), TRIM(brand_name)
        """, params)
        daily = pd.DataFrame()
        if platform == "TM":
            if segment == "PURE MASS":
                daily_from = "FROM tmall_store_ranking_day_jiashicang d"
                daily_segment_clause = "AND d.SELECTIVITY IS NULL"
            else:
                daily_from = f"""
                FROM tmall_store_ranking_day_jiashicang d
                LEFT JOIN (
                    SELECT DISTINCT TRIM(brand_name) AS brand_name
                    FROM three_platform_store_rank_monthly
                    WHERE UPPER(TRIM(SELECTIVITY)) = :segment
                      AND UPPER(TRIM(platform)) IN ('TM', 'TMALL')
                      AND category_EN_level_1 IN ({categories_sql})
                      AND brand_name IS NOT NULL AND TRIM(brand_name) <> ''
                ) segment_brand
                  ON TRIM(segment_brand.brand_name) = TRIM(d.brand_name)
                """
                daily_segment_clause = """
                  AND (
                    UPPER(TRIM(d.SELECTIVITY)) = :segment
                    OR (
                        (d.SELECTIVITY IS NULL OR TRIM(d.SELECTIVITY) = '')
                        AND segment_brand.brand_name IS NOT NULL
                    )
                  )
                """
            daily = fetch_df(f"""
                SELECT CASE WHEN d.bus_date BETWEEN :current_start_slash AND :current_end_slash THEN 'current' ELSE 'prior' END period_key,
                       DATE_FORMAT(STR_TO_DATE(d.bus_date, '%Y/%m/%d'), '%Y-%m') source_month, 'TM' platform,
                       TRIM(d.brand_name) brand_name, COUNT(*) row_count,
                       COALESCE(SUM(CAST(REPLACE(NULLIF(TRIM(d.gmv), ''), ',', '') AS DECIMAL(24,4))), 0) gmv
                {daily_from}
                WHERE d.category_EN_level_1 IN ({categories_sql})
                  {daily_segment_clause}
                  AND d.brand_name IS NOT NULL AND TRIM(d.brand_name) <> ''
                  AND CAST(REPLACE(NULLIF(TRIM(d.gmv), ''), ',', '') AS DECIMAL(24,4)) >= 0
                  AND (d.bus_date BETWEEN :current_start_slash AND :current_end_slash OR d.bus_date BETWEEN :prior_start_slash AND :prior_end_slash)
                GROUP BY period_key, source_month, TRIM(d.brand_name)
            """, params)
        monthly_lookup, daily_lookup = _rows(monthly), _rows(daily)
        chosen: list[dict] = []
        missing: list[dict] = []
        for period_key, ranges in (("current", slices), ("prior", prior_slices)):
            for item in ranges:
                available_platforms = {key[2] for key in monthly_lookup if key[0] == period_key and key[1] == item["month"]}
                monthly_complete = item["full_month"] and set(platforms).issubset(available_platforms)
                source = "monthly" if monthly_complete else "daily"
                lookup = monthly_lookup if monthly_complete else daily_lookup
                matching = [(key, value) for key, value in lookup.items() if key[0] == period_key and key[1] == item["month"] and key[2] in platforms]
                if not matching:
                    missing.append({"period": period_key, "month": item["month"], "source": source})
                for key, value in matching:
                    if value["gmv"] < 0:
                        continue
                    chosen.append({"period_key": period_key, "month": item["month"], "platform": key[2],
                                   "brand": key[3], "source": source, **value})
        if missing:
            labels = {
                "current": "本期",
                "prior": "同期",
            }
            detail = "、".join(
                f"{labels.get(item['period'], item['period'])}{item['month']}({item['source']})"
                for item in missing
            )
            return {
                "error": "incomplete_coverage",
                "message": f"品牌榜在请求期间存在数据缺口：{detail}。未生成可能失真的Top 5。",
                "missing": missing,
            }
        totals: dict[str, dict[str, float]] = {"current": {}, "prior": {}}
        for row in chosen:
            brand_values = totals[row["period_key"]]
            brand_values[row["brand"]] = brand_values.get(row["brand"], 0.0) + row["gmv"]
        comparable, new_brands = [], []
        for brand, current in totals["current"].items():
            prior = totals["prior"].get(brand)
            if prior is None or prior <= 0:
                new_brands.append({"brand": brand, "gmv_actual": current})
                continue
            growth = current - prior
            comparable.append({"brand": brand, "gmv_actual": current, "gmv_prior": prior,
                               "evol": current / prior - 1, "gmv_growth": growth})
        metric = "evol" if ranking_metric == "evol" else "gmv_growth"
        comparable = [row for row in comparable if row[metric] > 0]
        comparable.sort(key=lambda row: row[metric], reverse=True)
        result_rows = [{"rank": rank, **row} for rank, row in enumerate(comparable[:max(1, min(int(limit), 20))], 1)]
        return {"query_meta": {"tool": "query_market_top_brands", "segment": segment, "category": "Total Beauty", "platform": platform,
                               "ranking_metric": metric, "current_period": [parsed["current_start"], parsed["current_end"]],
                               "prior_period": [parsed["prior_start"], parsed["prior_end"]]},
                "rows": result_rows, "coverage": chosen, "missing": [], "new_brands": sorted(new_brands, key=lambda x: x["gmv_actual"], reverse=True)[:5],
                "evidence": [{"rank": row["rank"], "brand": row["brand"], "value": row[metric]} for row in result_rows]}
    except Exception as exc:
        return {"error": "execution_error", "message": str(exc)}
