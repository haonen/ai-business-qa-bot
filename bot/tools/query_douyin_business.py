from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Iterable

import pandas as pd

from bot.db.connection import fetch_df
from bot.media_brand import configured_brand_alias, generate_brand_variants, resolve_source_brand
from bot.platforms import platform_filter_sql
from bot.tools.common import tool
from bot.tools.market_common import (
    month_slices,
    store_rank_business_category_sql,
    store_rank_core_category_sql,
    store_rank_monthly_date_sql,
)
from bot.utils import extract_json_object, llm_client, parse_ec_period, safe_div, safe_evol


S2_DAILY_TABLE = "dy_store_ranking_BFSS_day_jiashicang"
S2_MONTHLY_TABLE = "three_platform_store_rank_monthly"
S4_PRODUCT_TABLE = "ai_bot_dy_product_link"
log = logging.getLogger(__name__)

CHANNELS = (
    ("kol_live", "KOL直播", "kol_gmv"),
    ("store_live", "品牌自营直播", "storelive_gmv"),
    ("short_other", "短视频及其他", "short_other_gmv"),
)
PRODUCT_DRIVERS = ("Store live", "Kol live", "Video", "Product tab")

_SERIES_PATH = Path(__file__).resolve().parents[1] / "data" / "series_map.json"


def _amount_sql(column: str) -> str:
    return (
        "COALESCE(CAST(REPLACE(NULLIF(TRIM(" + column + "), ''), ',', '') "
        "AS DECIMAL(24,4)), 0)"
    )


def _daily_ttl_beauty_sql(column: str = "category_EN_level_1") -> str:
    """Daily-store TTL Beauty scope: Skincare + Makeup + Hair."""
    return store_rank_business_category_sql(
        "TOTAL BEAUTY", column, "category_EN_level_2",
    )


def _enabled(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


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


def _batched_source_slices(
    brand: str,
    period_meta: dict,
    monthly_brand: str | None,
    daily_brand: str | None = None,
) -> dict[str, dict]:
    groups: dict[str, dict] = {}
    for period_key, start_key, end_key in (
        ("current", "current_start", "current_end"),
        ("prior", "prior_start", "prior_end"),
    ):
        for item in _source_slices(period_meta[start_key], period_meta[end_key]):
            source = item["source"]
            group = groups.setdefault(source, {
                "source": source, "table": item["table"], "items": [],
                "brand": (
                    monthly_brand if source == "monthly" and monthly_brand
                    else daily_brand if source == "daily" and daily_brand
                    else brand
                ),
            })
            group["items"].append({**item, "period_key": period_key})
    return groups


def _batch_period_sql(items: list[dict], business_date: str) -> tuple[str, str, str, dict]:
    period_cases = []
    month_cases = []
    predicates = []
    params = {}
    for index, item in enumerate(items):
        start_key, end_key = f"batch_start_{index}", f"batch_end_{index}"
        period_key, month_key = f"batch_period_{index}", f"batch_month_{index}"
        predicate = f"{business_date} BETWEEN :{start_key} AND :{end_key}"
        predicates.append(predicate)
        period_cases.append(f"WHEN {predicate} THEN :{period_key}")
        month_cases.append(f"WHEN {predicate} THEN :{month_key}")
        params.update({
            start_key: item["start"], end_key: item["end"],
            period_key: item["period_key"], month_key: item["month"],
        })
    return (
        "CASE " + " ".join(period_cases) + " END",
        "CASE " + " ".join(month_cases) + " END",
        "(" + " OR ".join(predicates) + ")",
        params,
    )


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

    # Product names can be numerous. The caller passes a GMV-ranked bounded set
    # so an unknown brand cannot trigger dozens of serial model requests.
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
                timeout=float(os.environ.get("DOUYIN_SERIES_LLM_TIMEOUT", "12")),
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
    *,
    monthly_brand: str | None = None,
    daily_brand: str | None = None,
) -> tuple[pd.DataFrame, list[dict], list[dict]]:
    frames: list[pd.DataFrame] = []
    sources: list[dict] = []
    missing: list[dict] = []
    groups = _batched_source_slices(brand, period_meta, monthly_brand, daily_brand)
    for source, group in groups.items():
        table = group["table"]
        query_brand = group["brand"]
        platform_clause = "AND " + platform_filter_sql(S2_MONTHLY_TABLE, "DY") if source == "monthly" else ""
        business_date = store_rank_monthly_date_sql("bus_date") if source == "monthly" else "CAST(bus_date AS DATE)"
        period_case, month_case, range_clause, params = _batch_period_sql(group["items"], business_date)
        # The cross-platform monthly table exposes category_CN, not the
        # category_level_4 column used by the Douyin daily table.  Alias
        # both sources to one internal field only after selecting the
        # source-specific physical column.
        category_column = "category_CN" if source == "monthly" else "category_level_4"
        if source == "monthly":
                # Import batches can repeat the same monthly business row.
                # Deduplicate on the confirmed monthly business key before
                # summing so a brand total cannot be doubled by re-imports.
                sql = f"""
                    SELECT
                      {period_case} AS period_key,
                      {month_case} AS source_month,
                      'monthly' AS source_name,
                      COALESCE(NULLIF(TRIM(category_CN), ''), '未分类') AS category_level_4,
                      SUM(gmv) AS gmv,
                      COUNT(*) AS row_count
                    FROM (
                      SELECT bus_date, clear_category_status, category_CN, category_EN_level_1,
                             category_EN_level_2, store_id, store_CN, brand_name,
                             SELECTIVITY, platform,
                             MAX({_amount_sql('gmv')}) AS gmv
                      FROM {table}
                      WHERE TRIM(brand_name) = :brand
                        {platform_clause}
                        AND {store_rank_core_category_sql()}
                        AND {range_clause}
                      GROUP BY bus_date, clear_category_status, category_CN, category_EN_level_1,
                               category_EN_level_2, store_id, store_CN, brand_name,
                               SELECTIVITY, platform
                    ) monthly_dedup
                    GROUP BY period_key, source_month,
                             COALESCE(NULLIF(TRIM(category_CN), ''), '未分类')
                """
        else:
                sql = f"""
                    SELECT
                      {period_case} AS period_key,
                      {month_case} AS source_month,
                      'daily' AS source_name,
                      COALESCE(NULLIF(TRIM({category_column}), ''), '未分类') AS category_level_4,
                      SUM({_amount_sql('gmv')}) AS gmv,
                      COUNT(*) AS row_count
                    FROM {table}
                    WHERE TRIM(brand_name) = :brand
                      AND {_daily_ttl_beauty_sql()}
                      AND {range_clause}
                    GROUP BY period_key, source_month,
                             COALESCE(NULLIF(TRIM({category_column}), ''), '未分类')
                """
        frame = fetch_df(sql, {"brand": query_brand, **params})
        if not frame.empty:
            frames.append(frame)

    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    present = set()
    if not combined.empty:
        present = {
            (str(row["period_key"]), str(row["source_month"]), str(row["source_name"]))
            for _, row in combined[["period_key", "source_month", "source_name"]].drop_duplicates().iterrows()
        }
    for group in groups.values():
        for item in group["items"]:
            source_row = {
                "period": item["period_key"],
                "month": item["month"],
                "source": group["source"],
                "table": group["table"],
                "start": item["start"],
                "end": item["end"],
            }
            sources.append(source_row)
            if (item["period_key"], item["month"], group["source"]) not in present:
                missing.append(source_row)
    return combined, sources, missing


def _query_s2_channels(
    brand: str,
    period_meta: dict,
    *,
    monthly_brand: str | None = None,
    daily_brand: str | None = None,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for source, group in _batched_source_slices(
        brand, period_meta, monthly_brand, daily_brand
    ).items():
        query_brand = group["brand"]
        business_date = store_rank_monthly_date_sql("bus_date") if source == "monthly" else "CAST(bus_date AS DATE)"
        period_case, month_case, range_clause, params = _batch_period_sql(group["items"], business_date)
        if source == "monthly":
                sql = f"""
                    SELECT {period_case} AS period_key,
                           {month_case} AS source_month,
                           SUM(gmv) AS brand_gmv,
                           SUM(kol_gmv) AS kol_gmv,
                           SUM(storelive_gmv) AS storelive_gmv,
                           COUNT(*) AS row_count
                    FROM (
                      SELECT bus_date, clear_category_status, category_CN, category_EN_level_1,
                             category_EN_level_2, store_id, store_CN, brand_name,
                             SELECTIVITY, platform,
                             MAX({_amount_sql('gmv')}) AS gmv,
                             MAX({_amount_sql('kol_gmv')}) AS kol_gmv,
                             MAX({_amount_sql('storelive_gmv')}) AS storelive_gmv
                      FROM {S2_MONTHLY_TABLE}
                      WHERE TRIM(brand_name) = :brand
                        AND {platform_filter_sql(S2_MONTHLY_TABLE, 'DY')}
                        AND {store_rank_core_category_sql()}
                        AND {range_clause}
                      GROUP BY bus_date, clear_category_status, category_CN, category_EN_level_1,
                               category_EN_level_2, store_id, store_CN, brand_name,
                               SELECTIVITY, platform
                    ) monthly_dedup
                    GROUP BY period_key, source_month
                """
        else:
                sql = f"""
                    SELECT {period_case} AS period_key,
                           {month_case} AS source_month,
                           SUM({_amount_sql('gmv')}) AS brand_gmv,
                           SUM({_amount_sql('KOL_gmv')}) AS kol_gmv,
                           SUM({_amount_sql('storelive_gmv')}) AS storelive_gmv,
                           COUNT(*) AS row_count
                    FROM {S2_DAILY_TABLE}
                    WHERE TRIM(brand_name) = :brand
                      AND {_daily_ttl_beauty_sql()}
                      AND {range_clause}
                    GROUP BY period_key, source_month
                """
        frame = fetch_df(sql, {"brand": query_brand, **params})
        if not frame.empty:
            frames.append(frame)
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True)
    return combined.groupby("period_key", as_index=False)[
        ["brand_gmv", "kol_gmv", "storelive_gmv", "row_count"]
    ].sum()


def _resolve_s2_monthly_brand(
    user_brand: str,
    resolved_brand: str,
    period_meta: dict,
    brand_aliases: list[str] | tuple[str, ...] | None = None,
) -> str:
    """Validate the brand against the monthly fact table's own spelling."""
    configured = configured_brand_alias(user_brand, S2_MONTHLY_TABLE)
    if configured:
        log.info(
            "[douyin_business] using configured monthly brand user_brand=%s monthly_brand=%s",
            user_brand, configured,
        )
        return configured
    candidates = list(dict.fromkeys(
        cleaned
        for value in (
            resolved_brand,
            user_brand,
            *(brand_aliases or ()),
            *generate_brand_variants(user_brand),
        )
        if (cleaned := str(value or "").strip())
    ))
    if not candidates:
        return resolved_brand
    params = {f"brand_{index}": value for index, value in enumerate(candidates)}
    placeholders = ", ".join(f":brand_{index}" for index in range(len(candidates)))
    params.update({
        "current_start": period_meta["current_start"],
        "current_end": period_meta["current_end"],
        "prior_start": period_meta["prior_start"],
        "prior_end": period_meta["prior_end"],
    })
    try:
        frame = fetch_df(
            f"""
            SELECT DISTINCT TRIM(brand_name) AS source_brand
            FROM {S2_MONTHLY_TABLE}
            WHERE TRIM(brand_name) IN ({placeholders})
              AND {platform_filter_sql(S2_MONTHLY_TABLE, 'DY')}
              AND {store_rank_core_category_sql()}
              AND (
                {store_rank_monthly_date_sql('bus_date')} BETWEEN :current_start AND :current_end
                OR {store_rank_monthly_date_sql('bus_date')} BETWEEN :prior_start AND :prior_end
              )
            ORDER BY source_brand
            """,
            params,
        )
    except Exception:
        log.exception("[douyin_business] monthly brand validation failed")
        return resolved_brand
    if frame.empty or "source_brand" not in frame.columns:
        log.warning(
            "[douyin_business] monthly brand not found user_brand=%s resolved_brand=%s candidates=%s",
            user_brand, resolved_brand, candidates,
        )
        return resolved_brand
    matches = [str(value).strip() for value in frame["source_brand"].dropna() if str(value).strip()]
    if resolved_brand in matches:
        return resolved_brand
    if len(matches) == 1:
        log.info(
            "[douyin_business] monthly brand remapped user_brand=%s resolved_brand=%s monthly_brand=%s",
            user_brand, resolved_brand, matches[0],
        )
        return matches[0]
    log.warning(
        "[douyin_business] monthly brand ambiguous user_brand=%s resolved_brand=%s matches=%s",
        user_brand, resolved_brand, matches,
    )
    return resolved_brand


def _resolve_s2_daily_brand(
    user_brand: str,
    resolved_brand: str,
    period_meta: dict,
    brand_aliases: list[str] | tuple[str, ...] | None = None,
) -> str:
    """Resolve the dedicated daily store table's exact brand spelling."""
    configured = configured_brand_alias(user_brand, S2_DAILY_TABLE)
    if configured:
        return configured
    candidates = list(dict.fromkeys(
        cleaned
        for value in (
            resolved_brand,
            user_brand,
            *(brand_aliases or ()),
            *generate_brand_variants(user_brand),
        )
        if (cleaned := str(value or "").strip())
    ))
    if not candidates:
        return resolved_brand
    params = {f"brand_{index}": value for index, value in enumerate(candidates)}
    placeholders = ", ".join(f":brand_{index}" for index in range(len(candidates)))
    params.update({
        "current_start": period_meta["current_start"],
        "current_end": period_meta["current_end"],
        "prior_start": period_meta["prior_start"],
        "prior_end": period_meta["prior_end"],
    })
    try:
        frame = fetch_df(
            f"""
            SELECT DISTINCT TRIM(brand_name) AS source_brand
            FROM {S2_DAILY_TABLE}
            WHERE TRIM(brand_name) IN ({placeholders})
              AND {_daily_ttl_beauty_sql()}
              AND (
                CAST(bus_date AS DATE) BETWEEN :current_start AND :current_end
                OR CAST(bus_date AS DATE) BETWEEN :prior_start AND :prior_end
              )
            ORDER BY source_brand
            """,
            params,
        )
    except Exception:
        log.exception("[douyin_business] daily brand validation failed")
        return resolved_brand
    if frame.empty or "source_brand" not in frame.columns:
        return resolved_brand
    matches = [str(value).strip() for value in frame["source_brand"].dropna() if str(value).strip()]
    if resolved_brand in matches:
        return resolved_brand
    if len(matches) == 1:
        log.info(
            "[douyin_business] daily brand remapped user_brand=%s resolved_brand=%s daily_brand=%s",
            user_brand, resolved_brand, matches[0],
        )
        return matches[0]
    return resolved_brand


def _query_s4_products(brand: str, period_meta: dict) -> pd.DataFrame:
    # ai_bot_dy_product_link already owns key_driver. Its 销售额 remains
    # VARCHAR, so normalize it only in the SELECT aggregate; brand and DATE
    # predicates stay function-free and can use idx_dy_brand_date.
    sales = _amount_sql("`销售额`")
    date_params = {
        key: str(period_meta[key])
        for key in ("current_start", "current_end", "prior_start", "prior_end")
    }
    return fetch_df(
        f"""
        SELECT
          CASE
            WHEN `业务日期` BETWEEN :current_start AND :current_end THEN 'current'
            ELSE 'prior'
          END AS period_key,
          CAST(`商品ID` AS CHAR) AS item_id,
          TRIM(`商品名称`) AS product_name,
          NULL AS product_url,
          COALESCE(NULLIF(TRIM(`商品四级分类`), ''), '未分类') AS category_level_4,
          `key_driver`,
          SUM({sales}) AS sales_gmv,
          0 AS kol_gmv,
          0 AS storelive_gmv,
          0 AS invalid_numeric_rows,
          COUNT(*) AS row_count
        FROM {S4_PRODUCT_TABLE}
        WHERE `商品品牌` = :brand
          AND (
            `业务日期` BETWEEN :current_start AND :current_end
            OR `业务日期` BETWEEN :prior_start AND :prior_end
          )
        GROUP BY period_key, CAST(`商品ID` AS CHAR),
                 COALESCE(NULLIF(TRIM(`商品四级分类`), ''), '未分类'),
                 `key_driver`, TRIM(`商品名称`)
        """,
        {"brand": brand, **date_params},
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
    brand_candidates: Iterable[str],
) -> dict:
    if frame.empty:
        return {"series": [], "channels": {}, "series_mapping": {}, "reconciliation": {}}
    numeric = ["sales_gmv", "kol_gmv", "storelive_gmv"]
    for column in numeric:
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
    frame["short_other_gmv"] = frame["sales_gmv"] - frame["kol_gmv"] - frame["storelive_gmv"]
    # Series labeling is descriptive enrichment only. Rank unique names by
    # complete-period GMV and bound model work to one batch by default; all
    # rows still participate in GMV, channel and Top-link calculations.
    max_titles = max(0, int(os.environ.get("DOUYIN_SERIES_MAX_TITLES", "60")))
    ranked_titles = (
        frame.assign(product_name=frame["product_name"].fillna("").astype(str).str.strip())
        .groupby("product_name", as_index=False)["sales_gmv"].sum()
        .sort_values("sales_gmv", ascending=False)["product_name"]
    )
    ranked_titles = [title for title in ranked_titles.tolist() if title][:max_titles]
    mapping = _infer_series_mapping(ranked_titles, brand_candidates=brand_candidates)
    frame["series"] = frame["product_name"].map(mapping).fillna("其他")

    brand_current = float(frame.loc[frame["period_key"] == "current", "sales_gmv"].sum())
    brand_prior = float(frame.loc[frame["period_key"] == "prior", "sales_gmv"].sum())
    series_rows = _paired_rows(
        frame,
        ["series"],
        "sales_gmv",
        parent_current=brand_current,
        parent_prior=brand_prior,
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
        "brand_product_total": brand_current,
        "brand_product_total_prior": brand_prior,
        "channels": channels,
        "series_mapping": mapping,
        "negative_short_other": bool((frame["short_other_gmv"] < -0.01).any()),
        "reconciliation": {
            "sales_current": float(frame.loc[frame["period_key"] == "current", "sales_gmv"].sum()),
            "sales_prior": float(frame.loc[frame["period_key"] == "prior", "sales_gmv"].sum()),
        },
    }


def _product_analysis_v2(
    frame: pd.DataFrame,
    *,
    brand_candidates: Iterable[str],
    store_total: dict,
) -> dict:
    """Build category, series and driver evidence from one product-table result."""
    if frame.empty:
        return {
            "quality": {"passed": False, "reason": "no_data"},
            "product_total": {}, "categories": [], "selected_category": "",
            "selected_category_series": [], "key_drivers": [],
            "driver_drilldowns": [], "reconciliation": {},
        }
    frame = frame.copy()
    for column in ("sales_gmv", "row_count", "invalid_numeric_rows"):
        if column not in frame.columns:
            frame[column] = 0
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
    if "key_driver" not in frame.columns:
        frame["key_driver"] = None
    null_driver = int(frame.loc[frame["key_driver"].isna(), "row_count"].sum())
    invalid_numeric = int(frame["invalid_numeric_rows"].sum())
    quality = {
        "passed": null_driver == 0 and invalid_numeric == 0,
        "null_driver_rows": null_driver,
        "invalid_numeric_rows": invalid_numeric,
    }

    valid = frame[
        frame["key_driver"].isin(PRODUCT_DRIVERS) & (frame["invalid_numeric_rows"] == 0)
    ].copy()
    product_current = float(valid.loc[valid["period_key"] == "current", "sales_gmv"].sum())
    product_prior = float(valid.loc[valid["period_key"] == "prior", "sales_gmv"].sum())
    product_total = {
        "gmv_current": product_current,
        "gmv_prior": product_prior,
        "gmv_change": product_current - product_prior,
        "evol": safe_evol(product_current, product_prior),
    }

    category_frame = valid.rename(columns={"category_level_4": "category"})
    categories = _paired_rows(
        category_frame, ["category"], "sales_gmv",
        parent_current=product_current, parent_prior=product_prior,
    )
    # “未分类” is a source-data quality bucket, not a business category. Keep
    # it in the reconciliation table, but never let it win a category ranking
    # or feed a category/driver drilldown and its title interpretation.
    classified_frame = category_frame[
        category_frame["category"].fillna("").astype(str).str.strip().ne("未分类")
    ].copy()
    analysis_categories = [
        row for row in categories if str(row.get("category") or "").strip() != "未分类"
    ]
    unclassified_frame = category_frame[
        category_frame["category"].fillna("").astype(str).str.strip().eq("未分类")
    ]
    unclassified = {
        "gmv_current": float(unclassified_frame.loc[
            unclassified_frame["period_key"] == "current", "sales_gmv"
        ].sum()),
        "gmv_prior": float(unclassified_frame.loc[
            unclassified_frame["period_key"] == "prior", "sales_gmv"
        ].sum()),
        "row_count": int(unclassified_frame["row_count"].sum()),
    }
    eligible = [
        row for row in analysis_categories if float(row.get("weight") or 0) >= 0.03
    ]
    selected_row = max(
        eligible,
        key=lambda row: (float(row.get("gmv_change") or 0), float(row.get("gmv_current") or 0)),
        default=None,
    )
    selected_category = str((selected_row or {}).get("category") or "")

    selected_series: list[dict] = []
    selected_category_top_links: list[dict] = []
    series_mapping: dict[str, str] = {}
    max_titles = max(0, int(os.environ.get("DOUYIN_SERIES_MAX_TITLES", "60")))
    if not classified_frame.empty:
        all_titles = (
            classified_frame.assign(
                product_name=classified_frame["product_name"].fillna("").astype(str).str.strip()
            )
            .groupby("product_name", as_index=False)["sales_gmv"].sum()
            .sort_values("sales_gmv", ascending=False)["product_name"]
        )
        ranked_titles = [title for title in all_titles.tolist() if title][:max_titles]
        series_mapping = _infer_series_mapping(ranked_titles, brand_candidates=brand_candidates)
        classified_frame["series"] = classified_frame["product_name"].map(
            series_mapping
        ).fillna("其他")
    if selected_category:
        selected = classified_frame[classified_frame["category"] == selected_category].copy()
        category_current = float(selected.loc[selected["period_key"] == "current", "sales_gmv"].sum())
        category_prior = float(selected.loc[selected["period_key"] == "prior", "sales_gmv"].sum())
        selected_series = _paired_rows(
            selected, ["series"], "sales_gmv",
            parent_current=category_current, parent_prior=category_prior,
        )[:5]
        selected_links = _paired_rows(
            selected,
            ["item_id", "product_name", "product_url", "category", "series"],
            "sales_gmv",
            parent_current=category_current,
            parent_prior=category_prior,
        )
        selected_category_top_links = [
            row for row in selected_links if row["gmv_current"] > 0
        ][:5]

    paired_drivers = _paired_rows(
        valid, ["key_driver"], "sales_gmv",
        parent_current=product_current, parent_prior=product_prior,
    )
    driver_lookup = {str(row["key_driver"]): row for row in paired_drivers}
    key_drivers: list[dict] = []
    driver_drilldowns: list[dict] = []
    for driver in PRODUCT_DRIVERS:
        driver_row = driver_lookup.get(driver, {
            "key_driver": driver, "gmv_current": 0.0, "gmv_prior": 0.0,
            "gmv_change": 0.0, "evol": None, "weight": 0.0,
            "weight_prior": 0.0, "weight_change": 0.0,
        })
        key_drivers.append(driver_row)
        driver_frame = classified_frame[classified_frame["key_driver"] == driver].copy()
        driver_current = float(driver_row.get("gmv_current") or 0)
        driver_prior = float(driver_row.get("gmv_prior") or 0)
        driver_categories = _paired_rows(
            driver_frame, ["category"], "sales_gmv",
            parent_current=driver_current, parent_prior=driver_prior,
        )
        driver_series = _paired_rows(
            driver_frame, ["series"], "sales_gmv",
            parent_current=driver_current, parent_prior=driver_prior,
        )[:5]
        links = _paired_rows(
            driver_frame,
            ["item_id", "product_name", "product_url", "category", "series"],
            "sales_gmv", parent_current=driver_current, parent_prior=driver_prior,
        )
        driver_drilldowns.append({
            "key_driver": driver,
            "top_category": driver_categories[0] if driver_categories else None,
            "series": driver_series,
            "top_links": [row for row in links if row["gmv_current"] > 0][:5],
        })

    store_current = float(store_total.get("gmv_current") or 0)
    store_prior = float(store_total.get("gmv_prior") or 0)
    return {
        "quality": quality,
        "product_total": product_total,
        "categories": categories,
        "analysis_categories": analysis_categories,
        "unclassified": unclassified,
        "selected_category": selected_category,
        "selected_category_series": selected_series,
        "selected_category_top_links": selected_category_top_links,
        "series_mapping": series_mapping,
        "key_drivers": key_drivers,
        "driver_drilldowns": driver_drilldowns,
        "reconciliation": {
            "store_gmv_current": store_current,
            "store_gmv_prior": store_prior,
            "product_gmv_current": product_current,
            "product_gmv_prior": product_prior,
            "coverage_current": safe_div(product_current, store_current),
            "coverage_prior": safe_div(product_prior, store_prior),
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
        monthly_brand = source_brand
        daily_brand = source_brand
        # Parse first, then query the requested source directly.  A complete
        # natural-month report must not probe either daily table merely to
        # discover a common max date; monthly coverage below is authoritative.
        period_meta = parse_ec_period(period, date.today().year)
        product_v2_enabled = _enabled("DOUYIN_PRODUCT_DAILY_V2_ENABLED")
        product_v2_shadow = _enabled("DOUYIN_PRODUCT_DAILY_V2_SHADOW")

        category_frame, sources, missing = _query_s2_categories(source_brand, period_meta)
        if missing:
            log.info(
                "[douyin_business] retrying missing store coverage brand=%s period=%s missing=%s",
                source_brand, period, missing,
            )
            if any(row["source"] == "monthly" for row in missing):
                monthly_brand = _resolve_s2_monthly_brand(
                    brand, source_brand, period_meta, brand_aliases
                )
            if any(row["source"] == "daily" for row in missing):
                daily_brand = _resolve_s2_daily_brand(
                    brand, source_brand, period_meta, brand_aliases
                )
            if monthly_brand != source_brand or daily_brand != source_brand:
                category_frame, sources, missing = _query_s2_categories(
                    source_brand,
                    period_meta,
                    monthly_brand=monthly_brand,
                    daily_brand=daily_brand,
                )
        store_missing_detail = "、".join(
            f"{row['period']} {row['month']}({row['source']})" for row in missing
        )
        category_result = _category_result(category_frame) if not category_frame.empty else {
            "total": {"gmv_current": 0.0, "gmv_prior": 0.0, "gmv_change": 0.0, "evol": None},
            "categories": [], "selected_category": "",
        }
        limitations: list[str] = []
        if missing:
            limitations.append(
                f"店铺表数据存在缺口（{store_missing_detail}），"
                "本次不展示店铺表整体GMV及商品覆盖率。"
            )
        elif category_frame.empty:
            limitations.append("店铺表在指定期间无数据，本次不展示店铺表整体GMV。")
        product_analysis_v2: dict = {}
        if product_v2_enabled or product_v2_shadow:
            try:
                product_frame = _query_s4_products(source_brand, period_meta)
                product_analysis_v2 = _product_analysis_v2(
                    product_frame,
                    brand_candidates=(brand, source_brand, *(brand_aliases or [])),
                    store_total=category_result["total"] if not missing else {},
                )
                log.info(
                    "[douyin_product_daily_v2] brand=%s period=%s enabled=%s shadow=%s "
                    "quality=%s product_total=%s categories=%s drivers=%s",
                    source_brand, period, product_v2_enabled, product_v2_shadow,
                    product_analysis_v2.get("quality"),
                    product_analysis_v2.get("product_total"),
                    len(product_analysis_v2.get("categories") or []),
                    len(product_analysis_v2.get("key_drivers") or []),
                )
            except Exception as exc:
                log.exception("[douyin_product_daily_v2] query or analysis failed")
                if product_v2_enabled:
                    return {
                        "error": "product_daily_not_ready",
                        "message": (
                            "抖音商品表ai_bot_dy_product_link查询失败或数据库连接中断。"
                            "为避免使用错误口径，本次未生成品类和Key Driver结论。"
                        ),
                        "diagnostic": type(exc).__name__,
                    }

        if product_v2_enabled:
            quality = product_analysis_v2.get("quality") or {}
            if quality.get("reason") == "no_data":
                return {
                    "error": "product_daily_no_data",
                    "message": (
                        f"品牌“{source_brand}”在指定期间的店铺GMV有数据，但商品日表没有数据。"
                        "本次不会用店铺表冒充品类或Key Driver分析。"
                    ),
                }
            if _enabled("DOUYIN_PRODUCT_DAILY_QUALITY_GATE", "1") and not quality.get("passed"):
                return {
                    "error": "product_daily_quality_blocked",
                    "message": (
                        "抖音商品日表质量检查未通过，已阻断品类和Key Driver分析。"
                        f"空标签={quality.get('null_driver_rows', 0)}，"
                        f"无法转数值={quality.get('invalid_numeric_rows', 0)}。"
                    ),
                    "quality": quality,
                }
            return {
                "brand": brand,
                "source_brand": source_brand,
                "monthly_source_brand": monthly_brand,
                "daily_source_brand": daily_brand,
                "period": period,
                "period_meta": period_meta,
                "brand_result": category_result["total"] if not missing else {},
                "categories": product_analysis_v2.get("categories") or [],
                "selected_category": product_analysis_v2.get("selected_category") or "",
                "product_analysis": product_analysis_v2,
                "channel_result": {},
                "reconciliation": product_analysis_v2.get("reconciliation") or {},
                "limitations": limitations,
                "sources": [
                    *sources,
                    {
                        "period": "current+prior", "source": "daily",
                        "table": S4_PRODUCT_TABLE,
                        "purpose": "品类、系列、Key Driver与商品链接",
                    },
                ],
            }

        # V1 keeps strict store-table coverage. Shadow has already executed,
        # so a V1 gap no longer prevents collecting V2 rollout evidence.
        if missing:
            return {
                "error": "incomplete_coverage",
                "message": f"品牌/类目数据存在缺口：{store_missing_detail}。",
            }
        if category_frame.empty:
            return {
                "error": "no_data",
                "message": f"品牌“{source_brand}”在指定期间没有品牌/类目数据。",
            }

        # Brand-level channel metrics use the same S2 monthly/daily source
        # selection as the core report. Douyin product-detail tables are
        # intentionally outside this report's scope.
        channel_result: dict = {}
        try:
            channel_frame = _query_s2_channels(
                source_brand, period_meta, monthly_brand=monthly_brand
            )
            if channel_frame.empty:
                limitations.append("渠道日表在指定期间无数据，未展示渠道拆分。")
            else:
                channel_result = _channel_overview(channel_frame)
                if channel_result["negative_remainder"]:
                    limitations.append("渠道倒减结果出现负数，未展示渠道拆分。")
                    channel_result = {}
        except Exception:
            log.exception("[douyin_business] channel enrichment failed")
            limitations.append("渠道日表查询超时或连接中断，未展示渠道拆分。")

        reconciliation: dict = {}
        if channel_result:
            reconciliation.update({
                "s2_channel_brand_current": channel_result["denominator"],
                "s2_channel_brand_prior": channel_result["denominator_prior"],
            })
        report_sources = list(sources)
        report_sources.append({
            "period": "current+prior",
            "source": "monthly/daily by natural-month completeness",
            "table": S2_MONTHLY_TABLE,
            "purpose": "品牌级渠道占比",
        })
        return {
            "brand": brand,
            "source_brand": source_brand,
            "monthly_source_brand": monthly_brand,
            "period": period,
            "period_meta": period_meta,
            "brand_result": category_result["total"],
            "categories": category_result["categories"],
            "selected_category": category_result["selected_category"],
            "channel_result": channel_result,
            "reconciliation": reconciliation,
            "limitations": limitations,
            "sources": report_sources,
        }
    except Exception:
        log.exception("[douyin_business] query failed brand=%s period=%s", brand, period)
        return {
            "error": "execution_error",
            "message": "抖音数据查询超时或数据库连接中断，请稍后重试。系统未生成可能失真的报告。",
        }
