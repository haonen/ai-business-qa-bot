from __future__ import annotations

from collections import defaultdict

import pandas as pd

from bot.db.connection import fetch_df
from bot.media_brand import resolve_source_brand
from bot.tools.common import tool
from bot.tools.query_market_top_brands import query_market_top_brands
from bot.utils import parse_ec_period, safe_div, safe_evol


PLATFORMS = ("TM", "DY", "JD")
EVENT_WINDOWS = (
    ("214情人节", (2,)),
    ("38大促", (3,)),
    ("520", (5,)),
    # 月度数据无法精确切分各平台预售期，因此把5—6月标为618观察窗口，
    # 不把节点本身写成增长原因。
    ("618观察窗口", (5, 6)),
    ("双11", (10, 11)),
)


def _select_representative_brands(pool: list[dict], limit: int = 3) -> list[dict]:
    """Select scale, absolute-growth and growth-rate leaders, then fill by growth."""
    valid = [row for row in pool if float(row.get("gmv_actual") or 0) > 0]
    if not valid:
        return []
    leaders = (
        ("规模领先", max(valid, key=lambda row: float(row.get("gmv_actual") or 0))),
        ("增长额领先", max(valid, key=lambda row: float(row.get("gmv_growth") or 0))),
        ("增速领先", max(valid, key=lambda row: float(row.get("evol") or -999))),
    )
    selected: list[dict] = []
    for reason, row in leaders:
        existing = next((item for item in selected if item["brand"] == row["brand"]), None)
        if existing:
            existing["selection_reasons"].append(reason)
        elif len(selected) < limit:
            selected.append({**row, "selection_reasons": [reason]})
    for row in sorted(valid, key=lambda item: float(item.get("gmv_growth") or 0), reverse=True):
        if len(selected) >= limit:
            break
        if not any(item["brand"] == row["brand"] for item in selected):
            selected.append({**row, "selection_reasons": ["综合增长代表"]})
    return selected


def _platform_and_month_analysis(coverage: list[dict], brands: list[str]) -> dict[str, dict]:
    wanted = set(brands)
    values: dict[tuple[str, str, str], float] = defaultdict(float)
    months: dict[tuple[str, str, str], float] = defaultdict(float)
    for row in coverage:
        brand = str(row.get("brand") or "")
        if brand not in wanted:
            continue
        period = str(row.get("period_key") or "")
        platform = str(row.get("platform") or "").upper()
        month = str(row.get("month") or "")
        gmv = float(row.get("gmv") or 0)
        values[(brand, period, platform)] += gmv
        months[(brand, period, month + "|" + platform)] += gmv

    output: dict[str, dict] = {}
    for brand in brands:
        current_ttl = sum(values[(brand, "current", p)] for p in PLATFORMS)
        prior_ttl = sum(values[(brand, "prior", p)] for p in PLATFORMS)
        platform_rows = []
        for platform in PLATFORMS:
            current = values[(brand, "current", platform)]
            prior = values[(brand, "prior", platform)]
            platform_rows.append({
                "platform": platform,
                "gmv_actual": current,
                "gmv_prior": prior,
                "evol": safe_evol(current, prior),
                "gmv_growth": current - prior,
                "weight": safe_div(current, current_ttl),
                "growth_contribution": safe_div(current - prior, current_ttl - prior_ttl),
            })
        current_months = sorted({key[2].split("|", 1)[0] for key in months if key[0] == brand and key[1] == "current"})
        monthly_rows = []
        previous_current = None
        for month in current_months:
            prior_month = f"{int(month[:4]) - 1:04d}{month[4:]}"
            current = sum(months[(brand, "current", month + "|" + p)] for p in PLATFORMS)
            prior = sum(months[(brand, "prior", prior_month + "|" + p)] for p in PLATFORMS)
            platform_values = {
                p: months[(brand, "current", month + "|" + p)] for p in PLATFORMS
            }
            monthly_rows.append({
                "month": month,
                "gmv_actual": current,
                "gmv_prior": prior,
                "evol": safe_evol(current, prior),
                "mom": safe_evol(current, previous_current) if previous_current is not None else None,
                "gmv_growth": current - prior,
                "leading_platform": max(platform_values, key=platform_values.get),
                "event_context": [label for label, event_months in EVENT_WINDOWS if int(month[5:7]) in event_months],
            })
            previous_current = current
        output[brand] = {
            "platforms": platform_rows,
            "months": monthly_rows,
            "peak_month": max(monthly_rows, key=lambda row: row["gmv_actual"]) if monthly_rows else None,
            "growth_month": max(monthly_rows, key=lambda row: row["gmv_growth"]) if monthly_rows else None,
        }
    return output


def _event_labels(period_meta: dict) -> list[str]:
    start = pd.Timestamp(period_meta["current_start"])
    end = pd.Timestamp(period_meta["current_end"])
    labels = []
    for label, months in EVENT_WINDOWS:
        if any(
            start.to_period("M") <= pd.Timestamp(year=start.year, month=month, day=1).to_period("M") <= end.to_period("M")
            for month in months
        ):
            labels.append(label)
    return labels


def _resolve_product_brand(brand: str, source: str) -> str | None:
    result = resolve_source_brand(brand, source, brand_aliases=[brand])
    return None if result.get("error") else str(result.get("brand") or "").strip() or None


def _query_tmall_products(brand: str, period_meta: dict) -> pd.DataFrame:
    source_brand = _resolve_product_brand(brand, "tmall")
    if not source_brand:
        return pd.DataFrame()
    return fetch_df(
        """
        SELECT
          CASE WHEN CAST(bus_date AS DATE) BETWEEN :current_start AND :current_end
               THEN 'current' ELSE 'prior' END AS period_key,
          DATE_FORMAT(CAST(bus_date AS DATE), '%Y-%m') AS source_month,
          CAST(item_id AS CHAR) AS item_id,
          MAX(product_title) AS product_name,
          SUM(gmv) AS gmv,
          SUM(unit) AS unit
        FROM ai_bot_tmall_product_link FORCE INDEX (idx_tmall_brand_date)
        WHERE brand_name = :brand
          AND (CAST(bus_date AS DATE) BETWEEN :current_start AND :current_end
               OR CAST(bus_date AS DATE) BETWEEN :prior_start AND :prior_end)
        GROUP BY period_key, source_month, CAST(item_id AS CHAR)
        """,
        {"brand": source_brand, **{key: period_meta[key] for key in (
            "current_start", "current_end", "prior_start", "prior_end"
        )}},
    )


def _query_douyin_products(brand: str, period_meta: dict) -> pd.DataFrame:
    source_brand = _resolve_product_brand(brand, "dy")
    if not source_brand:
        return pd.DataFrame()
    return fetch_df(
        """
        SELECT
          CASE WHEN CAST(`业务日期` AS DATE) BETWEEN :current_start AND :current_end
               THEN 'current' ELSE 'prior' END AS period_key,
          DATE_FORMAT(CAST(`业务日期` AS DATE), '%Y-%m') AS source_month,
          CAST(`商品ID` AS CHAR) AS item_id,
          MAX(`商品名称`) AS product_name,
          SUM(`销售额`) AS gmv,
          NULL AS unit
        FROM ai_bot_dy_product_link
        WHERE `商品品牌` = :brand
          AND (CAST(`业务日期` AS DATE) BETWEEN :current_start AND :current_end
               OR CAST(`业务日期` AS DATE) BETWEEN :prior_start AND :prior_end)
        GROUP BY period_key, source_month, CAST(`商品ID` AS CHAR)
        """,
        {"brand": source_brand, **{key: period_meta[key] for key in (
            "current_start", "current_end", "prior_start", "prior_end"
        )}},
    )


def _product_summary(frame: pd.DataFrame, platform: str) -> dict:
    if frame.empty:
        return {"platform": platform, "status": "no_data", "products": []}
    work = frame.copy()
    for column in ("gmv", "unit"):
        work[column] = pd.to_numeric(work[column], errors="coerce").fillna(0.0)
    current = work[work["period_key"] == "current"].groupby(
        ["item_id", "product_name"], dropna=False
    )[["gmv", "unit"]].sum().reset_index().rename(columns={"gmv": "gmv_actual", "unit": "unit_actual"})
    prior = work[work["period_key"] == "prior"].groupby(
        ["item_id", "product_name"], dropna=False
    )[["gmv", "unit"]].sum().reset_index().rename(columns={"gmv": "gmv_prior", "unit": "unit_prior"})
    merged = current.merge(prior, on=["item_id", "product_name"], how="outer").fillna(0)
    rows = []
    for _, row in merged.iterrows():
        actual = float(row["gmv_actual"] or 0)
        prior_value = float(row["gmv_prior"] or 0)
        unit = float(row["unit_actual"] or 0)
        monthly = work[(work["period_key"] == "current") & (work["item_id"].astype(str) == str(row["item_id"]))]
        proxies = [float(raw["gmv"]) / float(raw["unit"]) for _, raw in monthly.iterrows() if float(raw["unit"] or 0) > 0]
        rows.append({
            "item_id": str(row["item_id"]),
            "product_name": str(row["product_name"] or ""),
            "gmv_actual": actual,
            "gmv_prior": prior_value,
            "gmv_growth": actual - prior_value,
            "evol": safe_evol(actual, prior_value),
            "paid_unit_value_proxy": actual / unit if unit else None,
            "proxy_month_min": min(proxies) if proxies else None,
            "proxy_month_max": max(proxies) if proxies else None,
        })
    rows.sort(key=lambda row: row["gmv_growth"], reverse=True)
    return {"platform": platform, "status": "ok", "products": rows[:5]}


@tool
def query_market_brand_deep_dive(
    period: str,
    segment: str = "PURE MASS",
    brand_limit: int = 3,
    platform: str = "TTL",
    ranking_metric: str = "gmv_growth",
) -> dict:
    """Select representative Mass brands and analyze platforms, rhythm and products."""
    try:
        ranking = query_market_top_brands(
            period=period, segment=segment, platform=platform,
            ranking_metric=ranking_metric, limit=max(brand_limit, 20)
        )
        if ranking.get("error"):
            return ranking
        if ranking_metric == "gmv_actual":
            selected = [
                {**row, "selection_reasons": ["本期GMV规模Top"]}
                for row in (ranking.get("rows") or [])[:brand_limit]
            ]
        else:
            selected = _select_representative_brands(ranking.get("candidate_pool") or [], brand_limit)
        if len(selected) < 2:
            return {"error": "insufficient_representative_brands", "message": "该期间可用的Mass品牌不足2个，无法生成品牌深度分析。"}
        brands = [row["brand"] for row in selected]
        breakdown = _platform_and_month_analysis(ranking.get("coverage") or [], brands)
        parsed = parse_ec_period(period, 2026)
        for selected_row in selected:
            brand = selected_row["brand"]
            selected_row.update(breakdown.get(brand) or {})
            products = []
            for platform, loader in (("TM", _query_tmall_products), ("DY", _query_douyin_products)):
                try:
                    products.append(_product_summary(loader(brand, parsed), platform))
                except Exception as exc:
                    products.append({"platform": platform, "status": "query_error", "message": str(exc), "products": []})
            products.append({"platform": "JD", "status": "no_product_table", "products": []})
            selected_row["product_evidence"] = products
        return {
            "query_meta": {
                "tool": "query_market_brand_deep_dive",
                "segment": segment,
                "platform": platform,
                "ranking_metric": ranking_metric,
                "category": "Total Beauty",
                "current_period": [parsed["current_start"], parsed["current_end"]],
                "prior_period": [parsed["prior_start"], parsed["prior_end"]],
            },
            "brands": selected,
            "event_context": _event_labels(parsed),
            "coverage": ranking.get("coverage") or [],
            "limitations": [
                "当前商品表没有标价、券后价或促销标记；GMV/销量只是成交均价代理，不是商品标价。",
                "京东当前没有商品级表，不输出京东选品和价格结论。",
                "天猫/抖音商品链接数据与ECIP品牌GMV口径可能不同，商品仅用作增长证据，不强制与TTL对齐。",
            ],
        }
    except Exception as exc:
        return {"error": "execution_error", "message": str(exc)}
