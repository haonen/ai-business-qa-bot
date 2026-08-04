from __future__ import annotations

from pathlib import Path
import json
import logging
import re
import time
from typing import Any

import pandas as pd

from bot.db.connection import fetch_df, fetch_one
from bot.media_brand import resolve_source_brand
from bot.utils import DATA_DIR, clean_label, parse_ec_period, safe_div, safe_evol


log = logging.getLogger(__name__)


def tool(fn=None, **_kwargs):
    """Marker decorator. Registry exposes LangChain wrappers separately."""
    return fn if fn is not None else (lambda f: f)


class EcDataError(ValueError):
    pass


_EC_CONTEXT_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}
_EC_SKU_CACHE: dict[tuple[str, str, str, str, str], tuple[float, pd.DataFrame]] = {}


def ec_query_context(
    brand: str,
    period: str,
    brand_aliases: list[str] | tuple[str, ...] | None = None,
) -> dict:
    cache_key = (str(brand or "").strip(), str(period or "").strip())
    cached = _EC_CONTEXT_CACHE.get(cache_key)
    if cached and time.monotonic() - cached[0] < 300:
        return dict(cached[1])

    resolved = resolve_source_brand(brand, "tmall", brand_aliases=brand_aliases)
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

    latest = fetch_one(
        """
        SELECT bus_date AS max_date
        FROM ai_bot_tmall_product_link FORCE INDEX (idx_tmall_brand_date)
        WHERE brand_name = :brand
        ORDER BY bus_date DESC
        LIMIT 1
        """,
        {"brand": source_brand},
    )
    if not latest.get("max_date"):
        raise EcDataError(f"品牌“{brand}”在天猫商品链接数据中没有记录。")
    max_date = str(latest["max_date"])
    parsed = parse_ec_period(period, int(max_date[:4]))

    if parsed["current_end"] > max_date:
        raise EcDataError(
            f"当前品牌数据更新至{max_date}，"
            f"你指定的本期为{parsed['current_start']}至{parsed['current_end']}，请重新指定时间。"
        )
    current_exists = fetch_one(
        """
        SELECT 1 AS row_exists
        FROM ai_bot_tmall_product_link FORCE INDEX (idx_tmall_brand_date)
        WHERE brand_name = :brand
          AND bus_date BETWEEN :current_start AND :current_end
        LIMIT 1
        """,
        {
            "brand": source_brand,
            "current_start": parsed["current_start"],
            "current_end": parsed["current_end"],
        },
    )
    if not current_exists.get("row_exists"):
        raise EcDataError(f"品牌“{source_brand}”在本期没有商品链接数据，报告未生成。")
    prior_exists = fetch_one(
        """
        SELECT 1 AS row_exists
        FROM ai_bot_tmall_product_link FORCE INDEX (idx_tmall_brand_date)
        WHERE brand_name = :brand
          AND bus_date BETWEEN :prior_start AND :prior_end
        LIMIT 1
        """,
        {
            "brand": source_brand,
            "prior_start": parsed["prior_start"],
            "prior_end": parsed["prior_end"],
        },
    )
    if not prior_exists.get("row_exists"):
        raise EcDataError(f"品牌“{source_brand}”在去年同期没有商品链接数据，报告未生成。")

    context = {
        **parsed,
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
    if cached and time.monotonic() - cached[0] < 300:
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
            FROM ai_bot_tmall_product_link FORCE INDEX (idx_tmall_brand_date)
            WHERE brand_name = :brand
              AND bus_date BETWEEN :current_start AND :current_end
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
            FROM ai_bot_tmall_product_link FORCE INDEX (idx_tmall_brand_date)
            WHERE brand_name = :brand
              AND bus_date BETWEEN :prior_start AND :prior_end
            GROUP BY item_id, key_driver
        """
        params = {
            "brand": context["source_brand"],
            **{k: context[k] for k in (
                "current_start", "current_end", "prior_start", "prior_end"
            )},
        }
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
