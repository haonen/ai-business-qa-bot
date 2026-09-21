from __future__ import annotations
from bot.brand_query import source_fetch, enabled as brand_gate_enabled

import pandas as pd

from bot.db.connection import fetch_df
from bot.media_period import parse_media_period
from bot.platforms import canonical_platform, platform_filter_sql
from bot.tools.common import tool
from bot.tools.followup_common import month_keys, standard_result
from bot.tools.query_media_investment import _CHANNEL_RULES
from bot.tools.query_ec_nso import query_ec_nso, BET_SCOPE_NOTE
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
    df = source_fetch(fetch_df,'ai_bot_media_topline_investment','brand_r','brand_r = :brand',
        f"""
        SELECT
          CASE WHEN CAST(period_month AS DATE) BETWEEN :focus_start AND :focus_end THEN 'current' ELSE 'prior' END AS period_key,
          CAST(period_month AS DATE) AS period_month, ait_roe AS ait, media, submedia,
          bkfs_overall, bkfs_xiaohongshu, bkfs_douyin,
          SUM(spend_million) * 1000000 AS spend, COUNT(*) AS row_count
        FROM ai_bot_media_topline_investment
        WHERE brand_r = :brand
          AND (CAST(period_month AS DATE) BETWEEN :focus_start AND :focus_end OR CAST(period_month AS DATE) BETWEEN :prior_start AND :prior_end)
        GROUP BY period_key, CAST(period_month AS DATE), ait_roe, media, submedia, bkfs_overall, bkfs_xiaohongshu, bkfs_douyin
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
    requested_commerce_platform = ""
    if requested_platform:
        try:
            requested_commerce_platform = canonical_platform(
                requested_platform, allow_ttl=False
            )
        except ValueError:
            pass
    platform_breakdown = (
        ("platform" in group_by or requested_commerce_platform in {"TM", "DY", "JD"})
        and "bkfst" not in group_by
    )
    if platform_breakdown:
        transaction = raw[raw["ait"].fillna("").astype(str).str.casefold() == "transaction"].copy()
        parts = []
        for key, label in (("tmall", "TM"), ("douyin", "DY"), ("jd", "JD")):
            rule = _CHANNEL_RULES[key]
            mask = [rule(str(m or "").strip().casefold(), str(s or "").strip().casefold()) for m, s in zip(transaction["media"], transaction["submedia"])]
            piece = transaction.loc[mask].copy()
            piece["platform"] = label
            parts.append(piece)
        df = pd.concat(parts, ignore_index=True) if parts else transaction.iloc[0:0]
        if requested_commerce_platform:
            df = df[df["platform"] == requested_commerce_platform]
    bkfst_source_column = None
    if "bkfst" in group_by or filters.get("bkfst"):
        scope = str(filters.get("platform") or "overall").casefold()
        try:
            scope = canonical_platform(scope, allow_ttl=False, allow_red=True)
        except ValueError:
            scope = "OVERALL"
        column = "bkfs_xiaohongshu" if scope == "RED" else ("bkfs_douyin" if scope == "DY" else "bkfs_overall")
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
        "source_current_months": sorted(raw.loc[raw["period_key"] == "current", "month"].unique().tolist()),
    }
    return rows, totals, coverage



def _query_search(brand: str, parsed, group_by: list[str]) -> tuple[list[dict], dict, dict]:
    df = source_fetch(fetch_df,'ai_bot_media_search_index','brand','brand=:brand',
        """
        SELECT CAST(report_month AS DATE) AS report_month, grain_level, category,
               current_search_index AS search_actual, previous_search_index AS search_prior,
               COUNT(*) AS row_count
        FROM ai_bot_media_search_index
        WHERE brand=:brand AND CAST(report_month AS DATE) BETWEEN :start_month AND :end_month
        GROUP BY CAST(report_month AS DATE), grain_level, category, current_search_index, previous_search_index
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
    df = source_fetch(fetch_df,'ai_bot_media_ksi_performance','brand','brand=:brand',
        """
        SELECT CASE WHEN CAST(period_month AS DATE) BETWEEN :focus_start AND :focus_end THEN 'current' ELSE 'prior' END AS period_key,
               CAST(period_month AS DATE) AS period_month, LOWER(platform) AS kol_platform, tier, kol_type,
               COALESCE(NULLIF(TRIM(nickname), ''), NULLIF(TRIM(kol_id_front), ''), '未知KOL') AS kol,
               SUM(big_v_cost) AS cost, SUM(COALESCE(ttl_engagement,0)) AS engage, COUNT(*) AS row_count
        FROM ai_bot_media_ksi_performance
        WHERE brand=:brand AND (CAST(period_month AS DATE) BETWEEN :focus_start AND :focus_end OR CAST(period_month AS DATE) BETWEEN :prior_start AND :prior_end)
        GROUP BY period_key, CAST(period_month AS DATE), LOWER(platform), tier, kol_type,
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
        normalized = set()
        for value in values:
            try:
                canonical = canonical_platform(value, allow_ttl=False, allow_red=True)
                normalized.add("douyin" if canonical == "DY" else "red" if canonical == "RED" else canonical.casefold())
            except ValueError:
                normalized.add(str(value).casefold())
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
        if "category" in group_by or filters.get("category"):
            return {"error": "unsupported_category", "message": BET_SCOPE_NOTE}
        _validate(group_by)
        remarks = [BET_SCOPE_NOTE]
        parsed = parse_media_period(period)
        source_brands = ({key: brand for key in ("topline", "search", "ksi", "nso")}
                         if brand_gate_enabled() else (source_brands or {}))
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
            requested = month_keys(parsed.focus_start, parsed.focus_end)
            source_current_months = coverage.get("source_current_months") or coverage.get("current_months") or []
            missing_current_months = [
                month for month in requested if month not in source_current_months
            ]
            if missing_current_months:
                missing_text = "、".join(f"{int(month[-2:])}月" for month in missing_current_months)
                latest_text = (
                    f"，当前最新可用月份为{int(source_current_months[-1][-2:])}月"
                    if source_current_months
                    else ""
                )
                return {
                    "error": "requested_period_incomplete",
                    "message": (
                        f"Topline的BET花费数据尚未覆盖{missing_text}{latest_text}。"
                        "为避免把部分月份累计值误当成完整区间，"
                        "本次不返回该时间段的BET花费数字。"
                    ),
                    "coverage": {
                        **coverage,
                        "requested_months": requested,
                        "missing_current_months": missing_current_months,
                    },
                }
            if any(m in metrics for m in ("nso_actual", "nso_evol", "fee_ratio", "fee_ratio_change")):
                nso_brand = source_brands.get("nso") or (brand if not source_brands else None)
                nso = query_ec_nso(nso_brand, parsed.focus_start, parsed.focus_end,
                                   parsed.prior_start, parsed.prior_end) if nso_brand else {
                                       "error": "source_unavailable", "message": "NSO品牌映射不可用，费比留空。"}
                remarks.extend(nso.get("remarks") or [nso.get("message", "")])
                coverage["nso"] = nso.get("coverage", {})
                for row in rows:
                    month = row.get("month")
                    prior_month = f"{int(month[:4])-1}{month[4:]}" if month else None
                    current_nso = (nso.get("monthly", {}).get("current", {}).get(month, {}).get("nso")
                                   if month else nso.get("nso_actual"))
                    prior_nso = (nso.get("monthly", {}).get("prior", {}).get(prior_month, {}).get("nso")
                                 if month else nso.get("nso_prior"))
                    row["nso_actual"] = current_nso
                    row["nso_evol"] = safe_evol(current_nso, prior_nso) if current_nso is not None and prior_nso else None
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
        result = standard_result(
            query_meta={"domain": "bet", "business_category": "TTL", "scope_note": "品牌整体BET，不按电商品类拆分", "family": family, "brand": brand, "matched_brand": matched_brand, "period": period, "group_by": group_by},
            filters=filters, totals=totals, rows=rows, coverage={**coverage, "requested_months": requested},
            missing=[m for m in requested if "month" in group_by and m not in present],
        )
        result["remarks"] = remarks
        return result
    except Exception as exc:
        return {"error": "execution_error", "message": str(exc)}
