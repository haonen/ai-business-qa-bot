from __future__ import annotations

from bot.tools.common import tool
from bot.tools.query_bet_followup_table import query_bet_followup_table
from bot.tools.query_ec_followup_table import query_ec_followup_table


@tool
def query_change_contribution(
    domain: str,
    brand: str,
    period: str,
    dimension: str,
    filters: dict | None = None,
    limit: int = 20,
    brand_aliases=None,
    source_brands: dict | None = None,
) -> dict:
    """Rank positive growth contribution and negative drag from whitelisted dimensions."""
    if domain == "ec":
        if dimension not in {"category", "key_driver", "series"}:
            return {"error": "invalid_dimension", "message": "EC贡献分析仅支持品类、渠道或系列。"}
        result = query_ec_followup_table(brand, period, [dimension], filters, ["gmv_actual", "gmv_evol"], 50, brand_aliases)
        actual_key, prior_key = "gmv_actual", "gmv_prior"
    elif domain == "bet":
        if dimension not in {"ait", "bkfst", "tier", "kol_type"}:
            return {"error": "invalid_dimension", "message": "BET贡献分析仅支持AIT、BKFST、Tier或KOL Type。"}
        metrics = ["cost_actual"] if dimension in {"tier", "kol_type"} else ["spend_actual"]
        result = query_bet_followup_table(brand, period, [dimension], filters, metrics, 50, source_brands)
        actual_key, prior_key = (("cost_actual", "cost_prior") if dimension in {"tier", "kol_type"} else ("spend_actual", "spend_prior"))
    else:
        return {"error": "invalid_domain", "message": "贡献分析只支持EC或BET。"}
    if result.get("error"):
        return result
    rows = result.get("rows") or []
    if sum(int(row.get("_prior_rows") or 0) for row in rows) == 0:
        return {"error": "missing_prior", "message": "2025年同期无数据，无法计算变化贡献或下滑拖累。"}
    for row in rows:
        row["change_amount"] = float(row.get(actual_key) or 0) - float(row.get(prior_key) or 0)
    positive = sum(r["change_amount"] for r in rows if r["change_amount"] > 0)
    negative = sum(abs(r["change_amount"]) for r in rows if r["change_amount"] < 0)
    for row in rows:
        row["growth_contribution"] = row["change_amount"] / positive if row["change_amount"] > 0 and positive else None
        row["decline_drag"] = abs(row["change_amount"]) / negative if row["change_amount"] < 0 and negative else None
    rows.sort(key=lambda r: abs(r["change_amount"]), reverse=True)
    result["rows"] = rows[:max(1, min(int(limit), 50))]
    result["totals"].update({"positive_change": positive, "negative_change": -negative})
    result["evidence"] = [{"evidence_id": f"row_{i}", **row} for i, row in enumerate(result["rows"], 1)]
    return result
