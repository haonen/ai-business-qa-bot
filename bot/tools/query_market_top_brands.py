from __future__ import annotations

import logging

import pandas as pd

from bot.db.connection import fetch_df
from bot.newsletter.data import TABLES as DAILY_STORE_TABLES
from bot.platforms import normalized_platform_sql, platform_filter_sql, platform_values
from bot.store_metric_scope import weight_sql
from bot.tools.common import tool
from bot.tools.market_common import (
    expected_platforms,
    month_slices,
    normalize_market_category,
    store_rank_business_category_sql,
    store_rank_monthly_date_sql,
    validate_scope,
)
from bot.utils import parse_ec_period

log = logging.getLogger(__name__)


def _rows(df: pd.DataFrame) -> dict[tuple[str, str, str, str], dict]:
    result = {}
    if df.empty:
        return result
    for _, row in df.iterrows():
        key = (str(row["period_key"]), str(row["source_month"]), str(row["platform"]).upper(), str(row["brand_name"]).strip())
        result[key] = {"gmv": float(row.get("gmv") or 0), "row_count": int(row.get("row_count") or 0)}
    return result


@tool
def query_market_top_brands(period: str, segment: str = "PURE MASS", platform: str = "TM", ranking_metric: str = "gmv_actual", limit: int = 5, category: str = "TOTAL BEAUTY") -> dict:
    """查询大盘中可比品牌 Top N；整月优先月表，非整月使用各平台日表。"""
    try:
        parsed = parse_ec_period(period, 2026)
        segment, platform = validate_scope(segment, platform)
        category = normalize_market_category(category)
        slices = month_slices(parsed["current_start"], parsed["current_end"])
        prior_slices = month_slices(parsed["prior_start"], parsed["prior_end"])
        platforms = expected_platforms(platform)
        # The monthly fact table contains both TM and TMALL in historical
        # batches.  Treat them as one canonical TM platform so TTL coverage is
        # not incorrectly declared missing merely because a batch used the
        # longer platform label.
        source_platforms = tuple(dict.fromkeys(
            source for canonical in platforms
            for source in platform_values("three_platform_store_rank_monthly", canonical)
        ))
        platform_sql = ", ".join(f"'{p}'" for p in source_platforms)
        normalized_platform_expr = normalized_platform_sql(
            "three_platform_store_rank_monthly"
        )
        monthly_category_clause = store_rank_business_category_sql(category)
        # 品牌榜数据中 Pure Mass 以 SELECTIVITY 为 NULL 或空字符串表示；
        # Selective/Professional 使用字段中的显式标签。
        monthly_segment_clause = (
            "AND (SELECTIVITY IS NULL OR TRIM(SELECTIVITY) = '')" if segment == "PURE MASS"
            else "AND UPPER(TRIM(SELECTIVITY)) = :segment"
        )
        params = {
            "segment": segment,
            "current_start": parsed["current_start"], "current_end": parsed["current_end"],
            "prior_start": parsed["prior_start"], "prior_end": parsed["prior_end"],
        }
        monthly_date = store_rank_monthly_date_sql("bus_date")
        has_full_month = any(item["full_month"] for item in slices + prior_slices)
        monthly = fetch_df(f"""
            SELECT CASE WHEN {monthly_date} BETWEEN :current_start AND :current_end THEN 'current' ELSE 'prior' END period_key,
                   DATE_FORMAT({monthly_date}, '%Y-%m') source_month, {normalized_platform_expr} platform,
                   TRIM(brand_name) brand_name, COUNT(*) row_count, COALESCE(SUM(gmv), 0) gmv
            FROM (
                SELECT bus_date, category_EN_level_1,
                       category_EN_level_2, store_id, store_CN, brand_name,
                       SELECTIVITY, platform,
                       MAX(CAST(REPLACE(NULLIF(TRIM(gmv), ''), ',', '') AS DECIMAL(24,4))) AS gmv
                FROM three_platform_store_rank_monthly
                WHERE UPPER(TRIM(platform)) IN ({platform_sql})
                  {monthly_segment_clause}
                  AND {monthly_category_clause}
                  AND brand_name IS NOT NULL AND TRIM(brand_name) <> ''
                  AND (
                    {monthly_date} BETWEEN :current_start AND :current_end
                    OR {monthly_date} BETWEEN :prior_start AND :prior_end
                  )
                GROUP BY bus_date, category_EN_level_1,
                         category_EN_level_2, store_id, store_CN, brand_name,
                         SELECTIVITY, platform
            ) monthly_dedup
            WHERE UPPER(TRIM(platform)) IN ({platform_sql})
              {monthly_segment_clause}
              AND brand_name IS NOT NULL AND TRIM(brand_name) <> ''
              AND gmv >= 0
              AND ({monthly_date} BETWEEN :current_start AND :current_end
                   OR {monthly_date} BETWEEN :prior_start AND :prior_end)
            GROUP BY period_key, source_month, {normalized_platform_expr}, TRIM(brand_name)
        """, params) if has_full_month else pd.DataFrame()
        monthly_lookup = _rows(monthly)
        fallback_slices: list[tuple[str, dict]] = []
        for period_key, ranges in (("current", slices), ("prior", prior_slices)):
            for item in ranges:
                available_platforms = {
                    key[2] for key in monthly_lookup
                    if key[0] == period_key and key[1] == item["month"]
                }
                if not (item["full_month"] and set(platforms).issubset(available_platforms)):
                    fallback_slices.append((period_key, item))

        daily_frames = []
        if fallback_slices:
            weight_category = {
                "TOTAL BEAUTY": "TTL", "FEMALE SKINCARE": "SKIN",
                "MALE SKINCARE": "MEX", "MAKEUP": "MAKEUP", "HAIR": "HAIR",
            }[category]
            for daily_platform in platforms:
                table = DAILY_STORE_TABLES[daily_platform]
                # These source columns use ISO dates.  Keep the predicate on the
                # raw column so MySQL can use the date index; casting every row
                # made a short ranking query take tens of seconds.
                day_expr = "CAST(d.bus_date AS DATE)"
                category_weight = weight_sql(
                    weight_category, daily_platform,
                    "d.category_EN_level_1", "d.category_EN_level_2",
                )
                if segment == "PURE MASS":
                    daily_from = f"FROM `{table}` d"
                    daily_segment_clause = "AND (d.SELECTIVITY IS NULL OR TRIM(d.SELECTIVITY) = '')"
                else:
                    daily_from = f"""
                    FROM `{table}` d
                    LEFT JOIN (
                        SELECT DISTINCT TRIM(brand_name) AS brand_name
                        FROM three_platform_store_rank_monthly
                        WHERE UPPER(TRIM(SELECTIVITY)) = :segment
                          AND {platform_filter_sql('three_platform_store_rank_monthly', daily_platform)}
                          AND {monthly_category_clause}
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
                daily_params = dict(params)
                current_clauses: list[str] = []
                all_clauses: list[str] = []
                for index, (period_key, item) in enumerate(fallback_slices):
                    start_key = f"daily_{daily_platform.lower()}_{index}_start"
                    end_key = f"daily_{daily_platform.lower()}_{index}_end"
                    daily_params[start_key] = item["start"]
                    daily_params[end_key] = item["end"]
                    clause = f"d.bus_date BETWEEN :{start_key} AND :{end_key}"
                    all_clauses.append(clause)
                    if period_key == "current":
                        current_clauses.append(clause)
                current_case = " OR ".join(current_clauses) or "FALSE"
                fallback_where = " OR ".join(all_clauses)
                daily_frames.append(fetch_df(f"""
                    SELECT CASE WHEN ({current_case}) THEN 'current' ELSE 'prior' END period_key,
                           DATE_FORMAT({day_expr}, '%Y-%m') source_month,
                           '{daily_platform}' platform, TRIM(d.brand_name) brand_name,
                           COUNT(*) row_count,
                           COALESCE(SUM(
                               CAST(REPLACE(NULLIF(TRIM(d.gmv), ''), ',', '') AS DECIMAL(24,4))
                               * ({category_weight})
                           ), 0) gmv
                    {daily_from}
                    WHERE ({category_weight}) <> 0
                      {daily_segment_clause}
                      AND d.brand_name IS NOT NULL AND TRIM(d.brand_name) <> ''
                      AND CAST(REPLACE(NULLIF(TRIM(d.gmv), ''), ',', '') AS DECIMAL(24,4)) >= 0
                      AND ({fallback_where})
                    GROUP BY period_key, source_month, TRIM(d.brand_name)
                    HAVING gmv >= 0
                """, daily_params))
        daily = pd.concat(daily_frames, ignore_index=True) if daily_frames else pd.DataFrame()
        daily_lookup = _rows(daily)
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
                "message": (
                    f"品牌榜在请求期间存在数据缺口：{detail}。"
                    f"未生成可能失真的Top {max(1, min(int(limit), 20))}。"
                ),
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
                if ranking_metric == "gmv_actual":
                    comparable.append({"brand": brand, "gmv_actual": current, "gmv_prior": None,
                                       "evol": None, "gmv_growth": None})
                continue
            growth = current - prior
            comparable.append({"brand": brand, "gmv_actual": current, "gmv_prior": prior,
                               "evol": current / prior - 1, "gmv_growth": growth})
        metric = ranking_metric if ranking_metric in {"gmv_actual", "gmv_growth", "evol"} else "gmv_actual"
        candidate_pool = list(comparable)
        if metric != "gmv_actual":
            comparable = [row for row in comparable if row.get(metric) is not None and row[metric] > 0]
        comparable.sort(key=lambda row: float(row.get(metric) or 0), reverse=True)
        result_rows = [{"rank": rank, **row} for rank, row in enumerate(comparable[:max(1, min(int(limit), 20))], 1)]
        return {"query_meta": {"tool": "query_market_top_brands", "segment": segment, "category": category, "platform": platform,
                               "ranking_metric": metric, "current_period": [parsed["current_start"], parsed["current_end"]],
                               "prior_period": [parsed["prior_start"], parsed["prior_end"]]},
                "rows": result_rows, "candidate_pool": candidate_pool, "coverage": chosen, "missing": [], "new_brands": sorted(new_brands, key=lambda x: x["gmv_actual"], reverse=True)[:5],
                "evidence": [{"rank": row["rank"], "brand": row["brand"], "value": row.get(metric)} for row in result_rows],
                "deduplication": "monthly rows collapsed by month/platform/store/brand/category business key before aggregation"}
    except Exception:
        log.exception(
            "[market_top_brands] query failed period=%s segment=%s platform=%s metric=%s",
            period, segment, platform, ranking_metric,
        )
        return {
            "error": "execution_error",
            "message": "品牌榜数据查询超时或连接中断，请稍后重试。系统未生成可能失真的排名。",
        }
