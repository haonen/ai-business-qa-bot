from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from bot.db.connection import fetch_df

from bot.media_period import parse_media_period
from bot.tools.common import tool
from bot.tools.followup_common import month_keys, standard_result
from bot.tools.query_bet_followup_table import query_bet_followup_table
from bot.tools.ecip_ttl_gmv_blend import query_blended_tmall_ttl_gmv
from bot.utils import safe_evol


def _query_tmall_ttl_monthly(source_brand: str | None, parsed) -> dict:
    if not source_brand:
        return {"error": "source_unavailable", "message": "缺少天猫TTL GMV品牌映射。", "rows": []}
    blended = query_blended_tmall_ttl_gmv(
        fetch_df,
        brand=source_brand,
        current_start=parsed.focus_start,
        current_end=parsed.focus_end,
        prior_start=parsed.prior_start,
        prior_end=parsed.prior_end,
    )
    lookup = {}
    for raw in blended["rows"]:
        month = str(raw["source_month"])
        aligned = month if raw["period_key"] == "current" else f"{int(month[:4]) + 1:04d}{month[4:]}"
        lookup[(str(raw["period_key"]), aligned)] = float(raw["gmv"] or 0)
    rows = []
    for month in month_keys(parsed.focus_start, parsed.focus_end):
        actual, prior = lookup.get(("current", month)), lookup.get(("prior", month))
        rows.append({"month": month, "gmv_actual": actual, "gmv_prior": prior, "gmv_evol": safe_evol(actual, prior) if prior else None})
    return {
        "rows": rows,
        "coverage": {
            "current_months": sorted({m for (kind, m) in lookup if kind == "current"}),
            "prior_months": sorted({m for (kind, m) in lookup if kind == "prior"}),
            **blended["coverage"],
        },
    }


@tool
def query_ec_bet_monthly(
    brand: str,
    period: str,
    brand_aliases=None,
    source_brands: dict | None = None,
) -> dict:
    """Align monthly EC and BET evidence; returns signals, never causal claims."""
    try:
        parsed = parse_media_period(period)
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="followup-link") as executor:
            tasks = {
                "ec": executor.submit(_query_tmall_ttl_monthly, (source_brands or {}).get("tmall"), parsed),
                "media": executor.submit(query_bet_followup_table, brand, period, ["month"], {}, ["spend_actual", "spend_evol", "nso_actual", "nso_evol", "fee_ratio", "fee_ratio_change"], 50, source_brands),
                "search": executor.submit(query_bet_followup_table, brand, period, ["month"], {}, ["search_actual", "search_evol"], 50, source_brands),
                "kol": executor.submit(query_bet_followup_table, brand, period, ["month"], {}, ["cost_actual", "cost_evol", "engage_actual", "engage_evol", "cpe"], 50, source_brands),
            }
            results = {name: task.result() for name, task in tasks.items()}
        months = month_keys(parsed.focus_start, parsed.focus_end)
        lookups = {name: {row.get("month"): row for row in result.get("rows", [])} for name, result in results.items()}
        rows = []
        for month in months:
            row = {"month": month}
            for name in ("ec", "media", "search", "kol"):
                row.update({k: v for k, v in lookups[name].get(month, {}).items() if k not in {"month", "_total"}})
            ec_evol, spend_evol, search_evol = row.get("gmv_evol"), row.get("spend_evol"), row.get("search_evol")
            if ec_evol is not None and spend_evol is not None:
                if ec_evol > 0 and spend_evol > 0:
                    row["alignment_signal"] = "投入与站内生意同向增长"
                elif ec_evol < 0 and spend_evol < 0:
                    row["alignment_signal"] = "投入与站内生意同向下滑"
                elif spend_evol > 0 and ec_evol <= 0:
                    row["alignment_signal"] = "投入增长但站内生意未同步"
                else:
                    row["alignment_signal"] = "投入与站内生意变化背离"
            if search_evol is not None and ec_evol is not None and search_evol > 0 and ec_evol <= 0:
                row["search_signal"] = "搜索增长但GMV未同步"
            rows.append(row)
        return standard_result(
            query_meta={"domain": "ec_bet", "brand": brand, "period": period, "group_by": ["month"]},
            filters={}, totals={}, rows=rows,
            coverage={"requested_months": months, "sources": {name: result.get("coverage", {}) for name, result in results.items()}},
            missing=[{"source": name, "error": result.get("error"), "message": result.get("message")} for name, result in results.items() if result.get("error")],
        )
    except Exception as exc:
        return {"error": "execution_error", "message": str(exc)}
