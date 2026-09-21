from __future__ import annotations
from bot.store_report_scope import amount_sql as store_amount, monthly_category_sql
from bot.brand_query import source_fetch, enabled as brand_gate_enabled

from datetime import date
import logging
import os

import pandas as pd

from bot.brand_reference import english_brand_for_chinese
from bot.db.connection import fetch_df, fetch_one
from bot.failures import (
    AnalysisFailure, INVALID_PERIOD, data_coverage_failure,
    failure_result, infrastructure_failure,
)
from bot.platforms import normalized_platform_sql
from bot.period_coverage import normalize_period_to_latest
from bot.tools.common import tool
from bot.tools.market_common import month_slices, store_rank_core_category_sql
from bot.utils import parse_ec_period, safe_div, safe_evol


MONTHLY_BRAND_TABLE = "three_platform_store_rank_monthly"
MARKET_MONTHLY_TABLE = "three_platforms_segmented_markets_monthly"
MARKET_DAILY_TABLE = "three_platforms_segmented_markets_daily"

PLATFORM_CONFIG = {
    "TM": {
        "label": "天猫",
        "daily_table": "tmall_store_ranking_day_jiashicang",
        "category": "category_level_3",
    },
    "DY": {
        "label": "抖音",
        "daily_table": "dy_store_ranking_BFSS_day_jiashicang",
        "category": "category_level_4",
    },
    "JD": {
        "label": "京东",
        "daily_table": "jd_store_ranking_selfrun_day_jiashicang",
        "category": "category_level_3",
    },
}
log = logging.getLogger(__name__)
PLATFORMS = tuple(PLATFORM_CONFIG)
_CATEGORY_COLUMN_CACHE: dict[tuple[str, str], str] = {}
_ALLOWED_CATEGORY_TABLES = {
    MONTHLY_BRAND_TABLE,
    *(config["daily_table"] for config in PLATFORM_CONFIG.values()),
}


def _amount_sql(column: str) -> str:
    return (
        "COALESCE(CAST(REPLACE(NULLIF(TRIM(" + column + "), ''), ',', '') "
        "AS DECIMAL(24,4)), 0)"
    )


def _category_candidates(table: str, platform: str) -> tuple[str, ...]:
    preferred = str(PLATFORM_CONFIG[platform]["category"])
    if table == MONTHLY_BRAND_TABLE:
        return ("category_EN_level_1",)
    return (preferred,)


def _category_sql(column: str, *, monthly_level: int | None = None) -> str:
    if column=="category_EN_level_1":return monthly_category_sql()
    value = (
        f"TRIM(SUBSTRING_INDEX(SUBSTRING_INDEX(TRIM({column}), '-', "
        f"{monthly_level}), '-', -1))"
        if monthly_level else f"TRIM({column})"
    )
    return f"COALESCE(NULLIF({value}, ''), '未分类')"


def _resolve_category_column(table: str, platform: str) -> str:
    key = (table, platform)
    if key in _CATEGORY_COLUMN_CACHE:
        return _CATEGORY_COLUMN_CACHE[key]
    if table not in _ALLOWED_CATEGORY_TABLES or platform not in PLATFORM_CONFIG:
        raise ValueError("不支持的品牌类目表或平台。")
    candidates = _category_candidates(table, platform)
    try:
        columns = fetch_df(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = :table_name
            ORDER BY ordinal_position
            """,
            {"table_name": table},
        )
        available = {
            str(value).strip().casefold(): str(value).strip()
            for value in columns.get("column_name", [])
            if str(value).strip()
        }
    except Exception:
        available = {}
    for candidate in candidates:
        physical = available.get(candidate.casefold())
        if physical:
            _CATEGORY_COLUMN_CACHE[key] = physical
            return physical
    if not available:
        # Retain compatibility with restricted DB users that cannot read
        # information_schema. The query error remains actionable in that case.
        return candidates[0]
    expected = "、".join(candidates)
    raise ValueError(f"表{table}缺少可用类目字段，期望字段为：{expected}。")


def _is_unknown_column_error(exc: Exception) -> bool:
    text = str(exc).casefold()
    return "unknown column" in text or "(1054," in text or " 1054 " in text


def _source_slices(start: str, end: str, platform: str) -> list[dict]:
    config = PLATFORM_CONFIG[platform]
    return [
        {
            **item,
            "platform": platform,
            "source": "monthly" if item["full_month"] else "daily",
            "table": MONTHLY_BRAND_TABLE if item["full_month"] else config["daily_table"],
            "category": config["category"],
        }
        for item in month_slices(start, end)
    ]


def _date_contract(table: str, start: str, end: str) -> tuple[str, dict]:
    if table == "tmall_store_ranking_day_jiashicang":
        return (
            "bus_date BETWEEN :slice_start_raw AND :slice_end_raw",
            {
                "slice_start_raw": start.replace("-", "/"),
                "slice_end_raw": end.replace("-", "/"),
            },
        )
    return (
        "CAST(bus_date AS DATE) BETWEEN :slice_start AND :slice_end",
        {"slice_start": start, "slice_end": end},
    )


def _fetch_brand_category_slice(
    *, brand: str, period_key: str, item: dict,
) -> pd.DataFrame:
    table = item["table"]
    date_clause, date_params = _date_contract(table, item["start"], item["end"])
    platform_clause = (
        "AND UPPER(TRIM(platform)) = :platform_key"
        if item["source"] == "monthly" else ""
    )
    category_scope_clause = (
        "AND " + store_rank_core_category_sql()
        if item["source"] == "monthly" else ""
    )
    resolved = _resolve_category_column(table, item["platform"])
    candidates = tuple(dict.fromkeys((resolved, *_category_candidates(table, item["platform"]))))
    last_error: Exception | None = None
    for category in candidates:
        try:
            category_expr = _category_sql(
                category,
                monthly_level=(
                    (4 if item["platform"] == "DY" else 3)
                    if (
                        item["source"] == "monthly"
                        and category.casefold() == "category_cn"
                    )
                    else None
                ),
            )
            frame = source_fetch(fetch_df,table,"brand_name",'UPPER(TRIM(brand_name)) = UPPER(:brand)',
                f"""
                SELECT
                  :period_key AS period_key,
                  :platform_key AS platform,
                  {category_expr} AS category,
                  SUM({store_amount(_amount_sql('gmv'),item['platform'])}) AS gmv,
                  COUNT(1) AS row_count
                FROM {table}
                WHERE UPPER(TRIM(brand_name)) = UPPER(:brand)
                  {platform_clause}
                  {category_scope_clause}
                  AND {date_clause}
                GROUP BY {category_expr}
                """,
                {
                    "period_key": period_key,
                    "platform_key": item["platform"],
                    "brand": brand,
                    **date_params,
                },
            )
            _CATEGORY_COLUMN_CACHE[(table, item["platform"])] = category
            return frame
        except Exception as exc:
            last_error = exc
            if not _is_unknown_column_error(exc):
                raise
    expected = "、".join(candidates)
    raise ValueError(f"表{table}缺少可用类目字段，已尝试：{expected}。") from last_error


def _query_brand_categories(
    brand: str,
    period_meta: dict,
    *,
    slice_keys: set[tuple[str, str, str, str]] | None = None,
) -> tuple[pd.DataFrame, list[dict], list[dict]]:
    frames: list[pd.DataFrame] = []
    sources: list[dict] = []
    missing: list[dict] = []
    for period_key, start_key, end_key in (
        ("current", "current_start", "current_end"),
        ("prior", "prior_start", "prior_end"),
    ):
        for platform in PLATFORMS:
            for item in _source_slices(period_meta[start_key], period_meta[end_key], platform):
                slice_key = (period_key, item["month"], platform, item["source"])
                if slice_keys is not None and slice_key not in slice_keys:
                    continue
                frame = _fetch_brand_category_slice(
                    brand=brand, period_key=period_key, item=item,
                )
                sources.append({
                    "module": "brand_category",
                    "period": period_key,
                    "month": item["month"],
                    "platform": platform,
                    "source": item["source"],
                    "table": item["table"],
                    "start": item["start"],
                    "end": item["end"],
                })
                has_rows = (
                    not frame.empty
                    and float(pd.to_numeric(frame.get("row_count"), errors="coerce").fillna(0).sum()) > 0
                )
                if has_rows:
                    frames.append(frame)
                else:
                    missing.append({
                        "period": period_key,
                        "month": item["month"],
                        "platform": platform,
                        "source": item["source"],
                    })
    if not frames:
        return (
            pd.DataFrame(columns=["period_key", "platform", "category", "gmv"]),
            sources,
            missing,
        )
    combined = pd.concat(frames, ignore_index=True)
    combined["gmv"] = pd.to_numeric(combined["gmv"], errors="coerce").fillna(0.0)
    return (
        combined.groupby(["period_key", "platform", "category"], as_index=False)["gmv"].sum(),
        sources,
        missing,
    )


def _query_brand_categories_with_reference_fallback(
    brand: str,
    period_meta: dict,
) -> tuple[str, str, pd.DataFrame, list[dict], list[dict]]:
    """Retry each missing brand slice with its unambiguous English reference name."""
    frame, sources, missing = _query_brand_categories(brand, period_meta)
    if brand_gate_enabled():
        return brand, "unified_per_table", frame, sources, missing
    if not missing:
        return brand, "direct", frame, sources, missing
    english = english_brand_for_chinese(brand)
    if not english or english.casefold() == brand.casefold():
        return brand, "direct", frame, sources, missing
    missing_slice_keys = {
        (row["period"], row["month"], row["platform"], row["source"])
        for row in missing
    }
    retry_frame, retry_sources, retry_missing = _query_brand_categories(
        english,
        period_meta,
        slice_keys=missing_slice_keys,
    )
    if retry_frame.empty:
        return brand, "direct", frame, sources, missing
    for row in retry_sources:
        row["query_brand"] = english
        row["brand_match_method"] = "cn_en_reference"
    combined = pd.concat([frame, retry_frame], ignore_index=True)
    combined["gmv"] = pd.to_numeric(combined["gmv"], errors="coerce").fillna(0.0)
    combined = combined.groupby(
        ["period_key", "platform", "category"], as_index=False,
    )["gmv"].sum()
    method = "cn_en_reference" if frame.empty else "direct+cn_en_reference"
    return english, method, combined, sources + retry_sources, retry_missing


def _fetch_douyin_channel_slice(
    *, brand: str, period_key: str, item: dict,
) -> pd.DataFrame:
    table = item["table"]
    date_clause, date_params = _date_contract(table, item["start"], item["end"])
    platform_clause = (
        "AND UPPER(TRIM(platform)) = 'DY'"
        if item["source"] == "monthly" else ""
    )
    category_scope_clause = (
        "AND " + store_rank_core_category_sql()
        if item["source"] == "monthly" else ""
    )
    return source_fetch(fetch_df,table,"brand_name",'UPPER(TRIM(brand_name)) = UPPER(:brand)',
        f"""
        SELECT
          :period_key AS period_key,
          SUM({store_amount(_amount_sql('gmv'),'DY')}) AS brand_gmv,
          SUM({store_amount(_amount_sql('kol_gmv'),'DY')}) AS kol_gmv,
          SUM({store_amount(_amount_sql('storelive_gmv'),'DY')}) AS storelive_gmv,
          COUNT(1) AS row_count
        FROM {table}
        WHERE UPPER(TRIM(brand_name)) = UPPER(:brand)
          {platform_clause}
          {category_scope_clause}
          AND {date_clause}
        """,
        {"period_key": period_key, "brand": brand, **date_params},
    )


def _query_douyin_channels(brand: str, period_meta: dict) -> tuple[pd.DataFrame, list[dict]]:
    frames: list[pd.DataFrame] = []
    sources: list[dict] = []
    for period_key, start_key, end_key in (
        ("current", "current_start", "current_end"),
        ("prior", "prior_start", "prior_end"),
    ):
        for item in _source_slices(period_meta[start_key], period_meta[end_key], "DY"):
            frame = _fetch_douyin_channel_slice(
                brand=brand, period_key=period_key, item=item,
            )
            sources.append({
                "module": "douyin_channel",
                "period": period_key,
                "month": item["month"],
                "platform": "DY",
                "source": item["source"],
                "table": item["table"],
                "start": item["start"],
                "end": item["end"],
            })
            if not frame.empty:
                frames.append(frame)
    if not frames:
        return pd.DataFrame(), sources
    combined = pd.concat(frames, ignore_index=True)
    numeric = ["brand_gmv", "kol_gmv", "storelive_gmv", "row_count"]
    for column in numeric:
        combined[column] = pd.to_numeric(combined[column], errors="coerce").fillna(0.0)
    return combined.groupby("period_key", as_index=False)[numeric].sum(), sources


def _fetch_market_slice(
    *, segment: str, period_key: str, item: dict,
) -> pd.DataFrame:
    monthly = item["full_month"]
    table = MARKET_MONTHLY_TABLE if monthly else MARKET_DAILY_TABLE
    platform_expr = normalized_platform_sql(table)
    if monthly:
        # Production segmented-market month rows use normal natural dates
        # (for example 2026-02-01 for February). Do not reinterpret DAY()
        # as the business month: that collapses February and March into January.
        date_clause = "CAST(bus_date AS DATE) BETWEEN :slice_start AND :slice_end"
        category_clause = "AND UPPER(TRIM(category_EN)) = 'TOTAL BEAUTY'"
    else:
        date_clause = "CAST(bus_date AS DATE) BETWEEN :slice_start AND :slice_end"
        category_clause = ""
    return fetch_df(
        f"""
        SELECT
          :period_key AS period_key,
          {platform_expr} AS platform,
          SUM({_amount_sql('gmv')}) AS gmv,
          COUNT(1) AS row_count
        FROM {table}
        WHERE UPPER(TRIM(global_segment)) = :segment
          {category_clause}
          AND UPPER(TRIM(platform)) IN ('TM', 'TMALL', '天猫', 'DY', 'DOUYIN', '抖音', 'JD', 'JINGDONG', '京东')
          AND {date_clause}
        GROUP BY {platform_expr}
        """,
        {
            "period_key": period_key,
            "segment": segment.upper(),
            "slice_start": item["start"],
            "slice_end": item["end"],
        },
    )


def _query_market(segment: str, period_meta: dict) -> tuple[pd.DataFrame, list[dict], list[dict]]:
    frames: list[pd.DataFrame] = []
    sources: list[dict] = []
    missing: list[dict] = []
    for period_key, start_key, end_key in (
        ("current", "current_start", "current_end"),
        ("prior", "prior_start", "prior_end"),
    ):
        for item in month_slices(period_meta[start_key], period_meta[end_key]):
            frame = _fetch_market_slice(segment=segment, period_key=period_key, item=item)
            table = MARKET_MONTHLY_TABLE if item["full_month"] else MARKET_DAILY_TABLE
            source = "monthly" if item["full_month"] else "daily"
            sources.append({
                "module": "market",
                "segment": segment,
                "period": period_key,
                "month": item["month"],
                "source": source,
                "table": table,
                "start": item["start"],
                "end": item["end"],
            })
            present = set(str(value).upper() for value in frame.get("platform", []))
            for platform in PLATFORMS:
                if platform not in present:
                    missing.append({
                        "segment": segment,
                        "period": period_key,
                        "month": item["month"],
                        "platform": platform,
                        "source": source,
                    })
            if not frame.empty:
                frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=["period_key", "platform", "gmv"]), sources, missing
    combined = pd.concat(frames, ignore_index=True)
    combined["gmv"] = pd.to_numeric(combined["gmv"], errors="coerce").fillna(0.0)
    return (
        combined.groupby(["period_key", "platform"], as_index=False)["gmv"].sum(),
        sources,
        missing,
    )


def _platform_totals(frame: pd.DataFrame, value_col: str = "gmv") -> dict[str, dict[str, float]]:
    result = {
        period: {platform: 0.0 for platform in PLATFORMS}
        for period in ("current", "prior")
    }
    if not frame.empty:
        grouped = frame.groupby(["period_key", "platform"], as_index=False)[value_col].sum()
        for _, row in grouped.iterrows():
            period = str(row["period_key"])
            platform = str(row["platform"]).upper()
            if period in result and platform in result[period]:
                result[period][platform] = float(row[value_col] or 0)
    for period in result:
        result[period]["TTL"] = sum(result[period][platform] for platform in PLATFORMS)
    return result


def _overall_rows(
    brand_totals: dict, beauty_totals: dict, pure_mass_totals: dict | None,
) -> list[dict]:
    rows: list[dict] = []
    for platform in ("TTL", *PLATFORMS):
        current = brand_totals["current"][platform]
        prior = brand_totals["prior"][platform]
        beauty_share = safe_div(current, beauty_totals["current"][platform])
        beauty_share_prior = safe_div(prior, beauty_totals["prior"][platform])
        row = {
            "platform": platform,
            "gmv_current": current,
            "gmv_prior": prior,
            "evol": safe_evol(current, prior),
            "beauty_share": beauty_share,
            "beauty_share_prior": beauty_share_prior,
            "beauty_share_change": (
                beauty_share - beauty_share_prior
                if beauty_share is not None and beauty_share_prior is not None else None
            ),
        }
        if pure_mass_totals:
            pure_share = safe_div(current, pure_mass_totals["current"][platform])
            pure_share_prior = safe_div(prior, pure_mass_totals["prior"][platform])
            row.update({
                "pure_mass_share": pure_share,
                "pure_mass_share_prior": pure_share_prior,
                "pure_mass_share_change": (
                    pure_share - pure_share_prior
                    if pure_share is not None and pure_share_prior is not None else None
                ),
            })
        rows.append(row)
    return rows


def _top_categories(frame: pd.DataFrame, limit: int = 5) -> dict[str, list[dict]]:
    totals = _platform_totals(frame)
    result: dict[str, list[dict]] = {}
    for platform in PLATFORMS:
        platform_frame = frame[frame["platform"] == platform]
        grouped = (
            platform_frame.groupby(["period_key", "category"], as_index=False)["gmv"].sum()
            if not platform_frame.empty else pd.DataFrame(columns=["period_key", "category", "gmv"])
        )
        current = grouped[grouped["period_key"] == "current"].sort_values("gmv", ascending=False)
        prior_lookup = {
            str(row["category"]): float(row["gmv"] or 0)
            for _, row in grouped[grouped["period_key"] == "prior"].iterrows()
        }
        rows: list[dict] = []
        for _, row in current[current["gmv"] > 0].head(limit).iterrows():
            category = str(row["category"])
            current_gmv = float(row["gmv"] or 0)
            prior_gmv = prior_lookup.get(category, 0.0)
            weight = safe_div(current_gmv, totals["current"][platform])
            weight_prior = safe_div(prior_gmv, totals["prior"][platform])
            rows.append({
                "category": category,
                "gmv_current": current_gmv,
                "gmv_prior": prior_gmv,
                "evol": safe_evol(current_gmv, prior_gmv),
                "weight": weight,
                "weight_prior": weight_prior,
                "weight_change": (
                    weight - weight_prior
                    if weight is not None and weight_prior is not None else None
                ),
            })
        result[platform] = rows
    return result


def _channel_rows(frame: pd.DataFrame) -> dict:
    values = {
        period: {"brand_gmv": 0.0, "kol_gmv": 0.0, "storelive_gmv": 0.0}
        for period in ("current", "prior")
    }
    for _, row in frame.iterrows():
        period = str(row["period_key"])
        if period in values:
            for column in values[period]:
                values[period][column] += float(row.get(column) or 0)
    for period in values:
        values[period]["short_other_gmv"] = (
            values[period]["brand_gmv"]
            - values[period]["kol_gmv"]
            - values[period]["storelive_gmv"]
        )
    if any(values[period]["short_other_gmv"] < -0.01 for period in values):
        return {
            "error": "negative_short_other",
            "message": "抖音短视频及其他GMV倒减为负，请检查gmv、kol_gmv和storelive_gmv源数据。",
        }
    rows = []
    for key, label in (
        ("kol_gmv", "KOL直播"),
        ("storelive_gmv", "品牌自营直播"),
        ("short_other_gmv", "短视频及其他"),
    ):
        current = values["current"][key]
        prior = values["prior"][key]
        weight = safe_div(current, values["current"]["brand_gmv"])
        weight_prior = safe_div(prior, values["prior"]["brand_gmv"])
        rows.append({
            "channel": label,
            "gmv_current": current,
            "gmv_prior": prior,
            "gmv_change": current - prior,
            "evol": safe_evol(current, prior),
            "weight": weight,
            "weight_prior": weight_prior,
            "weight_change": (
                weight - weight_prior
                if weight is not None and weight_prior is not None else None
            ),
        })
    return {"rows": rows, "totals": values}


def _is_pure_mass_brand(brand: str) -> bool:
    row = source_fetch(fetch_one,MONTHLY_BRAND_TABLE,"brand_name",'TRIM(brand_name) = :brand',
        f"""
        SELECT COUNT(1) AS mass_rows
        FROM {MONTHLY_BRAND_TABLE}
        WHERE TRIM(brand_name) = :brand
          AND (selectivity IS NULL OR TRIM(selectivity) = '')
        """,
        {"brand": brand},
    )
    return int(row.get("mass_rows") or 0) > 0


def _latest_common_date(brand: str) -> str | None:
    """Latest date covered by every daily source required by the full report."""
    try:
        values: list[str] = []
        for platform, config in PLATFORM_CONFIG.items():
            table = config["daily_table"]
            date_expr = "MAX(bus_date)" if platform == "TM" else "MAX(CAST(bus_date AS DATE))"
            row = source_fetch(fetch_one,table,"brand_name",'UPPER(TRIM(brand_name)) = UPPER(:brand)',
                f"SELECT {date_expr} AS max_date FROM {table} "
                "WHERE UPPER(TRIM(brand_name)) = UPPER(:brand)",
                {"brand": brand},
            )
            value = row.get("max_date")
            if not value:
                return None
            values.append(str(value)[:10].replace("/", "-"))
        market = fetch_one(
            f"SELECT MAX(CAST(bus_date AS DATE)) AS max_date FROM {MARKET_DAILY_TABLE}",
            {},
        ).get("max_date")
        if not market:
            return None
        values.append(str(market)[:10])
        return min(values)
    except Exception:
        return None


def _coverage_result(brand: str, source_brand: str, period: str, period_meta: dict, error: str) -> dict:
    latest = _latest_common_date(source_brand)
    return failure_result(data_coverage_failure(
        brand=brand, platform_label="三平台",
        requested_period=(period_meta["current_start"] if period_meta["current_start"] == period_meta["current_end"]
                          else f"{period_meta['current_start']}至{period_meta['current_end']}"),
        latest_available_date=latest,
        after_latest=bool(latest and period_meta["current_end"] > latest),
    ), error=error)


@tool
def query_three_platform_competitor(brand: str, period: str) -> dict:
    """Return the fixed three-platform competitor business analysis dataset."""
    try:
        if brand_gate_enabled():
            from bot.brand_query import require_total_scope,require_platform
            require_total_scope()
            require_platform('TTL')
        input_brand = str(brand or "").strip()
        if not input_brand:
            return {"error": "missing_brand", "message": "请提供需要分析的品牌。"}
        try:
            period_meta = parse_ec_period(period, date.today().year)
        except ValueError as exc:
            return failure_result(AnalysisFailure(
                failure_kind=INVALID_PERIOD, user_message=str(exc),
                requested_period=period, retry_slot="period",
                preserved_slots=("brand", "platform", "goals"),
            ))
        legacy_emergency = os.environ.get(
            "LEGACY_PIPELINE_EMERGENCY", "0",
        ).strip().lower() in {"1", "true", "yes", "on"}
        if not legacy_emergency:
            coverage_brand = input_brand if brand_gate_enabled() else english_brand_for_chinese(input_brand) or input_brand
            latest_common = _latest_common_date(coverage_brand)
            if latest_common and period_meta["current_start"] > latest_common:
                return _coverage_result(
                    input_brand, coverage_brand, period, period_meta, "no_data",
                )
            period_meta, adjustment = normalize_period_to_latest(
                period_meta, latest_common,
            )
            if adjustment:
                log.info(
                    "[three_platform] MTD coverage adjusted brand=%s requested=%s "
                    "effective_end=%s",
                    input_brand, period, period_meta["current_end"],
                )
        (
            source_brand,
            brand_match_method,
            category_frame,
            brand_sources,
            brand_missing,
        ) = _query_brand_categories_with_reference_fallback(input_brand, period_meta)
        if category_frame.empty:
            return _coverage_result(input_brand, source_brand, period, period_meta, "no_data")
        if brand_missing:
            return _coverage_result(input_brand, source_brand, period, period_meta, "incomplete_brand")

        beauty_frame, beauty_sources, beauty_missing = _query_market("Beauty Market", period_meta)
        if beauty_missing:
            return _coverage_result(input_brand, source_brand, period, period_meta, "incomplete_market")

        pure_mass = _is_pure_mass_brand(source_brand)
        pure_frame = pd.DataFrame()
        pure_sources: list[dict] = []
        if pure_mass:
            pure_frame, pure_sources, pure_missing = _query_market("pure mass", period_meta)
            if pure_missing:
                return _coverage_result(input_brand, source_brand, period, period_meta, "incomplete_market")

        channel_frame, channel_sources = _query_douyin_channels(source_brand, period_meta)
        channel_result = _channel_rows(channel_frame)
        if channel_result.get("error"):
            return channel_result

        brand_totals = _platform_totals(category_frame)
        beauty_totals = _platform_totals(beauty_frame)
        pure_totals = _platform_totals(pure_frame) if pure_mass else None
        return {
            "brand": input_brand,
            "source_brand": source_brand,
            "brand_match_method": brand_match_method,
            "period": period,
            "period_meta": period_meta,
            "is_pure_mass": pure_mass,
            "overall": _overall_rows(brand_totals, beauty_totals, pure_totals),
            "categories": _top_categories(category_frame, limit=5),
            "channels": channel_result["rows"],
            "sources": brand_sources + beauty_sources + pure_sources + channel_sources,
            "reconciliation": {
                "brand_totals": brand_totals,
                "beauty_market_totals": beauty_totals,
                "pure_mass_totals": pure_totals,
                "douyin_channel_totals": channel_result["totals"],
            },
        }
    except Exception as exc:
        return failure_result(
            infrastructure_failure(exc, requested_period=period),
            error="execution_error",
        )
