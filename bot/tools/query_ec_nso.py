from __future__ import annotations

import math
import pandas as pd

from bot.brand_query import source_fetch, BrandQueryError
from bot.db.connection import fetch_df
from bot.tools.common import tool
from bot.tools.followup_common import month_keys
from bot.utils import safe_evol

BET_SCOPE_NOTE = "BET分析暂不支持按category拆分，只能查看整个品牌的BET；本报告中的媒体花费与费比均为品牌整体口径。"
NSO_RULE = "NSO来自EC Consolidation，覆盖全部平台，汇总除Total Beauty外各category的sales。"


def summarize_nso(df, brand, focus_start, focus_end, prior_start, prior_end, available_months=None):
    """Require every requested month; absence must never become zero or partial TTL."""
    monthly, totals, remarks, coverage = {}, {}, [], {}
    for kind, start, end in (("current", focus_start, focus_end), ("prior", prior_start, prior_end)):
        expected = month_keys(start, end)
        result = {}
        for key in expected:
            selected = df[(df["year"].astype(int) == int(key[:4])) &
                          (df["month"].astype(int) == int(key[5:]))] if not df.empty else df
            entries, categories, value, invalid = [], [], 0.0, False
            for row in selected.to_dict("records"):
                category = str(row.get("category") or "").strip()
                if category.casefold() == "total beauty":
                    continue
                if not category:
                    invalid = True
                    continue
                categories.append(category)
                number = pd.to_numeric(row.get("nso"), errors="coerce")
                if pd.isna(number) or not math.isfinite(float(number)) or int(row.get("invalid_rows") or 0):
                    invalid = True
                else:
                    value += float(number)
                    entries.append({"category": category, "sales": float(number)})
            status = ("source_month_unavailable" if available_months is not None and key not in available_months else
                      "not_top_brand" if selected.empty else "invalid_sales" if invalid else
                      "no_categories" if not categories else "nonpositive_nso" if value <= 0 else "ok")
            result[key] = {"status": status, "nso": round(value, 2) if status == "ok" else None,
                           "categories": sorted(set(categories)), "category_sales": entries}
            if status == "source_month_unavailable":
                remarks.append(f"EC Consolidation数据源未覆盖{key}，无法计算费比；不能据此判断该品牌是否进入Top brands。")
            elif status == "not_top_brand":
                remarks.append(f"{brand}在{key}未进入Top brands，数据源未收录该品牌，因此无法计算该月份费比。")
            elif status == "no_categories":
                remarks.append(f"{brand}在{key}只有Total Beauty，缺少可用的其他category数据，因此无法计算费比。")
            elif status != "ok":
                remarks.append(f"{brand}在{key}的非Total Beauty sales缺失、异常或汇总不大于0，因此无法计算费比。")
            else:
                remarks.append(f"{key}的NSO包含：{'、'.join(sorted(set(categories)))}。")
        monthly[kind] = result
        missing = [m for m in expected if result[m]["status"] != "ok"]
        coverage[kind + "_months"] = [m for m in expected if m not in missing]
        coverage["missing_" + kind + "_months"] = missing
        totals[kind] = None if missing else round(sum(r["nso"] for r in result.values()), 2)
        if missing:
            remarks.append(f"{'本期' if kind == 'current' else '同期'}区间NSO覆盖不完整，整段期间的费比留空，不使用部分月份NSO计算。")
    status = "missing_current" if totals["current"] is None else "missing_prior" if totals["prior"] is None else "ok"
    return {"brand": brand, "matched_brand": brand, "platform": "TTL",
            "business_category": "TTL", "scope_note": BET_SCOPE_NOTE,
            "date_range": {"current": [focus_start, focus_end], "prior": [prior_start, prior_end]},
            "nso_actual": totals["current"], "nso_prior": totals["prior"],
            "evol": safe_evol(totals["current"], totals["prior"]) if status == "ok" else None,
            "comparison_status": status, "monthly": monthly, "coverage": coverage,
            "remarks": [NSO_RULE, *remarks]}


@tool
def query_ec_nso(brand: str, focus_start: str, focus_end: str, prior_start: str, prior_end: str) -> dict:
    """品牌整体NSO：所有平台、除Total Beauty外的Category sales，逐月校验覆盖。"""
    try:
        periods = [month_keys(start, end) for start, end in ((focus_start, focus_end), (prior_start, prior_end))]
        params = {"brand": brand}
        clauses = []
        for i, months in enumerate(periods):
            params[f"start_{i}"] = int(months[0].replace("-", ""))
            params[f"end_{i}"] = int(months[-1].replace("-", ""))
            clauses.append(f"(year * 100 + month BETWEEN :start_{i} AND :end_{i})")
        # Keep Total Beauty rows to distinguish absent brand-months from unusable categories.
        df = source_fetch(fetch_df, "top_brands_total_ec", "Brand", "Brand = :brand",
            """SELECT year, month, TRIM(Category) AS category, SUM(sales) AS nso,
                      SUM(CASE WHEN sales IS NULL THEN 1 ELSE 0 END) AS invalid_rows
               FROM top_brands_total_ec WHERE Brand = :brand AND (""" + " OR ".join(clauses) + """ )
               GROUP BY year, month, TRIM(Category)""", params)
        source_months = fetch_df("SELECT year, month FROM top_brands_total_ec WHERE " +
                                " OR ".join(clauses) + " GROUP BY year, month",
                                {k: v for k, v in params.items() if k != "brand"})
        available = {f"{int(r.year):04d}-{int(r.month):02d}" for r in source_months.itertuples()}
        return summarize_nso(df, brand, focus_start, focus_end, prior_start, prior_end, available)
    except BrandQueryError as exc:
        return {"error": exc.status, "message": f"品牌“{brand}”在NSO数据源的映射尚不可用，无法计算费比；这不代表该月份未进入Top brands。"}
    except Exception as exc:
        return {"error": "execution_error", "message": "NSO查询失败，无法计算费比；这不代表该月份未进入Top brands。", "detail": str(exc)}
