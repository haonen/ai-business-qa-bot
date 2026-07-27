from __future__ import annotations

from pathlib import Path
import json
import re
from typing import Any

import pandas as pd

from bot.db.connection import fetch_df
from bot.utils import DATA_DIR, clean_label, parse_period, safe_div, safe_evol


def tool(fn=None, **_kwargs):
    """Marker decorator. Registry exposes LangChain wrappers separately."""
    return fn if fn is not None else (lambda f: f)


def date_ranges(period: str) -> tuple[str, str, str, str]:
    parsed = parse_period(period)
    return parsed["y2025"][0], parsed["y2025"][1], parsed["y2026"][0], parsed["y2026"][1]


def filter_sku(
    brand: str,
    period: str,
    category: str | None = None,
    kol_driver: str | None = None,
    link_type: str | None = None,
    series: str | None = None,
    function_tag: str | None = None,
) -> pd.DataFrame:
    s25, e25, s26, e26 = date_ranges(period)
    sql = """
        SELECT *
        FROM sku_sales
        WHERE (brand_name LIKE :brand_like OR store_cn LIKE :brand_like)
          AND (
            (bus_date BETWEEN :s25 AND :e25)
            OR (bus_date BETWEEN :s26 AND :e26)
          )
    """
    params = {"brand_like": f"%{brand}%", "s25": s25, "e25": e25, "s26": s26, "e26": e26}
    if category:
        sql += " AND category_cn = :category"
        params["category"] = category
    if kol_driver:
        sql += " AND kol_driver = :kol_driver"
        params["kol_driver"] = kol_driver
    if link_type:
        sql += " AND link_type = :link_type"
        params["link_type"] = link_type
    df = fetch_df(sql, params)
    if df.empty:
        return df
    df["bus_date"] = pd.to_datetime(df["bus_date"])
    if series:
        df = df[df["product_title"].fillna("").str.contains(series, case=False, regex=False)]
    if function_tag:
        words = load_function_tags().get(function_tag, [])
        if words:
            pattern = "|".join(re.escape(w) for w in words)
            df = df[df["product_title"].fillna("").str.contains(pattern, case=False, regex=True)]
    return df


def filter_kol(brand: str, period: str, kol_type: str | None = None) -> pd.DataFrame:
    s25, e25, s26, e26 = date_ranges(period)
    sql = """
        SELECT *
        FROM kol_live_sales
        WHERE brand LIKE :brand_like
          AND (
            (live_start_date BETWEEN :s25 AND :e25)
            OR (live_start_date BETWEEN :s26 AND :e26)
          )
    """
    params = {"brand_like": f"%{brand}%", "s25": s25, "e25": e25, "s26": s26, "e26": e26}
    if kol_type:
        sql += " AND kol_type = :kol_type"
        params["kol_type"] = kol_type
    df = fetch_df(sql, params)
    if not df.empty:
        df["live_start_date"] = pd.to_datetime(df["live_start_date"])
    return df


def split_years(df: pd.DataFrame, date_col: str, period: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    s25, e25, s26, e26 = date_ranges(period)
    col = pd.to_datetime(df[date_col])
    df25 = df[(col >= s25) & (col <= e25)]
    df26 = df[(col >= s26) & (col <= e26)]
    return df25, df26


def agg_metric(df: pd.DataFrame, group_cols: list[str], gmv_col: str = "gmv", unit_col: str = "unit") -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=group_cols + ["gmv", "unit", "atv"])
    agg = df.groupby(group_cols, dropna=False).agg(gmv=(gmv_col, "sum"), unit=(unit_col, "sum")).reset_index()
    agg["atv"] = agg.apply(lambda r: round(r["gmv"] / r["unit"], 2) if r["unit"] else None, axis=1)
    return agg


def combine_two_years(
    df25: pd.DataFrame,
    df26: pd.DataFrame,
    group_cols: list[str],
    gmv_col: str = "gmv",
    unit_col: str = "unit",
) -> tuple[list[dict], dict]:
    a25 = agg_metric(df25, group_cols, gmv_col, unit_col).rename(
        columns={"gmv": "gmv_2025", "unit": "unit_2025", "atv": "atv_2025"}
    )
    a26 = agg_metric(df26, group_cols, gmv_col, unit_col).rename(
        columns={"gmv": "gmv_2026", "unit": "unit_2026", "atv": "atv_2026"}
    )
    merged = pd.merge(a25, a26, on=group_cols, how="outer").fillna({
        "gmv_2025": 0,
        "unit_2025": 0,
        "gmv_2026": 0,
        "unit_2026": 0,
    })
    total_26 = float(merged["gmv_2026"].sum()) if not merged.empty else 0.0
    total_25 = float(merged["gmv_2025"].sum()) if not merged.empty else 0.0
    rows = []
    for _, row in merged.sort_values("gmv_2026", ascending=False).iterrows():
        item = {col: clean_label(row[col], "其他") for col in group_cols}
        g26 = float(row.get("gmv_2026") or 0)
        g25 = float(row.get("gmv_2025") or 0)
        u26 = float(row.get("unit_2026") or 0)
        u25 = float(row.get("unit_2025") or 0)
        item.update({
            "gmv_2026": round(g26),
            "gmv_2025": round(g25),
            "unit_2026": round(u26),
            "unit_2025": round(u25),
            "atv_2026": round(g26 / u26, 2) if u26 else None,
            "atv_2025": round(g25 / u25, 2) if u25 else None,
            "weight": safe_div(g26, total_26),
            "evol": safe_evol(g26, g25),
            "share_delta": None if not total_25 or not total_26 else round(g26 / total_26 - g25 / total_25, 4),
            "gmv_diff": round(g26 - g25),
        })
        rows.append(item)
    total_unit_26 = float(df26[unit_col].sum()) if not df26.empty else 0.0
    total_unit_25 = float(df25[unit_col].sum()) if not df25.empty else 0.0
    total = {
        "gmv_2026": round(total_26),
        "gmv_2025": round(total_25),
        "unit_2026": round(total_unit_26),
        "unit_2025": round(total_unit_25),
        "atv_2026": round(total_26 / total_unit_26, 2) if total_unit_26 else None,
        "atv_2025": round(total_25 / total_unit_25, 2) if total_unit_25 else None,
        "evol": safe_evol(total_26, total_25),
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
