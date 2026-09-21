"""Atomic Newsletter 02 category metrics using the production Newsletter rules."""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from bot.db.connection import fetch_df
from bot.newsletter.category_market import calculate as category_calculate, query as category_query, weights
from bot.newsletter.data import CoverageError, TABLES, date_set, periods
from bot.newsletter.mex_window import discover as discover_mex, metrics as mex_metrics, query as query_mex
from bot.store_metric_scope import category_weights
from bot.tools.market_common import month_slices

CATEGORIES = ("SKIN", "FEMALE_SKIN", "HAIR", "MEX", "MAKEUP")
DISPLAY_BRANDS = {
    "SKIN": ("L'OREAL PARIS", "MAYBELLINE"),
    "FEMALE_SKIN": ("L'OREAL PARIS", "MAYBELLINE"),
    "HAIR": ("L'OREAL PARIS",),
    "MEX": ("L'OREAL PARIS",),
    "MAKEUP": ("L'OREAL PARIS", "3CE", "MAYBELLINE"),
}
MONTHLY_MARKET = "three_platforms_segmented_markets_monthly"
MONTHLY_STORES = "three_platform_store_rank_monthly"
MONTHLY_CATEGORIES = {
    "SKIN": "Skincare", "FEMALE_SKIN": "Skincare", "HAIR": "Hair",
    "MEX": "male skincare", "MAKEUP": "Makeup (exclude Fragrance)",
}


def _full_month_slices(params):
    slices = {period: month_slices(params[start], params[end])
              for period, start, end in (("current", "cs", "ce"), ("prior", "ps", "pe"))}
    return slices if all(item["full_month"] for parts in slices.values() for item in parts) else None


def _evol(now, old):
    now, old = Decimal(str(now)), Decimal(str(old))
    return (now / old - 1) * 100 if old > 0 else None


def compute_category_result(category, start, end, *, denominators, brands,
                            requested_period=None, effective_period=None,
                            sources=None, warnings=None):
    now_market = Decimal(str(denominators["current", "ALL"]))
    old_market = Decimal(str(denominators["prior", "ALL"]))
    now_pm = Decimal(str(denominators["current", "PM"]))
    old_pm = Decimal(str(denominators["prior", "PM"]))
    rows = []
    for canonical in DISPLAY_BRANDS[category]:
        values = brands.get(canonical, {})
        now = Decimal(str(values.get("current", 0)))
        old = Decimal(str(values.get("prior", 0)))
        current_share = now / now_pm * 100 if now_pm > 0 else None
        prior_share = old / old_pm * 100 if old_pm > 0 else None
        rows.append({
            "brand": "Maybelline" if canonical == "MAYBELLINE" else canonical,
            "current_gmv_yuan": str(now), "prior_gmv_yuan": str(old),
            "evol_pct": str(_evol(now, old)) if _evol(now, old) is not None else None,
            "current_share_pct": str(current_share) if current_share is not None else None,
            "prior_share_pct": str(prior_share) if prior_share is not None else None,
            "share_change_pp": str(current_share - prior_share)
                if current_share is not None and prior_share is not None else None,
        })
    cpd = None
    if category in ("SKIN", "FEMALE_SKIN", "MAKEUP"):
        owned = ("L'OREAL PARIS", "3CE", "MAYBELLINE", "DR.G")
        cpd_now = sum(Decimal(str(brands.get(b, {}).get("current", 0))) for b in owned)
        cpd_old = sum(Decimal(str(brands.get(b, {}).get("prior", 0))) for b in owned)
        current_share = cpd_now / now_pm * 100 if now_pm > 0 else None
        prior_share = cpd_old / old_pm * 100 if old_pm > 0 else None
        cpd = {
            "current_gmv_yuan": str(cpd_now), "prior_gmv_yuan": str(cpd_old),
            "evol_pct": str(_evol(cpd_now, cpd_old)) if _evol(cpd_now, cpd_old) is not None else None,
            "current_share_pct": str(current_share) if current_share is not None else None,
            "prior_share_pct": str(prior_share) if prior_share is not None else None,
            "share_change_pp": str(current_share - prior_share)
                if current_share is not None and prior_share is not None else None,
        }
    return {
        "schema": "newsletter.section02.category.v1", "category": category,
        "requested_period": requested_period or {"start": start, "end": end},
        "effective_period": effective_period or {"start": start, "end": end},
        "market_gmv_yuan": str(now_market), "market_prior_gmv_yuan": str(old_market),
        "market_evol_pct": str(_evol(now_market, old_market)) if _evol(now_market, old_market) is not None else None,
        "pure_mass_gmv_yuan": str(now_pm), "pure_mass_prior_gmv_yuan": str(old_pm),
        "pure_mass_evol_pct": str(_evol(now_pm, old_pm)) if _evol(now_pm, old_pm) is not None else None,
        "brands": rows, "cpd": cpd, "sources": sources or {}, "warnings": warnings or [],
        "scope_policy": "newsletter.section02.v1", "platform": "TTL",
    }


def maybelline_share_comparison(skin, makeup, remover):
    """Compare category shares and the remover-plus-makeup reference share."""
    if skin["requested_period"] != makeup["requested_period"]:
        raise ValueError("美宝莲份额对照需要同一请求周期。")
    def brand(result):
        return next(row for row in result["brands"] if row["brand"] == "Maybelline")
    skin_brand, makeup_brand = brand(skin), brand(makeup)
    denominator_now = Decimal(makeup["pure_mass_gmv_yuan"])
    denominator_old = Decimal(makeup["pure_mass_prior_gmv_yuan"])
    combined_now = Decimal(str(remover["current"])) + Decimal(makeup_brand["current_gmv_yuan"])
    combined_old = Decimal(str(remover["prior"])) + Decimal(makeup_brand["prior_gmv_yuan"])
    old_share = combined_now / denominator_now * 100 if denominator_now > 0 else None
    prior_share = combined_old / denominator_old * 100 if denominator_old > 0 else None
    return {
        "remover_makeup": {
            "current_gmv_yuan": str(combined_now), "prior_gmv_yuan": str(combined_old),
            "evol_pct": str(_evol(combined_now, combined_old)) if _evol(combined_now, combined_old) is not None else None,
            "current_share_pct": str(old_share) if old_share is not None else None,
            "prior_share_pct": str(prior_share) if prior_share is not None else None,
            "share_change_pp": str(old_share - prior_share)
                if old_share is not None and prior_share is not None else None,
        },
        "skin": skin_brand, "makeup": makeup_brand,
        "remover_gmv_yuan": str(Decimal(str(remover["current"]))),
        "skin_pure_mass_yuan": skin["pure_mass_gmv_yuan"],
        "makeup_pure_mass_yuan": makeup["pure_mass_gmv_yuan"],
        "skin_period": skin["effective_period"], "makeup_period": makeup["effective_period"],
    }


def _runner(audit):
    def run(name, sql, params):
        frame = fetch_df(sql, params)
        audit[name] = {"rows": len(frame), "sql": sql, "params": params}
        return frame.to_dict("records")
    return run


def _monthly_market_values(params, category, slices, run):
    required = ("Skincare", "male skincare") if category == "FEMALE_SKIN" else (MONTHLY_CATEGORIES[category],)
    binds = {**params, **{f"cat{i}": name for i, name in enumerate(required)}}
    category_filter = ",".join(f"LOWER(:cat{i})" for i in range(len(required)))
    amount = "REPLACE(TRIM(CAST(gmv AS CHAR)),',','')"
    valid = f"{amount} REGEXP '^[+-]?[0-9]+([.][0-9]+)?$'"
    sql = f"""SELECT CAST(bus_date AS DATE) day,UPPER(TRIM(platform)) platform,
category_EN category,global_segment segment,COUNT(*) row_count,
SUM(CASE WHEN {valid} THEN CAST({amount} AS DECIMAL(30,6)) ELSE 0 END) gmv,
SUM(CASE WHEN {valid} THEN 0 ELSE 1 END) invalid_rows
FROM {MONTHLY_MARKET} WHERE (bus_date BETWEEN :cs AND :ce OR bus_date BETWEEN :ps AND :pe)
AND UPPER(TRIM(platform)) IN ('TM','JD','DY')
AND LOWER(TRIM(category_EN)) IN ({category_filter})
AND UPPER(TRIM(global_segment)) IN ('BEAUTY MARKET','PURE MASS')
GROUP BY CAST(bus_date AS DATE),UPPER(TRIM(platform)),category_EN,global_segment"""
    found = {}
    for row in run("section02_month_market", sql, binds):
        day = str(row["day"])[:10]
        period = "current" if params["cs"] <= day <= params["ce"] else "prior"
        key = (period, day[:7], row["platform"], str(row["category"]).strip().lower(),
               str(row["segment"]).strip().upper())
        if int(row["row_count"]) != 1 or int(row.get("invalid_rows") or 0) or key in found:
            raise ValueError("月度品类市场源存在重复或无效金额：" + str(key))
        found[key] = Decimal(str(row["gmv"]))
    totals = {(per, scope): Decimal(0) for per in ("current", "prior") for scope in ("ALL", "PM")}
    for period, parts in slices.items():
        for item in parts:
            for platform in TABLES:
                for scope, segment in (("ALL", "BEAUTY MARKET"), ("PM", "PURE MASS")):
                    keys = [(period, item["month"], platform, name.lower(), segment) for name in required]
                    if any(key not in found for key in keys):
                        raise ValueError("月度品类市场源覆盖不完整：" + str(keys))
                    value = found[keys[0]] - found[keys[1]] if category == "FEMALE_SKIN" else found[keys[0]]
                    if scope == "PM" and value <= 0:
                        raise ValueError("月度品类 Pure Mass 分母不大于零：" + str(keys))
                    totals[period, scope] += value
    return totals


def _monthly_store_rows(params, category, run):
    cpd_brands = ("L'OREAL PARIS", "3CE", "MAYBELLINE", "DR.G") if category in (
        "SKIN", "FEMALE_SKIN", "MAKEUP") else ()
    wanted = tuple(dict.fromkeys((*DISPLAY_BRANDS[category], *cpd_brands)))
    binds = {**params, **{f"brand{i}": brand for i, brand in enumerate(wanted)}}
    amount = "REPLACE(TRIM(CAST(gmv AS CHAR)),',','')"
    valid = f"{amount} REGEXP '^[+-]?[0-9]+([.][0-9]+)?$'"
    sql = f"""SELECT CAST(bus_date AS DATE) day,UPPER(TRIM(platform)) platform,
UPPER(TRIM(brand_name)) brand_name,category_EN_level_1,category_EN_level_2,
SUM(CASE WHEN {valid} THEN CAST({amount} AS DECIMAL(30,6)) ELSE 0 END) gmv,
SUM(CASE WHEN {valid} THEN 0 ELSE 1 END) invalid_rows
FROM {MONTHLY_STORES} WHERE (bus_date BETWEEN :cs AND :ce OR bus_date BETWEEN :ps AND :pe)
AND UPPER(TRIM(platform)) IN ('TM','JD','DY')
AND UPPER(TRIM(brand_name)) IN ({','.join(f':brand{i}' for i in range(len(wanted)))})
GROUP BY CAST(bus_date AS DATE),UPPER(TRIM(platform)),UPPER(TRIM(brand_name)),category_EN_level_1,category_EN_level_2"""
    return run("section02_month_brands", sql, binds), wanted


def _monthly_brand_values(params, category, run):
    rows, wanted = _monthly_store_rows(params, category, run)
    totals = defaultdict(Decimal)
    key = "SKINCARE_ALL" if category == "SKIN" else ("SKIN" if category == "FEMALE_SKIN" else category)
    for row in rows:
        if int(row.get("invalid_rows") or 0):
            raise ValueError("Invalid monthly section02 brand amount")
        platform = row["platform"]
        if platform not in TABLES:
            raise ValueError("Unexpected monthly store platform")
        day = str(row["day"])[:10]
        period = "current" if params["cs"] <= day <= params["ce"] else "prior"
        row_weights = weights(row, platform, category_weights, include_mex=category != "SKIN")
        sign = sum(value for name, value in row_weights if name == key)
        totals[row["brand_name"], period] += Decimal(str(row["gmv"])) * sign
    return {brand: {period: totals[brand, period] for period in ("current", "prior")}
            for brand in wanted}


def _brand_values(params, category, run):
    totals = defaultdict(Decimal)
    cpd_brands = ("L'OREAL PARIS", "3CE", "MAYBELLINE", "DR.G") if category in (
        "SKIN", "FEMALE_SKIN", "MAKEUP") else ()
    wanted = tuple(dict.fromkeys((*DISPLAY_BRANDS[category], *cpd_brands)))
    binds = {**params, **{f"brand{i}": b for i, b in enumerate(wanted)}}
    brand_filter = "UPPER(TRIM(brand_name)) IN (" + ",".join(f":brand{i}" for i in range(len(wanted))) + ")"
    for platform, table in TABLES.items():
        amount = "REPLACE(TRIM(CAST(gmv AS CHAR)),',','')"
        valid = f"{amount} REGEXP '^[+-]?[0-9]+([.][0-9]+)?$'"
        sql = f"""SELECT CAST(bus_date AS DATE) day,UPPER(TRIM(brand_name)) brand_name,
category_EN_level_1,category_EN_level_2,
SUM(CASE WHEN {valid} THEN CAST({amount} AS DECIMAL(30,6)) ELSE 0 END) gmv,
SUM(CASE WHEN {valid} THEN 0 ELSE 1 END) invalid_rows
FROM `{table}` WHERE (bus_date BETWEEN :cs AND :ce OR bus_date BETWEEN :ps AND :pe)
AND {brand_filter}
GROUP BY CAST(bus_date AS DATE),UPPER(TRIM(brand_name)),category_EN_level_1,category_EN_level_2"""
        for row in run("section02_atomic_brands_" + platform, sql, binds):
            if int(row.get("invalid_rows") or 0):
                raise ValueError("Invalid section02 brand amount")
            day = str(row["day"])[:10]
            period = "current" if params["cs"] <= day <= params["ce"] else "prior"
            brand = row["brand_name"]
            include_mex = category != "SKIN"
            row_weights = weights(row, platform, category_weights, include_mex=include_mex)
            key = "SKINCARE_ALL" if category == "SKIN" else (
                "SKIN" if category == "FEMALE_SKIN" else category
            )
            value = sum(sign for cat, sign in row_weights if cat == key)
            totals[brand, period] += Decimal(str(row["gmv"])) * value
    return {brand: {period: totals[brand, period] for period in ("current", "prior")}
            for brand in wanted}


def _remover_values(params, run):
    """The level-two remover subtotal, never the whole Skincare level-one row."""
    totals = {"current": Decimal(0), "prior": Decimal(0)}
    for platform, table in TABLES.items():
        amount = "REPLACE(TRIM(CAST(gmv AS CHAR)),',','')"
        valid = f"{amount} REGEXP '^[+-]?[0-9]+([.][0-9]+)?$'"
        sql = f"""SELECT CAST(bus_date AS DATE) day,
SUM(CASE WHEN {valid} THEN CAST({amount} AS DECIMAL(30,6)) ELSE 0 END) gmv,
SUM(CASE WHEN {valid} THEN 0 ELSE 1 END) invalid_rows
FROM `{table}` WHERE (bus_date BETWEEN :cs AND :ce OR bus_date BETWEEN :ps AND :pe)
AND UPPER(TRIM(brand_name))='MAYBELLINE'
AND LOWER(TRIM(category_EN_level_2))='makeup remover'
GROUP BY CAST(bus_date AS DATE)"""
        for row in run("section02_maybelline_remover_" + platform, sql, params):
            if int(row.get("invalid_rows") or 0):
                raise ValueError("Invalid Maybelline remover amount")
            period = "current" if params["cs"] <= str(row["day"])[:10] <= params["ce"] else "prior"
            totals[period] += Decimal(str(row["gmv"]))
    return totals


def _monthly_remover_values(params, run):
    amount = "REPLACE(TRIM(CAST(gmv AS CHAR)),',','')"
    valid = f"{amount} REGEXP '^[+-]?[0-9]+([.][0-9]+)?$'"
    sql = f"""SELECT CAST(bus_date AS DATE) day,UPPER(TRIM(platform)) platform,
SUM(CASE WHEN {valid} THEN CAST({amount} AS DECIMAL(30,6)) ELSE 0 END) gmv,
SUM(CASE WHEN {valid} THEN 0 ELSE 1 END) invalid_rows
FROM {MONTHLY_STORES} WHERE (bus_date BETWEEN :cs AND :ce OR bus_date BETWEEN :ps AND :pe)
AND UPPER(TRIM(platform)) IN ('TM','JD','DY')
AND UPPER(TRIM(brand_name))='MAYBELLINE'
AND LOWER(TRIM(category_EN_level_2))='makeup remover'
GROUP BY CAST(bus_date AS DATE),UPPER(TRIM(platform))"""
    totals = {"current": Decimal(0), "prior": Decimal(0)}
    for row in run("section02_month_remover", sql, params):
        if int(row.get("invalid_rows") or 0):
            raise ValueError("Invalid monthly Maybelline remover amount")
        day = str(row["day"])[:10]
        period = "current" if params["cs"] <= day <= params["ce"] else "prior"
        totals[period] += Decimal(str(row["gmv"]))
    return totals


def query_maybelline_remover_gmv(start, end):
    params = periods(start, end)
    run = _runner({})
    return (_monthly_remover_values(params, run) if _full_month_slices(params)
            else _remover_values(params, run))


def query_category_metrics(start, end, category):
    if category not in CATEGORIES:
        raise ValueError("不支持的品类。")
    params = periods(start, end)
    if (date.fromisoformat(end) - date.fromisoformat(start)).days >= 366:
        raise ValueError("查询期间请限制在366天以内。")
    audit = {}
    run = _runner(audit)
    requested = {"start": start, "end": end}
    monthly_slices = _full_month_slices(params)
    if monthly_slices:
        denominators = _monthly_market_values(params, category, monthly_slices, run)
        brands = _monthly_brand_values(params, category, run)
        effective, warnings, used_params = requested, [], params
        if category == "SKIN":
            warnings.append("SKIN护肤市场和品牌金额包含男士，与MEX有重叠，不能相加。")
        if category == "FEMALE_SKIN":
            warnings.append("Female Skin为护肤口径剔除男士护肤；本期与去年同期均按同一日期范围计算。")
    elif category == "MEX":
        window = {
            "params": params,
            "latest_observed": end,
            "window_days": len(date_set(start, end)),
            "source": "天猫_大盘_日表",
            "field": "category_EN_level_2",
            "value": "male skincare",
            "mode": "requested_period",
        }
        try:
            bundle = query_mex(window, run)
            mex_denominators, owned = mex_metrics(bundle, ["MEX"])
            used_fallback = False
        except CoverageError:
            window = discover_mex(params, run)
            bundle = query_mex(window, run)
            mex_denominators, owned = mex_metrics(bundle, ["MEX"])
            used_fallback = True
        denominators = {
            (period, scope): Decimal(str(mex_denominators["TTL", period, scope, "MEX"]))
            for period in ("current", "prior") for scope in ("ALL", "PM")
        }
        brands = {"L'OREAL PARIS": {period: Decimal(str(owned[period, "MEX"]))
                                     for period in ("current", "prior")}}
        effective = {"start": window["params"]["cs"], "end": window["params"]["ce"]}
        warnings = [] if not used_fallback else [
            f"请求期内的天猫男士大盘数据尚未完整更新，因此按最新可用数据日往前取连续7天；"
            f"本次实际数据周期为{effective['start']}至{effective['end']}。"
        ]
        used_params = window["params"]
    else:
        female_skin = category == "FEMALE_SKIN"
        calculation_category = "SKIN" if female_skin else category
        include_mex = category != "SKIN"
        raw = category_query(params, run, TABLES, include_mex=include_mex)
        values, missing = category_calculate(
            raw, params, category_weights, date_set,
            include_mex=include_mex,
            categories=["SKIN", "MEX"] if female_skin else [calculation_category],
        )
        if missing:
            raise ValueError("品类市场源覆盖不完整：" + str(missing[:3]))
        denominators = {
            (period, scope): Decimal(str(values["TTL", period, scope, calculation_category]))
            for period in ("current", "prior") for scope in ("ALL", "PM")
        }
        brands = _brand_values(params, category, run)
        effective, warnings, used_params = requested, [], params
        if category == "SKIN":
            warnings.append("SKIN护肤市场和品牌金额包含男士，与MEX有重叠，不能相加。")
        if female_skin:
            warnings.append("Female Skin为护肤口径剔除男士护肤；本期与去年同期均按同一日期范围计算。")
    result = compute_category_result(
        category, used_params["cs"], used_params["ce"], denominators=denominators,
        brands=brands, requested_period=requested, effective_period=effective,
        sources=({"market": MONTHLY_MARKET, "stores": MONTHLY_STORES}
                 if monthly_slices else
                 {"market": dict(__import__("bot.newsletter.category_market", fromlist=["MARKETS"]).MARKETS),
                  "stores": dict(TABLES)}), warnings=warnings,
    )
    result["audit"] = audit
    return result
