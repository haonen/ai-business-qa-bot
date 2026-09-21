from __future__ import annotations

"""Platform-specific EC follow-up adapter for Douyin product evidence."""

from typing import Any
from datetime import date

import pandas as pd

from bot.media_brand import resolve_source_brand
from bot.brand_query import enabled as brand_gate_enabled
from bot.tools.common import tool
from bot.tools.followup_common import standard_result
from bot.tools.query_douyin_business import (
    PRODUCT_DRIVERS, _infer_series_mapping, _query_s4_products,
)
from bot.utils import parse_ec_period, safe_div, safe_evol


SUPPORTED_DIMENSIONS = {"category", "key_driver", "series", "sku"}


def _name(value: Any) -> str:
    return str(value or "").strip()


def _prepare(frame: pd.DataFrame, brand: str) -> pd.DataFrame:
    work = frame.copy()
    work["sales_gmv"] = pd.to_numeric(work.get("sales_gmv"), errors="coerce").fillna(0.0)
    work["category"] = work.get("category_level_4", "").fillna("").astype(str).str.strip()
    work["product_name"] = work.get("product_name", "").fillna("").astype(str).str.strip()
    work = work[
        work["key_driver"].isin(PRODUCT_DRIVERS)
        & work["category"].ne("未分类")
        & work["category"].ne("")
    ].copy()
    titles = (
        work.groupby("product_name", as_index=False)["sales_gmv"].sum()
        .sort_values("sales_gmv", ascending=False)["product_name"].tolist()
    )
    mapping = _infer_series_mapping([title for title in titles if title][:60], brand_candidates=(brand,))
    work["series"] = work["product_name"].map(mapping).fillna("其他")
    work["sku"] = work.apply(
        lambda row: f"{_name(row.get('item_id'))}｜{_name(row.get('product_name'))}", axis=1,
    )
    return work


def _apply_filters(frame: pd.DataFrame, filters: dict) -> pd.DataFrame:
    work = frame
    for key in ("category", "key_driver", "series"):
        value = filters.get(key)
        if value in (None, ""):
            continue
        wanted = value if isinstance(value, list) else [value]
        normalized = {_name(item).casefold() for item in wanted}
        work = work[work[key].fillna("").astype(str).str.strip().str.casefold().isin(normalized)]
    return work


def _rows(frame: pd.DataFrame, dimensions: list[str], limit: int) -> tuple[list[dict], dict]:
    dims = dimensions or ["_total"]
    work = frame.copy()
    if dims == ["_total"]:
        work["_total"] = "DY"
    grouped = work.groupby(["period_key", *dims], dropna=False)["sales_gmv"].sum().reset_index()
    current = grouped[grouped["period_key"] == "current"].drop(columns="period_key")
    prior = grouped[grouped["period_key"] == "prior"].drop(columns="period_key")
    merged = current.merge(prior, on=dims, how="outer", suffixes=("_actual", "_prior")).fillna(0)
    total_actual = float(merged["sales_gmv_actual"].sum())
    total_prior = float(merged["sales_gmv_prior"].sum())
    result = []
    for _, raw in merged.iterrows():
        actual = float(raw.get("sales_gmv_actual") or 0)
        previous = float(raw.get("sales_gmv_prior") or 0)
        row = {dim: str(raw.get(dim)) for dim in dims}
        row.update({
            "gmv_actual": round(actual), "gmv_prior": round(previous),
            "gmv_diff": round(actual - previous), "gmv_evol": safe_evol(actual, previous),
            "weight": safe_div(actual, total_actual), "weight_prior": safe_div(previous, total_prior),
            "weight_change": (
                safe_div(actual, total_actual) - safe_div(previous, total_prior)
                if total_actual and total_prior else None
            ),
        })
        result.append(row)
    result.sort(key=lambda row: float(row.get("gmv_actual") or 0), reverse=True)
    return result[:max(1, min(limit, 50))], {
        "gmv_actual": round(total_actual), "gmv_prior": round(total_prior),
        "gmv_evol": safe_evol(total_actual, total_prior),
    }


@tool
def query_douyin_followup_table(
    brand: str,
    period: str,
    group_by: list[str] | None = None,
    filters: dict | None = None,
    metrics: list[str] | None = None,
    limit: int = 20,
    brand_aliases: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """Query Douyin once, then build category/driver/series/SKU evidence in memory."""
    try:
        dimensions = list(group_by or [])
        if len(dimensions) > 2 or any(item not in SUPPORTED_DIMENSIONS for item in dimensions):
            raise ValueError("抖音追问不支持该维度组合。")
        resolved = {"brand":brand} if brand_gate_enabled() else resolve_source_brand(brand, "dy", brand_aliases=brand_aliases)
        if resolved.get("error"):
            return resolved
        source_brand = str(resolved.get("brand") or brand)
        period_meta = parse_ec_period(period, date.today().year)
        raw = _query_s4_products(source_brand, period_meta)
        if raw.empty:
            return {"error": "no_data", "message": "指定抖音筛选下无商品数据。"}
        frame = _apply_filters(_prepare(raw, source_brand), dict(filters or {}))
        if frame.empty:
            return {"error": "no_data", "message": "指定抖音品类或Key Driver下无数据。"}
        rows, totals = _rows(frame, dimensions, limit)
        return standard_result(
            query_meta={
                "domain": "ec", "platform": "DY", "brand": brand, "source_brand": source_brand,
                "period": period, "group_by": dimensions, "table": "ai_bot_dy_product_link",
            },
            filters=dict(filters or {}), totals=totals, rows=rows,
            coverage={"current_start": period_meta["current_start"], "current_end": period_meta["current_end"]},
            missing=[],
        )
    except Exception as exc:
        return {"error": "execution_error", "message": str(exc)}


def query_douyin_drill_bundle(
    brand: str, period: str, filters: dict, brand_aliases: list[str] | None = None,
) -> dict:
    """One DB read, with all requested drilldown tables produced from that frame."""
    resolved = {"brand":brand} if brand_gate_enabled() else resolve_source_brand(brand, "dy", brand_aliases=brand_aliases)
    if resolved.get("error"):
        return resolved
    source_brand = str(resolved.get("brand") or brand)
    period_meta = parse_ec_period(period, date.today().year)
    raw = _query_s4_products(source_brand, period_meta)
    if raw.empty:
        return {"error": "no_data", "message": "指定抖音筛选下无商品数据。"}
    frame = _apply_filters(_prepare(raw, source_brand), filters)
    if frame.empty:
        return {"error": "no_data", "message": "指定抖音品类或Key Driver下无数据。"}
    summary_rows, totals = _rows(frame, [], 1)
    dimensions = ["key_driver", "series", "sku"] if filters.get("category") else ["category", "key_driver"]
    tables = []
    titles = {"category": "品类结构", "key_driver": "Key Driver结构", "series": "系列结构", "sku": "Top商品标题"}
    evidence = []
    for dimension in dimensions:
        rows, _ = _rows(frame, [dimension], 5 if dimension == "sku" else 20)
        tables.append({"title": titles[dimension], "rows": rows, "metrics": ["gmv_actual", "gmv_evol"]})
        evidence.extend({"evidence_id": f"dy_{dimension}_{index}", **row} for index, row in enumerate(rows, 1))
    result = standard_result(
        query_meta={
            "domain": "ec", "platform": "DY", "brand": brand, "source_brand": source_brand,
            "period": period, "group_by": [], "table": "ai_bot_dy_product_link",
        }, filters=filters, totals=totals, rows=summary_rows,
        coverage={"current_start": period_meta["current_start"], "current_end": period_meta["current_end"]},
        missing=[],
    )
    result["tables"] = tables
    result["evidence"] = evidence
    return result
