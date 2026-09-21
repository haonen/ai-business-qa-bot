from __future__ import annotations
from bot.timing import timed, job_timed, sql_timed, span, event

from pathlib import Path
import json
import logging
import re
import time
from typing import Any

import pandas as pd

from bot.db.connection import fetch_df, fetch_one
from bot.failures import (
    AnalysisFailure, DATA_AFTER_LATEST, INFRASTRUCTURE_ERROR, INVALID_PERIOD,
    NO_DATA_IN_RANGE, StructuredAnalysisError,
)
from bot.media_brand import resolve_source_brand
from bot.brand_query import enabled as brand_gate_enabled, brand_predicate
from bot.period_coverage import normalize_period_to_latest
from bot.utils import DATA_DIR, clean_label, parse_ec_period, safe_div, safe_evol


log = logging.getLogger(__name__)


MIGRATED_BRAND_TOOLS = {
    'query_ec_nso','query_ec_bet_monthly','query_three_platform_competitor','query_media_investment','query_bet_followup_table','query_social_search','query_kol_performance','query_douyin_business','query_douyin_followup_table','query_ecip_tmall_gmv','query_jd_business','query_tmall_gmv','query_category','query_driver','query_sku_list',
    'query_series','query_scene_tag','query_compare','query_ec_followup_table',
}


def tool(fn=None, **_kwargs):
    """Registry marker and opt-in migration boundary for brand-bearing tools."""
    from functools import wraps
    from inspect import signature
    def decorate(function):
        branded='brand' in signature(function).parameters
        @wraps(function)
        def guarded(*args,**kwargs):
            if brand_gate_enabled() and branded and function.__name__ not in MIGRATED_BRAND_TOOLS:
                return {'error':'brand_query_not_migrated','failure_kind':'BRAND_QUERY_NOT_MIGRATED',
                        'message':'该查询尚未接入统一品牌字典，本次未执行旧查询。',
                        'tool':function.__name__}
            with span('tool.'+function.__name__):
                return function(*args,**kwargs)
        return guarded
    return decorate(fn) if fn is not None else decorate


class EcDataError(StructuredAnalysisError):
    def __init__(
        self,
        message: str,
        *,
        failure_kind: str = INFRASTRUCTURE_ERROR,
        requested_period: str | None = None,
        latest_available_date: str | None = None,
        retry_slot: str | None = None,
    ):
        super().__init__(AnalysisFailure(
            failure_kind=failure_kind,
            user_message=message,
            requested_period=requested_period,
            latest_available_date=latest_available_date,
            retry_slot=retry_slot,
            preserved_slots=("brand", "platform", "goals"),
        ))


_EC_CONTEXT_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}
_EC_SKU_CACHE: dict[tuple[str, str, str, str, str], tuple[float, pd.DataFrame]] = {}


def ec_query_context(
    brand: str,
    period: str,
    brand_aliases: list[str] | tuple[str, ...] | None = None,
) -> dict:
    cache_key = (str(brand or "").strip(), str(period or "").strip())
    cached = _EC_CONTEXT_CACHE.get(cache_key)
    if not brand_gate_enabled() and cached and time.monotonic() - cached[0] < 300:
        return dict(cached[1])

    mapped = brand_predicate(brand, 'ai_bot_tmall_product_link', 'brand_name') if brand_gate_enabled() else None
    resolved = ({'brand': brand, 'match_method':'unified_dictionary'} if mapped else
                resolve_source_brand(brand, "tmall", brand_aliases=brand_aliases))
    def execute_one(sql, params):
        if mapped:
            sql=sql.replace('brand_name = :brand',mapped['sql'])
            params={k:v for k,v in params.items() if k!='brand'} | mapped['params']
        return fetch_one(sql,params)
    if resolved.get("error"):
        candidates = resolved.get("candidates") or []
        suffix = f" 可选品牌：{'、'.join(candidates)}。" if candidates else ""
        raise EcDataError(str(resolved.get("message") or "品牌解析失败。") + suffix)
    source_brand = str(resolved["brand"])
    log.info(
        "[ec_brand] input=%s source_brand=%s method=%s aliases=%s",
        brand,
        source_brand,
        resolved.get("match_method"),
        list(brand_aliases or []),
    )

    latest = execute_one(
        """
        SELECT CAST(bus_date AS DATE) AS max_date
        FROM ai_bot_tmall_product_link
        WHERE brand_name = :brand
        ORDER BY CAST(bus_date AS DATE) DESC
        LIMIT 1
        """,
        {"brand": source_brand},
    )
    if not latest.get("max_date"):
        from bot.brand_query import _scope
        scope = _scope.get() if mapped else None
        category = scope.category if scope else 'TTL'
        label = {'HAIR':'洗护','SKIN':'护肤（非男士）','MEX':'男士','MAKEUP':'彩妆'}.get(category)
        message = (f"品牌“{brand}”在天猫商品链接数据的{label}范围内没有记录，不代表该品牌其他品类没有销售。请换一个品类或选择全部生意。"
                   if label else f"品牌“{brand}”在天猫商品链接数据中没有记录。")
        raise EcDataError(message, failure_kind=NO_DATA_IN_RANGE, requested_period=str(period))
    max_date = str(latest["max_date"])
    try:
        parsed = parse_ec_period(period, int(max_date[:4]))
    except ValueError as exc:
        raise EcDataError(
            str(exc), failure_kind=INVALID_PERIOD,
            requested_period=str(period), retry_slot="period",
        ) from exc

    parsed, adjustment = normalize_period_to_latest(parsed, max_date)
    if mapped:
        from bot.brand_query import validate_query_dates
        validate_query_dates(parsed)
    if parsed["current_start"] > max_date:
        raise EcDataError(
            f"当前品牌数据更新至{max_date}，"
            f"你指定的期间从{parsed['current_start']}开始，尚无可用数据。"
            f"请提供不晚于{max_date}的新日期。",
            failure_kind=DATA_AFTER_LATEST,
            requested_period=str(period), latest_available_date=max_date,
            retry_slot="period",
        )
    if adjustment:
        log.info("[ec_coverage] brand=%s adjustment=%s", source_brand, adjustment)
    current_exists = execute_one(
        """
        SELECT 1 AS row_exists
        FROM ai_bot_tmall_product_link
        WHERE brand_name = :brand
          AND CAST(bus_date AS DATE) BETWEEN :current_start AND :current_end
        LIMIT 1
        """,
        {
            "brand": source_brand,
            "current_start": parsed["current_start"],
            "current_end": parsed["current_end"],
        },
    )
    if not current_exists.get("row_exists"):
        raise EcDataError(
            f"当前品牌数据更新至{max_date}，但品牌“{source_brand}”"
            f"在{parsed['current_start']}至{parsed['current_end']}没有商品链接数据。"
            "请改用有数据的日期，本次未生成报告。",
            failure_kind=NO_DATA_IN_RANGE,
            requested_period=str(period), latest_available_date=max_date,
            retry_slot="period",
        )
    prior_exists = execute_one(
        """
        SELECT 1 AS row_exists
        FROM ai_bot_tmall_product_link
        WHERE brand_name = :brand
          AND CAST(bus_date AS DATE) BETWEEN :prior_start AND :prior_end
        LIMIT 1
        """,
        {
            "brand": source_brand,
            "prior_start": parsed["prior_start"],
            "prior_end": parsed["prior_end"],
        },
    )
    if not prior_exists.get("row_exists"):
        raise EcDataError(
            f"品牌“{source_brand}”在去年同期没有商品链接数据，报告未生成。",
            failure_kind=NO_DATA_IN_RANGE,
            requested_period=str(period), latest_available_date=max_date,
            retry_slot="period",
        )

    context = {
        **parsed,
        "brand_filter": mapped,
        "input_brand": brand,
        "source_brand": source_brand,
        "brand_match_method": resolved.get("match_method"),
        "source_max_date": max_date,
        "current_rows": None,
        "prior_rows": None,
    }
    _EC_CONTEXT_CACHE[cache_key] = (time.monotonic(), context)
    return dict(context)


def filter_sku(
    brand: str,
    period: str,
    category: str | None = None,
    key_driver: str | None = None,
    series: str | None = None,
    function_tag: str | None = None,
    brand_aliases: list[str] | tuple[str, ...] | None = None,
) -> pd.DataFrame:
    context = ec_query_context(brand, period, brand_aliases=brand_aliases)
    cache_key = (
        context["source_brand"],
        context["current_start"],
        context["current_end"],
        context["prior_start"],
        context["prior_end"],
    )
    cached = _EC_SKU_CACHE.get(cache_key)
    if not brand_gate_enabled() and cached and time.monotonic() - cached[0] < 300:
        df = cached[1].copy(deep=True)
    else:
        sql = """
            SELECT
              :current_start AS bus_date,
              MAX(category_CN) AS category_cn,
              item_id,
              MAX(product_title) AS product_title,
              key_driver,
              SUM(gmv) AS gmv,
              SUM(unit) AS unit
            FROM ai_bot_tmall_product_link
            WHERE brand_name = :brand
              AND CAST(bus_date AS DATE) BETWEEN :current_start AND :current_end
            GROUP BY item_id, key_driver

            UNION ALL

            SELECT
              :prior_start AS bus_date,
              MAX(category_CN) AS category_cn,
              item_id,
              MAX(product_title) AS product_title,
              key_driver,
              SUM(gmv) AS gmv,
              SUM(unit) AS unit
            FROM ai_bot_tmall_product_link
            WHERE brand_name = :brand
              AND CAST(bus_date AS DATE) BETWEEN :prior_start AND :prior_end
            GROUP BY item_id, key_driver
        """
        params = {
            "brand": context["source_brand"],
            **{k: context[k] for k in (
                "current_start", "current_end", "prior_start", "prior_end"
            )},
        }
        mapped=context.get('brand_filter')
        if mapped:
            sql=sql.replace('brand_name = :brand',mapped['sql'])
            params={k:v for k,v in params.items() if k!='brand'} | mapped['params']
        df = fetch_df(sql, params)
        df.attrs["ec_context"] = context
        _EC_SKU_CACHE[cache_key] = (time.monotonic(), df.copy(deep=True))
    df.attrs["ec_context"] = context
    if df.empty:
        return df
    df["bus_date"] = pd.to_datetime(df["bus_date"])
    if category:
        df = df[df["category_cn"] == category]
    if key_driver:
        df = df[df["key_driver"] == key_driver]
    if series:
        df = df[df["product_title"].fillna("").str.contains(series, case=False, regex=False)]
    if function_tag:
        words = load_function_tags().get(function_tag, [])
        if words:
            pattern = "|".join(re.escape(w) for w in words)
            df = df[df["product_title"].fillna("").str.contains(pattern, case=False, regex=True)]
    df.attrs["ec_context"] = context
    return df


def split_periods(df: pd.DataFrame, date_col: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    context = df.attrs.get("ec_context") or {}
    if not context:
        raise EcDataError("查询结果缺少时间上下文。")
    col = pd.to_datetime(df[date_col])
    prior = df[(col >= context["prior_start"]) & (col <= context["prior_end"])]
    current = df[(col >= context["current_start"]) & (col <= context["current_end"])]
    return prior, current


def agg_metric(df: pd.DataFrame, group_cols: list[str], gmv_col: str = "gmv", unit_col: str = "unit") -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=group_cols + ["gmv", "unit", "atv"])
    agg = df.groupby(group_cols, dropna=False).agg(gmv=(gmv_col, "sum"), unit=(unit_col, "sum")).reset_index()
    agg["atv"] = agg.apply(lambda r: round(r["gmv"] / r["unit"], 2) if r["unit"] else None, axis=1)
    return agg


def combine_periods(
    prior_df: pd.DataFrame,
    current_df: pd.DataFrame,
    group_cols: list[str],
    gmv_col: str = "gmv",
    unit_col: str = "unit",
) -> tuple[list[dict], dict]:
    prior = agg_metric(prior_df, group_cols, gmv_col, unit_col).rename(
        columns={"gmv": "gmv_prior", "unit": "unit_prior", "atv": "atv_prior"}
    )
    current = agg_metric(current_df, group_cols, gmv_col, unit_col).rename(
        columns={"gmv": "gmv_current", "unit": "unit_current", "atv": "atv_current"}
    )
    merged = pd.merge(prior, current, on=group_cols, how="outer").fillna({
        "gmv_prior": 0,
        "unit_prior": 0,
        "gmv_current": 0,
        "unit_current": 0,
    })
    total_current = float(merged["gmv_current"].sum()) if not merged.empty else 0.0
    total_prior = float(merged["gmv_prior"].sum()) if not merged.empty else 0.0
    rows = []
    for _, row in merged.sort_values("gmv_current", ascending=False).iterrows():
        item = {col: clean_label(row[col], "其他") for col in group_cols}
        g_current = float(row.get("gmv_current") or 0)
        g_prior = float(row.get("gmv_prior") or 0)
        u_current = float(row.get("unit_current") or 0)
        u_prior = float(row.get("unit_prior") or 0)
        item.update({
            "gmv_current": round(g_current),
            "gmv_prior": round(g_prior),
            "unit_current": round(u_current),
            "unit_prior": round(u_prior),
            "atv_current": round(g_current / u_current, 2) if u_current else None,
            "atv_prior": round(g_prior / u_prior, 2) if u_prior else None,
            "weight": safe_div(g_current, total_current),
            "evol": safe_evol(g_current, g_prior),
            "share_delta": None if not total_prior or not total_current else round(g_current / total_current - g_prior / total_prior, 4),
            "gmv_diff": round(g_current - g_prior),
        })
        rows.append(item)
    total_unit_current = float(current_df[unit_col].sum()) if not current_df.empty else 0.0
    total_unit_prior = float(prior_df[unit_col].sum()) if not prior_df.empty else 0.0
    total = {
        "gmv_current": round(total_current),
        "gmv_prior": round(total_prior),
        "unit_current": round(total_unit_current),
        "unit_prior": round(total_unit_prior),
        "atv_current": round(total_current / total_unit_current, 2) if total_unit_current else None,
        "atv_prior": round(total_prior / total_unit_prior, 2) if total_unit_prior else None,
        "evol": safe_evol(total_current, total_prior),
    }
    return rows, total


def load_series_map() -> dict[str, Any]:
    path = DATA_DIR / "series_map.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def load_function_tags() -> dict[str, list[str]]:
    path = DATA_DIR / "function_tags.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def match_function_tag(title: str) -> str | None:
    tags = load_function_tags()
    for tag, words in tags.items():
        if any(word in (title or "") for word in words):
            return tag
    return None


def match_scene_tags(title: str) -> list[str]:
    title = title or ""
    bracket_tags = re.findall(r"【([^】]{1,20})】", title)
    keywords = [
        "520礼物", "520", "618现货", "618", "付尾款", "付定金", "抢先购",
        "新客", "小样节", "送男朋友", "送女朋友", "礼盒", "生日", "直播间",
    ]
    tags = [t.strip() for t in bracket_tags if t.strip()]
    tags.extend(k for k in keywords if k in title)
    return sorted(set(tags))
