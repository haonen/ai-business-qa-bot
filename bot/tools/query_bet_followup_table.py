from __future__ import annotations

import pandas as pd

from bot.db.connection import fetch_df
from bot.media_period import parse_media_period
from bot.tools.common import tool
from bot.tools.followup_common import month_keys, standard_result
from bot.tools.query_media_investment import _CHANNEL_RULES
from bot.utils import safe_div, safe_evol


BET_DIMENSIONS = {"month", "category", "ait", "platform", "bkfst", "kol_platform", "tier", "kol_type", "kol"}
BET_PAIRS = {
    ("month", "ait"), ("month", "platform"), ("month", "bkfst"),
    ("month", "tier"), ("month", "kol_type"), ("month", "kol_platform"), ("month", "category"),
}


def _validate(group_by: list[str]):
    if len(group_by) > 2 or any(value not in BET_DIMENSIONS for value in group_by):
        raise ValueError("不支持的BET维度组合。")
    if len(group_by) == 2 and tuple(group_by) not in BET_PAIRS and tuple(reversed(group_by)) not in BET_PAIRS:
        raise ValueError("该BET交叉维度未开放。")


def _aligned_month(value, period_key: str) -> str:
    month = pd.Timestamp(value).strftime("%Y-%m")
    return month if period_key == "current" else f"{int(month[:4]) + 1:04d}{month[4:]}"


def _merge_rows(df: pd.DataFrame, dims: list[str], value_columns: list[str]) -> tuple[list[dict], dict]:
    use_dims = dims or ["_total"]
    if not dims:
        df = df.copy()
        df["_total"] = "TTL"
    grouped = df.groupby(["period_key", *use_dims], dropna=False)[value_columns + ["row_count"]].sum().reset_index()
    current = grouped[grouped["period_key"] == "current"].drop(columns="period_key")
    prior = grouped[grouped["period_key"] == "prior"].drop(columns="period_key")
    merged = current.merge(prior, on=use_dims, how="outer", suffixes=("_actual", "_prior")).fillna(0)
    totals = {column: float(merged[f"{column}_actual"].sum()) for column in value_columns}
    totals.update({f"{column}_prior": float(merged[f"{column}_prior"].sum()) for column in value_columns})
    rows = []
    for _, raw in merged.iterrows():
        row = {dim: str(raw[dim]) for dim in use_dims}
        for column in value_columns:
            actual = float(raw[f"{column}_actual"] or 0)
            prior = float(raw[f"{column}_prior"] or 0)
            row[f"{column}_actual"] = round(actual, 4)
            row[f"{column}_prior"] = round(prior, 4)
            row[f"{column}_evol"] = safe_evol(actual, prior) if prior else None
        row["_current_rows"] = int(raw.get("row_count_actual") or 0)
        row["_prior_rows"] = int(raw.get("row_count_prior") or 0)
        rows.append(row)
    return rows, totals


def _query_media(brand: str, parsed, group_by: list[str], filters: dict) -> tuple[list[dict], dict, dict]:
    df = fetch_df(
        """
        SELECT
          CASE WHEN period_month BETWEEN :focus_start AND :focus_end THEN 'current' ELSE 'prior' END AS period_key,
          period_month, ait_roe AS ait, media, submedia,
          bkfs_overall, bkfs_xiaohongshu, bkfs_douyin,
          SUM(spend_million) * 1000000 AS spend, COUNT(*) AS row_count
        FROM ai_bot_media_topline_investment
        WHERE brand_r = :brand
          AND (period_month BETWEEN :focus_start AND :focus_end OR period_month BETWEEN :prior_start AND :prior_end)
        GROUP BY period_key, period_month, ait_roe, media, submedia, bkfs_overall, bkfs_xiaohongshu, bkfs_douyin
        """,
        {"brand": brand, "focus_start": parsed.focus_start, "focus_end": parsed.focus_end,
         "prior_start": parsed.prior_start, "prior_end": parsed.prior_end},
    )
    if df.empty:
        return [], {}, {}
    df["spend"] = pd.to_numeric(df["spend"], errors="coerce").fillna(0.0)
    df["row_count"] = pd.to_numeric(df["row_count"], errors="coerce").fillna(0)
    df["month"] = df.apply(lambda r: _aligned_month(r["period_month"], r["period_key"]), axis=1)
    df["ait"] = df["ait"].fillna("未分类").astype(str).str.strip().replace("", "未分类")
    raw = df.copy()
    if filters.get("ait"):
        df = df[df["ait"].fillna("").astype(str).str.casefold() == str(filters["ait"]).casefold()]
    requested_platform = str(filters.get("platform") or "").casefold()
    platform_breakdown = (
        ("platform" in group_by or requested_platform in {"tmall", "douyin", "jd"})
        and "bkfst" not in group_by
    )
    if platform_breakdown:
        transaction = raw[raw["ait"].fillna("").astype(str).str.casefold() == "transaction"].copy()
        parts = []
        for key, label in (("tmall", "TMALL"), ("douyin", "Douyin"), ("jd", "JD")):
            rule = _CHANNEL_RULES[key]
            mask = [rule(str(m or "").strip().casefold(), str(s or "").strip().casefold()) for m, s in zip(transaction["media"], transaction["submedia"])]
            piece = transaction.loc[mask].copy()
            piece["platform"] = label
            parts.append(piece)
        df = pd.concat(parts, ignore_index=True) if parts else transaction.iloc[0:0]
        if filters.get("platform") and str(filters["platform"]).casefold() not in {"red", "xiaohongshu"}:
            df = df[df["platform"].str.casefold() == str(filters["platform"]).casefold()]
    bkfst_source_column = None
    if "bkfst" in group_by or filters.get("bkfst"):
        scope = str(filters.get("platform") or "overall").casefold()
        column = "bkfs_xiaohongshu" if scope in {"red", "xiaohongshu"} else ("bkfs_douyin" if scope == "douyin" else "bkfs_overall")
        bkfst_source_column = column
        valid_bkfst = df[column].notna() & df[column].astype(str).str.strip().ne("")
        df = df[valid_bkfst].copy()
        df["bkfst"] = df[column].astype(str).str.strip()
        if filters.get("bkfst"):
            df = df[df["bkfst"].astype(str).str.casefold() == str(filters["bkfst"]).casefold()]
    rows, totals = _merge_rows(df, group_by, ["spend"])
    # AIT uses TTL; BKFS uses the selected reporting scope; platforms use Transaction.
    if platform_breakdown:
        denominator_source = raw[raw["ait"].fillna("").astype(str).str.casefold() == "transaction"]
    elif bkfst_source_column:
        denominator_source = raw[
            raw[bkfst_source_column].notna()
            & raw[bkfst_source_column].astype(str).str.strip().ne("")
        ]
    else:
        denominator_source = raw
    den_dims = ["period_key"] + (["month"] if "month" in group_by else [])
    den_lookup = denominator_source.groupby(den_dims, dropna=False)["spend"].sum().to_dict()
    for row in rows:
        actual, prior = row["spend_actual"], row["spend_prior"]
        current_key = ("current", row.get("month")) if "month" in group_by else "current"
        prior_key = ("prior", row.get("month")) if "month" in group_by else "prior"
        row["spend_weight"] = safe_div(actual, den_lookup.get(current_key))
        prior_weight = safe_div(prior, den_lookup.get(prior_key))
        row["spend_weight_change"] = row["spend_weight"] - prior_weight if row["spend_weight"] is not None and prior_weight is not None else None
    coverage = {
        "current_months": sorted(df.loc[df["period_key"] == "current", "month"].unique().tolist()),
        "prior_months": sorted(df.loc[df["period_key"] == "prior", "month"].unique().tolist()),
    }
    return rows, totals, coverage


def _query_nso(brand: str, parsed) -> pd.DataFrame:
    return fetch_df(
        """
        SELECT CASE WHEN year = :current_year THEN 'current' ELSE 'prior' END AS period_key,
               year, month, SUM(Sales) AS nso, COUNT(*) AS row_count
        FROM top_brands_total_ec
        WHERE Brand = :brand AND platform = 'TTL'
          AND ((year = :current_year AND month BETWEEN :current_start_month AND :current_end_month)
            OR (year = :prior_year AND month BETWEEN :prior_start_month AND :prior_end_month))
        GROUP BY period_key, year, month
        """,
        {"brand": brand, "current_year": int(parsed.focus_start[:4]), "prior_year": int(parsed.prior_start[:4]),
         "current_start_month": int(parsed.focus_start[5:7]), "current_end_month": int(parsed.focus_end[5:7]),
         "prior_start_month": int(parsed.prior_start[5:7]), "prior_end_month": int(parsed.prior_end[5:7])},
    )


def _query_search(brand: str, parsed, group_by: list[str]) -> tuple[list[dict], dict, dict]:
    df = fetch_df(
        """
        SELECT report_month, grain_level, category,
               current_search_index AS search_actual, previous_search_index AS search_prior,
               COUNT(*) AS row_count
        FROM ai_bot_media_search_index
        WHERE brand=:brand AND report_month BETWEEN :start_month AND :end_month
        GROUP BY report_month, grain_level, category, current_search_index, previous_search_index
        """,
        {"brand": brand, "start_month": parsed.focus_start, "end_month": parsed.focus_end},
    )
    if df.empty:
        return [], {}, {}
    category_mode = "category" in group_by
    df = df[df["grain_level"] == ("brand_category" if category_mode else "brand")]
    rows = []
    for _, raw in df.iterrows():
        actual, prior = float(raw["search_actual"] or 0), float(raw["search_prior"] or 0)
        row = {"month": pd.Timestamp(raw["report_month"]).strftime("%Y-%m"), "search_actual": round(actual), "search_prior": round(prior), "search_evol": safe_evol(actual, prior)}
        if category_mode:
            row["category"] = str(raw.get("category") or "其他")
        rows.append(row)
    return rows, {"search_actual": sum(r["search_actual"] for r in rows), "search_prior": sum(r["search_prior"] for r in rows)}, {"current_months": [r["month"] for r in rows]}


def _query_kol(brand: str, parsed, group_by: list[str], filters: dict) -> tuple[list[dict], dict, dict]:
    df = fetch_df(
        """
        SELECT CASE WHEN period_month BETWEEN :focus_start AND :focus_end THEN 'current' ELSE 'prior' END AS period_key,
               period_month, LOWER(platform) AS kol_platform, tier, kol_type,
               COALESCE(NULLIF(TRIM(nickname), ''), NULLIF(TRIM(kol_id_front), ''), '未知KOL') AS kol,
               SUM(big_v_cost) AS cost, SUM(COALESCE(ttl_engagement,0)) AS engage, COUNT(*) AS row_count
        FROM ai_bot_media_ksi_performance
        WHERE brand=:brand AND (period_month BETWEEN :focus_start AND :focus_end OR period_month BETWEEN :prior_start AND :prior_end)
        GROUP BY period_key, period_month, LOWER(platform), tier, kol_type,
                 COALESCE(NULLIF(TRIM(nickname), ''), NULLIF(TRIM(kol_id_front), ''), '未知KOL')
        """,
        {"brand": brand, "focus_start": parsed.focus_start, "focus_end": parsed.focus_end,
         "prior_start": parsed.prior_start, "prior_end": parsed.prior_end},
    )
    if df.empty:
        return [], {}, {}
    for column in ("cost", "engage", "row_count"):
        df[column] = pd.to_numeric(df[column], errors="coerce").fillna(0)
    for column in ("kol_platform", "tier", "kol_type", "kol"):
        df[column] = df[column].fillna("未分类").astype(str).str.strip().replace("", "未分类")
    df["month"] = df.apply(lambda r: _aligned_month(r["period_month"], r["period_key"]), axis=1)
    if filters.get("platform"):
        values = filters["platform"] if isinstance(filters["platform"], list) else [filters["platform"]]
        normalized = {str(value).casefold() for value in values}
        df = df[df["kol_platform"].fillna("").astype(str).str.casefold().isin(normalized)]
    denominator_source = df.copy()
    for field in ("tier", "kol_type"):
        if filters.get(field):
            requested = filters[field]
            values = requested if isinstance(requested, list) else [requested]
            normalized = {str(value).casefold() for value in values}
            df = df[df[field].fillna("").astype(str).str.casefold().isin(normalized)]
    rows, totals = _merge_rows(df, group_by, ["cost", "engage"])
    den_dims = ["period_key"] + (["month"] if "month" in group_by else [])
    cost_lookup = denominator_source.groupby(den_dims, dropna=False)["cost"].sum().to_dict()
    for row in rows:
        current_key = ("current", row.get("month")) if "month" in group_by else "current"
        prior_key = ("prior", row.get("month")) if "month" in group_by else "prior"
        row["cost_weight"] = safe_div(row["cost_actual"], cost_lookup.get(current_key))
        pw = safe_div(row["cost_prior"], cost_lookup.get(prior_key))
        row["cost_weight_change"] = row["cost_weight"] - pw if row["cost_weight"] is not None and pw is not None else None
        row["cpe"] = round(row["cost_actual"] / row["engage_actual"], 4) if row["engage_actual"] else None
    return rows, totals, {"current_months": sorted(df.loc[df["period_key"] == "current", "month"].unique().tolist())}


@tool
def query_bet_followup_table(
    brand: str,
    period: str,
    group_by: list[str] | None = None,
    filters: dict | None = None,
    metrics: list[str] | None = None,
    limit: int = 20,
    source_brands: dict[str, str | None] | None = None,
) -> dict:
    """Return a whitelisted BET follow-up table without accepting SQL fragments."""
    try:
        group_by, filters, metrics = list(group_by or []), dict(filters or {}), list(metrics or [])
        _validate(group_by)
        parsed = parse_media_period(period)
        source_brands = source_brands or {}
        family = "search" if any(m.startswith("search_") for m in metrics) else ("kol" if any(d in group_by for d in ("kol_platform", "tier", "kol_type", "kol")) or any(m.startswith(("cost_", "engage_", "cpe")) for m in metrics) else "media")
        source = {"search": "search", "kol": "ksi", "media": "topline"}[family]
        if source_brands and not source_brands.get(source):
            return {"error": "source_unavailable", "message": f"品牌“{brand}”在{source}数据源没有可用映射。"}
        matched_brand = source_brands.get(source) or brand
        if family == "search":
            rows, totals, coverage = _query_search(matched_brand, parsed, group_by)
        elif family == "kol":
            rows, totals, coverage = _query_kol(matched_brand, parsed, group_by, filters)
        else:
            rows, totals, coverage = _query_media(matched_brand, parsed, group_by, filters)
            if any(m in metrics for m in ("nso_actual", "nso_evol", "fee_ratio", "fee_ratio_change")):
                nso_brand = source_brands.get("nso")
                nso = _query_nso(nso_brand, parsed) if nso_brand else pd.DataFrame()
                nso_lookup = {}
                for _, raw in nso.iterrows():
                    month = f"{int(raw['year']) + (1 if raw['period_key'] == 'prior' else 0):04d}-{int(raw['month']):02d}"
                    nso_lookup[(str(raw["period_key"]), month)] = float(raw["nso"] or 0)
                for row in rows:
                    month = row.get("month")
                    current_nso = nso_lookup.get(("current", month)) if month else sum(v for (kind, _), v in nso_lookup.items() if kind == "current")
                    prior_nso = nso_lookup.get(("prior", month)) if month else sum(v for (kind, _), v in nso_lookup.items() if kind == "prior")
                    row["nso_actual"] = current_nso
                    row["nso_evol"] = safe_evol(current_nso, prior_nso) if prior_nso else None
                    row["fee_ratio"] = safe_div(row.get("spend_actual"), current_nso)
                    prior_ratio = safe_div(row.get("spend_prior"), prior_nso)
                    row["fee_ratio_change"] = row["fee_ratio"] - prior_ratio if row["fee_ratio"] is not None and prior_ratio is not None else None
        requested = month_keys(parsed.focus_start, parsed.focus_end)
        present = sorted({r.get("month") for r in rows if r.get("month")})
        if "month" in group_by:
            other_dims = [dim for dim in group_by if dim != "month"]
            for month in requested:
                if month not in present:
                    rows.append({"month": month, **{dim: "—" for dim in other_dims}})
            rows.sort(key=lambda row: (str(row.get("month") or ""), -float(row.get("spend_actual") or row.get("cost_actual") or row.get("search_actual") or 0)))
        rows = rows[:max(1, min(int(limit), 50))]
        return standard_result(
            query_meta={"domain": "bet", "family": family, "brand": brand, "matched_brand": matched_brand, "period": period, "group_by": group_by},
            filters=filters, totals=totals, rows=rows, coverage={**coverage, "requested_months": requested},
            missing=[m for m in requested if "month" in group_by and m not in present],
        )
    except Exception as exc:
        return {"error": "execution_error", "message": str(exc)}
