from __future__ import annotations

import re

import pandas as pd

from bot.db.connection import fetch_df
from bot.tools.common import ec_query_context, load_function_tags, tool
from bot.tools.followup_common import month_keys, prior_month_key, standard_result
from bot.tools.query_series import _infer_series_from_title
from bot.utils import safe_div, safe_evol


EC_DIMENSIONS = {"month", "category", "key_driver", "series", "sku"}
EC_PAIRS = {
    ("month", "category"), ("month", "key_driver"),
    ("category", "key_driver"), ("category", "series"),
    ("key_driver", "series"),
}


def _validate(group_by: list[str], filters: dict):
    if len(group_by) > 2 or any(item not in EC_DIMENSIONS for item in group_by):
        raise ValueError("不支持的EC维度组合。")
    if len(group_by) == 2 and tuple(group_by) not in EC_PAIRS and tuple(reversed(group_by)) not in EC_PAIRS:
        raise ValueError("该EC交叉维度未开放。")
    if "sku" in group_by and not any(filters.get(k) for k in ("category", "key_driver", "series", "function_tag")):
        raise ValueError("SKU维度至少需要一个品类、渠道、系列或功能线筛选。")


def _raw_rows(context: dict, filters: dict) -> pd.DataFrame:
    sql = """
        SELECT
          CASE
            WHEN bus_date BETWEEN :current_start AND :current_end THEN 'current'
            ELSE 'prior'
          END AS period_key,
          DATE_FORMAT(bus_date, '%Y-%m') AS source_month,
          category_CN AS category,
          key_driver,
          item_id AS sku,
          MAX(product_title) AS product_title,
          SUM(gmv) AS gmv,
          SUM(unit) AS unit,
          COUNT(*) AS row_count
        FROM ai_bot_tmall_product_link FORCE INDEX (idx_tmall_brand_date)
        WHERE brand_name = :brand
          AND (
            bus_date BETWEEN :current_start AND :current_end
            OR bus_date BETWEEN :prior_start AND :prior_end
          )
          AND (:category IS NULL OR category_CN = :category)
          AND (:key_driver IS NULL OR key_driver = :key_driver)
          AND (:series IS NULL OR product_title LIKE CONCAT('%', :series, '%'))
        GROUP BY period_key, source_month, category_CN, key_driver, item_id
    """
    params = {
        "brand": context["source_brand"],
        **{k: context[k] for k in ("current_start", "current_end", "prior_start", "prior_end")},
        "category": None if isinstance(filters.get("category"), list) else filters.get("category"),
        "key_driver": None if isinstance(filters.get("key_driver"), list) else filters.get("key_driver"),
        "series": None if isinstance(filters.get("series"), list) else filters.get("series"),
    }
    df = fetch_df(sql, params)
    if df.empty:
        return df
    for column in ("category", "key_driver"):
        if isinstance(filters.get(column), list):
            wanted = {str(value).casefold() for value in filters[column]}
            df = df[df[column].fillna("").astype(str).str.casefold().isin(wanted)]
    if isinstance(filters.get("series"), list):
        wanted = [str(value).casefold() for value in filters["series"]]
        df = df[df["product_title"].fillna("").astype(str).str.casefold().map(lambda title: any(value in title for value in wanted))]
    if filters.get("function_tag"):
        words = load_function_tags().get(filters["function_tag"], [])
        if words:
            pattern = "|".join(re.escape(str(word)) for word in words)
            df = df[df["product_title"].fillna("").str.contains(pattern, regex=True)]
    return df


def _dimension_values(df: pd.DataFrame, group_by: list[str], brand: str) -> pd.DataFrame:
    work = df.copy()
    if "month" in group_by:
        work["month"] = work["source_month"]
        # Align prior-year rows to the current-year month label.
        work.loc[work["period_key"] == "prior", "month"] = work.loc[
            work["period_key"] == "prior", "source_month"
        ].map(lambda value: f"{int(value[:4]) + 1:04d}{value[4:]}")
    if "series" in group_by:
        work["series"] = work["product_title"].map(
            lambda title: _infer_series_from_title(title, brand)[0]
        )
    if "sku" in group_by:
        work["sku"] = work.apply(
            lambda row: f"{row['sku']}｜{str(row.get('product_title') or '').strip()}",
            axis=1,
        )
    return work


@tool
def query_ec_followup_table(
    brand: str,
    period: str,
    group_by: list[str] | None = None,
    filters: dict | None = None,
    metrics: list[str] | None = None,
    limit: int = 20,
    brand_aliases: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """Return a whitelisted EC period/month breakdown for follow-up analysis."""
    try:
        group_by = list(group_by or [])
        filters = dict(filters or {})
        _validate(group_by, filters)
        context = ec_query_context(brand, period, brand_aliases=brand_aliases)
        df = _raw_rows(context, filters)
        if df.empty:
            return {"error": "no_data", "message": "指定EC筛选下无数据。"}
        df = _dimension_values(df, group_by, context.get("input_brand", brand))
        dims = group_by or ["_total"]
        if dims == ["_total"]:
            df["_total"] = "TTL"
        grouped = (
            df.groupby(["period_key", *dims], dropna=False)
            .agg(gmv=("gmv", "sum"), unit=("unit", "sum"), row_count=("row_count", "sum"))
            .reset_index()
        )
        current = grouped[grouped["period_key"] == "current"].drop(columns="period_key")
        prior = grouped[grouped["period_key"] == "prior"].drop(columns="period_key")
        merged = current.merge(prior, on=dims, how="outer", suffixes=("_actual", "_prior")).fillna(0)
        total_actual = float(merged["gmv_actual"].sum())
        total_prior = float(merged["gmv_prior"].sum())
        rows = []
        for _, raw in merged.iterrows():
            actual = float(raw["gmv_actual"] or 0)
            prior_value = float(raw["gmv_prior"] or 0)
            unit_actual = float(raw["unit_actual"] or 0)
            unit_prior = float(raw["unit_prior"] or 0)
            row = {dim: ("TTL" if dim == "_total" else str(raw[dim])) for dim in dims}
            row.update({
                "gmv_actual": round(actual),
                "gmv_prior": round(prior_value),
                "gmv_evol": safe_evol(actual, prior_value),
                "gmv_diff": round(actual - prior_value),
                "unit_actual": round(unit_actual),
                "unit_prior": round(unit_prior),
                "unit_evol": safe_evol(unit_actual, unit_prior),
                "atv_actual": round(actual / unit_actual, 2) if unit_actual else None,
                "weight": safe_div(actual, total_actual),
                "weight_prior": safe_div(prior_value, total_prior),
                "weight_change": (
                    safe_div(actual, total_actual) - safe_div(prior_value, total_prior)
                    if total_actual and total_prior else None
                ),
                "_current_rows": int(raw.get("row_count_actual") or 0),
                "_prior_rows": int(raw.get("row_count_prior") or 0),
            })
            rows.append(row)
        sort_metric = "gmv_actual"
        rows.sort(key=lambda row: row.get(sort_metric) or 0, reverse=True)
        requested = month_keys(context["current_start"], context["current_end"])
        present = sorted({str(row.get("month")) for row in rows if row.get("month")})
        if "month" in group_by:
            other_dims = [dim for dim in dims if dim != "month"]
            for month in requested:
                if month not in present:
                    rows.append({"month": month, **{dim: "—" for dim in other_dims}})
            rows.sort(key=lambda row: (str(row.get("month") or ""), -(float(row.get(sort_metric) or 0))))
        rows = rows[:max(1, min(int(limit), 50))]
        if metrics:
            prior_keys = {
                "gmv_actual": "gmv_prior", "unit_actual": "unit_prior",
                "gmv_evol": "gmv_prior", "unit_evol": "unit_prior",
                "atv_actual": "unit_actual",
            }
            keep = set(dims) | set(metrics) | {"gmv_diff", "_current_rows", "_prior_rows"} | {prior_keys[m] for m in metrics if m in prior_keys}
            rows = [{k: v for k, v in row.items() if k in keep} for row in rows]
        return standard_result(
            query_meta={"domain": "ec", "brand": brand, "period": period, "group_by": group_by},
            filters=filters,
            totals={"gmv_actual": round(total_actual), "gmv_prior": round(total_prior), "gmv_evol": safe_evol(total_actual, total_prior)},
            rows=rows,
            coverage={"requested_months": requested, "present_months": present},
            missing=[month for month in requested if group_by and "month" in group_by and month not in present],
        )
    except Exception as exc:
        return {"error": "execution_error", "message": str(exc)}
