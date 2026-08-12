from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import pandas as pd

from bot.db.connection import fetch_df, fetch_one
from bot.media_brand import resolve_source_brand
from bot.tools.common import tool
from bot.tools.market_common import month_slices, monthly_business_date_sql
from bot.utils import extract_json_object, llm_client, parse_ec_period, safe_div, safe_evol


S2_DAILY_TABLE = "dy_store_ranking_BFSS_day_jiashicang"
S2_MONTHLY_TABLE = "three_platform_store_rank_monthly"
S4_PRODUCT_TABLE = "dy_goodssales_rank_day_jiashicang"

CHANNELS = (
    ("kol_live", "KOL直播", "kol_gmv"),
    ("store_live", "品牌自营直播", "storelive_gmv"),
    ("short_other", "短视频及其他", "short_other_gmv"),
)

_SERIES_PATH = Path(__file__).resolve().parents[1] / "data" / "series_map.json"


def _amount_sql(column: str) -> str:
    return (
        "COALESCE(CAST(REPLACE(NULLIF(TRIM(" + column + "), ''), ',', '') "
        "AS DECIMAL(24,4)), 0)"
    )


def _source_slices(start: str, end: str) -> list[dict]:
    """Use monthly rows only for complete natural months; otherwise use daily rows."""
    return [
        {
            **item,
            "source": "monthly" if item["full_month"] else "daily",
            "table": S2_MONTHLY_TABLE if item["full_month"] else S2_DAILY_TABLE,
        }
        for item in month_slices(start, end)
    ]


def _load_known_series(brand_candidates: Iterable[str]) -> list[tuple[str, str]]:
    try:
        config = json.loads(_SERIES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    brand_config = None
    normalized = {str(value or "").strip().lower() for value in brand_candidates if value}
    for key, value in config.items():
        if key.startswith("_"):
            continue
        if key.strip().lower() in normalized:
            brand_config = value
            break
    if not brand_config:
        return []
    matches: list[tuple[str, str]] = []
    for series, item in (brand_config.get("series") or {}).items():
        for keyword in item.get("keywords") or []:
            keyword = str(keyword).strip()
            if keyword:
                matches.append((keyword, str(series).strip()))
    return sorted(matches, key=lambda row: len(row[0]), reverse=True)


def _infer_series_mapping(
    titles: Iterable[str],
    *,
    brand_candidates: Iterable[str],
) -> dict[str, str]:
    """Map titles once, then reuse the same mapping for current and prior periods."""
    unique_titles = sorted({str(title or "").strip() for title in titles if str(title or "").strip()})
    known = _load_known_series(brand_candidates)
    mapping: dict[str, str] = {}
    unmatched: list[str] = []
    for title in unique_titles:
        matched = next((series for keyword, series in known if keyword.lower() in title.lower()), None)
        if matched:
            mapping[title] = matched
        else:
            unmatched.append(title)

    # Product names can be numerous. Small deterministic batches keep the JSON response valid.
    for offset in range(0, len(unmatched), 60):
        batch = unmatched[offset:offset + 60]
        prompt = f"""
你只做商品名称到产品系列的归类，不写营销、人群、功效或策略结论。
根据商品名称中明确出现的系列词，给每个商品归纳一个简短、稳定的产品系列名。
无法可靠判断时填“其他”。不同规格、赠品、直播专属或套装词不应单独成为系列。
只返回JSON：{{"mapping":[{{"product_name":"原商品名称","series":"系列名"}}]}}。
商品名称：
{json.dumps(batch, ensure_ascii=False)}
"""
        try:
            response = llm_client(max_retries=1).chat.completions.create(
                model="qwen-plus-latest",
                messages=[
                    {"role": "system", "content": "你是严格的电商产品系列归类器，只返回JSON。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=3000,
                timeout=30,
                response_format={"type": "json_object"},
                extra_body={"enable_thinking": False},
            )
            parsed = extract_json_object(response.choices[0].message.content or "")
            allowed = set(batch)
            for row in parsed.get("mapping") or []:
                product_name = str(row.get("product_name") or "").strip()
                series = str(row.get("series") or "其他").strip() or "其他"
                if product_name in allowed:
                    mapping[product_name] = series
        except Exception:
            pass
        for title in batch:
            mapping.setdefault(title, "其他")
    return mapping


def _query_s2_categories(
    brand: str,
    period_meta: dict,
) -> tuple[pd.DataFrame, list[dict], list[dict]]:
    frames: list[pd.DataFrame] = []
    sources: list[dict] = []
    missing: list[dict] = []
    for period_key, start_key, end_key in (
        ("current", "current_start", "current_end"),
        ("prior", "prior_start", "prior_end"),
    ):
        for item in _source_slices(period_meta[start_key], period_meta[end_key]):
            table = item["table"]
            platform_clause = "AND UPPER(TRIM(platform)) = 'DY'" if item["source"] == "monthly" else ""
            business_date = (
                monthly_business_date_sql("bus_date")
                if item["source"] == "monthly"
                else "CAST(bus_date AS DATE)"
            )
            sql = f"""
                SELECT
                  :period_key AS period_key,
                  COALESCE(NULLIF(TRIM(category_level_4), ''), '未分类') AS category_level_4,
                  SUM({_amount_sql('gmv')}) AS gmv,
                  COUNT(*) AS row_count
                FROM {table}
                WHERE TRIM(brand_name) = :brand
                  {platform_clause}
                  AND {business_date} BETWEEN :slice_start AND :slice_end
                GROUP BY COALESCE(NULLIF(TRIM(category_level_4), ''), '未分类')
            """
            frame = fetch_df(sql, {
                "period_key": period_key,
                "brand": brand,
                "slice_start": item["start"],
                "slice_end": item["end"],
            })
            source_row = {
                "period": period_key,
                "month": item["month"],
                "source": item["source"],
                "table": table,
                "start": item["start"],
                "end": item["end"],
            }
            sources.append(source_row)
            if frame.empty:
                missing.append(source_row)
            else:
                frames.append(frame)
    if not frames:
        return pd.DataFrame(), sources, missing
    return pd.concat(frames, ignore_index=True), sources, missing


def _query_s2_channels(brand: str, period_meta: dict) -> pd.DataFrame:
    gmv = _amount_sql("gmv")
    kol = _amount_sql("KOL_gmv")
    store = _amount_sql("storelive_gmv")
    return fetch_df(
        f"""
        SELECT
          CASE
            WHEN CAST(bus_date AS DATE) BETWEEN :current_start AND :current_end THEN 'current'
            ELSE 'prior'
          END AS period_key,
          SUM({gmv}) AS brand_gmv,
          SUM({kol}) AS kol_gmv,
          SUM({store}) AS storelive_gmv,
          COUNT(*) AS row_count
        FROM {S2_DAILY_TABLE}
        WHERE TRIM(brand_name) = :brand
          AND (
            CAST(bus_date AS DATE) BETWEEN :current_start AND :current_end
            OR CAST(bus_date AS DATE) BETWEEN :prior_start AND :prior_end
          )
        GROUP BY period_key
        """,
        {"brand": brand, **{key: period_meta[key] for key in (
            "current_start", "current_end", "prior_start", "prior_end"
        )}},
    )


def _query_s4_products(brand: str, period_meta: dict) -> pd.DataFrame:
    sales = _amount_sql("`销售额`")
    kol = _amount_sql("`达人推广直播GMV`")
    store = _amount_sql("`品牌自营直播GMV`")
    return fetch_df(
        f"""
        SELECT
          CASE
            WHEN CAST(`业务日期` AS DATE) BETWEEN :current_start AND :current_end THEN 'current'
            ELSE 'prior'
          END AS period_key,
          CAST(`商品ID` AS CHAR) AS item_id,
          MAX(TRIM(`商品名称`)) AS product_name,
          MAX(TRIM(`商品url`)) AS product_url,
          MAX(COALESCE(NULLIF(TRIM(`商品四级分类`), ''), '未分类')) AS category_level_4,
          SUM({sales}) AS sales_gmv,
          SUM({kol}) AS kol_gmv,
          SUM({store}) AS storelive_gmv,
          COUNT(*) AS row_count
        FROM {S4_PRODUCT_TABLE}
        WHERE TRIM(`商品品牌`) = :brand
          AND (
            CAST(`业务日期` AS DATE) BETWEEN :current_start AND :current_end
            OR CAST(`业务日期` AS DATE) BETWEEN :prior_start AND :prior_end
          )
        GROUP BY period_key, CAST(`商品ID` AS CHAR)
        """,
        {"brand": brand, **{key: period_meta[key] for key in (
            "current_start", "current_end", "prior_start", "prior_end"
        )}},
    )


def _paired_rows(
    df: pd.DataFrame,
    group_cols: list[str],
    value_col: str,
    *,
    parent_current: float,
    parent_prior: float,
) -> list[dict]:
    if df.empty:
        return []
    grouped = df.groupby(["period_key", *group_cols], dropna=False)[value_col].sum().reset_index()
    current = grouped[grouped["period_key"] == "current"].drop(columns="period_key").rename(
        columns={value_col: "gmv_current"}
    )
    prior = grouped[grouped["period_key"] == "prior"].drop(columns="period_key").rename(
        columns={value_col: "gmv_prior"}
    )
    merged = current.merge(prior, on=group_cols, how="outer").fillna({"gmv_current": 0, "gmv_prior": 0})
    rows: list[dict] = []
    for _, row in merged.iterrows():
        current_gmv = float(row["gmv_current"] or 0)
        prior_gmv = float(row["gmv_prior"] or 0)
        weight = safe_div(current_gmv, parent_current)
        prior_weight = safe_div(prior_gmv, parent_prior)
        result = {col: row[col] for col in group_cols}
        result.update({
            "gmv_current": current_gmv,
            "gmv_prior": prior_gmv,
            "gmv_change": current_gmv - prior_gmv,
            "evol": safe_evol(current_gmv, prior_gmv),
            "weight": weight,
            "weight_prior": prior_weight,
            "weight_change": (
                weight - prior_weight if weight is not None and prior_weight is not None else None
            ),
        })
        rows.append(result)
    rows.sort(key=lambda value: value["gmv_current"], reverse=True)
    return rows


def _category_result(frame: pd.DataFrame) -> dict:
    totals = {
        period: float(frame.loc[frame["period_key"] == period, "gmv"].sum())
        for period in ("current", "prior")
    }
    rows = _paired_rows(
        frame.rename(columns={"category_level_4": "category"}),
        ["category"],
        "gmv",
        parent_current=totals["current"],
        parent_prior=totals["prior"],
    )
    return {
        "total": {
            "gmv_current": totals["current"],
            "gmv_prior": totals["prior"],
            "gmv_change": totals["current"] - totals["prior"],
            "evol": safe_evol(totals["current"], totals["prior"]),
        },
        "categories": rows,
        "selected_category": rows[0]["category"] if rows else "",
    }


def _channel_overview(frame: pd.DataFrame) -> dict:
    periods: dict[str, dict] = {}
    for _, row in frame.iterrows():
        key = str(row["period_key"])
        brand_gmv = float(row.get("brand_gmv") or 0)
        kol_gmv = float(row.get("kol_gmv") or 0)
        store_gmv = float(row.get("storelive_gmv") or 0)
        periods[key] = {
            "brand_gmv": brand_gmv,
            "kol_live": kol_gmv,
            "store_live": store_gmv,
            "short_other": brand_gmv - kol_gmv - store_gmv,
        }
    current = periods.get("current", {})
    prior = periods.get("prior", {})
    rows: list[dict] = []
    negative_remainder = False
    for key, label, _ in CHANNELS:
        actual = float(current.get(key) or 0)
        previous = float(prior.get(key) or 0)
        negative_remainder = negative_remainder or (key == "short_other" and (actual < -0.01 or previous < -0.01))
        weight = safe_div(actual, current.get("brand_gmv"))
        prior_weight = safe_div(previous, prior.get("brand_gmv"))
        rows.append({
            "channel_key": key,
            "channel": label,
            "gmv_current": actual,
            "gmv_prior": previous,
            "gmv_change": actual - previous,
            "evol": safe_evol(actual, previous),
            "weight": weight,
            "weight_prior": prior_weight,
            "weight_change": weight - prior_weight if weight is not None and prior_weight is not None else None,
        })
    business_leader = max(rows, key=lambda row: row["gmv_current"]) if rows else None
    growth_leader = max(rows, key=lambda row: row["gmv_change"]) if rows else None
    return {
        "rows": rows,
        "business_leader": business_leader,
        "growth_leader": growth_leader,
        "all_declining": all(row["gmv_change"] < 0 for row in rows),
        "negative_remainder": negative_remainder,
        "denominator": current.get("brand_gmv", 0),
        "denominator_prior": prior.get("brand_gmv", 0),
    }


def _product_analysis(
    frame: pd.DataFrame,
    *,
    selected_category: str,
    brand_candidates: Iterable[str],
) -> dict:
    if frame.empty:
        return {"series": [], "channels": {}, "series_mapping": {}, "reconciliation": {}}
    numeric = ["sales_gmv", "kol_gmv", "storelive_gmv"]
    for column in numeric:
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
    frame["short_other_gmv"] = frame["sales_gmv"] - frame["kol_gmv"] - frame["storelive_gmv"]
    mapping = _infer_series_mapping(frame["product_name"].tolist(), brand_candidates=brand_candidates)
    frame["series"] = frame["product_name"].map(mapping).fillna("其他")

    category_rows = frame[frame["category_level_4"].fillna("").str.strip() == str(selected_category).strip()]
    category_current = float(category_rows.loc[category_rows["period_key"] == "current", "sales_gmv"].sum())
    category_prior = float(category_rows.loc[category_rows["period_key"] == "prior", "sales_gmv"].sum())
    series_rows = _paired_rows(
        category_rows,
        ["series"],
        "sales_gmv",
        parent_current=category_current,
        parent_prior=category_prior,
    )

    channels: dict[str, dict] = {}
    for channel_key, label, value_col in CHANNELS:
        current_total = float(frame.loc[frame["period_key"] == "current", value_col].sum())
        prior_total = float(frame.loc[frame["period_key"] == "prior", value_col].sum())
        channel_series = _paired_rows(
            frame,
            ["series"],
            value_col,
            parent_current=current_total,
            parent_prior=prior_total,
        )
        links = _paired_rows(
            frame,
            ["item_id", "product_name", "product_url", "series"],
            value_col,
            parent_current=current_total,
            parent_prior=prior_total,
        )
        top_links = [row for row in links if row["gmv_current"] > 0][:5]
        channels[channel_key] = {
            "channel": label,
            "gmv_current": current_total,
            "gmv_prior": prior_total,
            "top_series": channel_series[0] if channel_series else None,
            "top_links": top_links,
            "top5_concentration": safe_div(sum(row["gmv_current"] for row in top_links), current_total),
        }

    return {
        "series": series_rows,
        "category_product_total": category_current,
        "category_product_total_prior": category_prior,
        "channels": channels,
        "series_mapping": mapping,
        "negative_short_other": bool((frame["short_other_gmv"] < -0.01).any()),
        "reconciliation": {
            "sales_current": float(frame.loc[frame["period_key"] == "current", "sales_gmv"].sum()),
            "sales_prior": float(frame.loc[frame["period_key"] == "prior", "sales_gmv"].sum()),
        },
    }


@tool
def query_douyin_business(
    brand: str,
    period: str,
    brand_aliases: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """Generate the deterministic data bundle for a Douyin brand business report."""
    try:
        resolved = resolve_source_brand(brand, "dy", brand_aliases=brand_aliases)
        if resolved.get("error"):
            return resolved
        source_brand = str(resolved["brand"])
        latest_s2 = fetch_one(
            f"""
            SELECT MAX(CAST(bus_date AS DATE)) AS max_date
            FROM {S2_DAILY_TABLE}
            WHERE TRIM(brand_name) = :brand
            """,
            {"brand": source_brand},
        ).get("max_date")
        latest_s4 = fetch_one(
            f"""
            SELECT MAX(CAST(`业务日期` AS DATE)) AS max_date
            FROM {S4_PRODUCT_TABLE}
            WHERE TRIM(`商品品牌`) = :brand
            """,
            {"brand": source_brand},
        ).get("max_date")
        latest_values = [str(value)[:10] for value in (latest_s2, latest_s4) if value]
        if not latest_values:
            return {"error": "no_data", "message": f"抖音数据中没有找到品牌“{brand}”。"}
        source_max_date = min(latest_values)
        period_meta = parse_ec_period(period, int(source_max_date[:4]))
        if period_meta["current_end"] > source_max_date:
            return {
                "error": "period_after_data",
                "message": (
                    f"当前品牌抖音数据共同更新至{source_max_date}，"
                    f"你指定的本期结束于{period_meta['current_end']}。"
                ),
            }

        category_frame, sources, missing = _query_s2_categories(source_brand, period_meta)
        if missing:
            detail = "、".join(f"{row['period']} {row['month']}({row['source']})" for row in missing)
            return {"error": "incomplete_coverage", "message": f"品牌/类目数据存在缺口：{detail}。"}
        if category_frame.empty:
            return {"error": "no_data", "message": f"品牌“{source_brand}”在指定期间没有品牌/类目数据。"}

        category_result = _category_result(category_frame)
        channel_frame = _query_s2_channels(source_brand, period_meta)
        product_frame = _query_s4_products(source_brand, period_meta)
        if channel_frame.empty or product_frame.empty:
            return {"error": "no_data", "message": f"品牌“{source_brand}”在指定期间缺少渠道或商品数据。"}
        channel_result = _channel_overview(channel_frame)
        product_result = _product_analysis(
            product_frame,
            selected_category=category_result["selected_category"],
            brand_candidates=[brand, source_brand, *(brand_aliases or [])],
        )
        if category_result["selected_category"] and not product_result["series"]:
            return {
                "error": "category_mapping_mismatch",
                "message": (
                    f"S2中GMV第一的四级类目为“{category_result['selected_category']}”，"
                    "但S4的商品四级分类没有匹配记录，请核对两个类目字段的取值口径。"
                ),
            }
        if channel_result["negative_remainder"] or product_result["negative_short_other"]:
            return {
                "error": "negative_channel_remainder",
                "message": "短视频及其他GMV倒减后出现负数，请先核对渠道字段口径。",
            }
        return {
            "brand": brand,
            "source_brand": source_brand,
            "period": period,
            "period_meta": {**period_meta, "source_max_date": source_max_date},
            "brand_result": category_result["total"],
            "categories": category_result["categories"],
            "selected_category": category_result["selected_category"],
            "product_series": product_result["series"],
            "channel_result": channel_result,
            "channel_products": product_result["channels"],
            "series_mapping": product_result["series_mapping"],
            "reconciliation": {
                "s2_channel_brand_current": channel_result["denominator"],
                "s2_channel_brand_prior": channel_result["denominator_prior"],
                **product_result["reconciliation"],
            },
            "sources": sources + [{
                "period": "current+prior",
                "source": "daily",
                "table": S2_DAILY_TABLE,
                "purpose": "品牌级渠道占比",
            }, {
                "period": "current+prior",
                "source": "daily",
                "table": S4_PRODUCT_TABLE,
                "purpose": "商品、系列、渠道内系列与Top 5链接",
            }],
        }
    except Exception as exc:
        return {"error": "execution_error", "message": str(exc)}
