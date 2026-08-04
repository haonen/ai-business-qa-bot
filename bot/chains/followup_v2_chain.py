from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
import time

from bot.followup_formatter import format_followup_result
from bot.followup_plan import build_followup_plan
from bot.media_brand import resolve_source_brand
from bot.session import SessionState
from bot.tools import (
    query_bet_followup_table,
    query_change_contribution,
    query_ec_bet_monthly,
    query_ec_followup_table,
)


log = logging.getLogger(__name__)


def _sort_and_limit(result: dict, plan) -> dict:
    if result.get("error"):
        return result
    rows = list(result.get("rows") or [])
    metric = plan.sort.get("metric") or (plan.metrics[0] if plan.metrics else "")
    reverse = plan.sort.get("direction") != "asc"
    if plan.mode in {"ranking", "comparison", "change_attribution"} and metric and any(row.get(metric) is not None for row in rows):
        rows.sort(key=lambda row: (row.get(metric) is not None, float(row.get(metric) or 0)), reverse=reverse)
    result["rows"] = rows[:plan.limit]
    if not result.get("tables"):
        result["evidence"] = [{"evidence_id": f"row_{index}", **row} for index, row in enumerate(result["rows"], 1)]
    return result


def _source_brands(brand: str, aliases: list[str] | None, sources: tuple[str, ...]) -> tuple[dict, dict]:
    resolved, methods = {}, {}
    with ThreadPoolExecutor(max_workers=len(sources), thread_name_prefix="followup-brand") as executor:
        tasks = {source: executor.submit(resolve_source_brand, brand, source, brand_aliases=aliases) for source in sources}
        items = {source: task.result() for source, task in tasks.items()}
    for source, item in items.items():
        if item.get("error"):
            resolved[source] = None
            methods[source] = "not_found"
        else:
            resolved[source] = item.get("brand")
            methods[source] = item.get("match_method", "unknown")
    return resolved, methods


def _required_sources(plan) -> tuple[str, ...]:
    if plan.domain == "ec_bet":
        return ("search", "topline", "ksi", "nso", "tmall")
    if any(metric.startswith("search_") for metric in plan.metrics) or "category" in plan.group_by and plan.domain == "bet":
        return ("search",)
    if any(metric.startswith(("cost_", "engage_", "cpe")) for metric in plan.metrics) or any(dimension in plan.group_by for dimension in ("kol_platform", "tier", "kol_type", "kol")):
        return ("ksi",)
    sources = ["topline"]
    if any(metric in {"nso_actual", "nso_evol", "fee_ratio", "fee_ratio_change"} for metric in plan.metrics):
        sources.append("nso")
    return tuple(sources)


def _ec_bundle_dimensions(filters: dict) -> list[str]:
    if filters.get("category") and filters.get("key_driver"):
        return ["series", "sku"]
    if filters.get("category"):
        return ["key_driver", "series", "sku"]
    if filters.get("key_driver"):
        return ["category", "series", "sku"]
    if filters.get("series"):
        return ["category", "key_driver", "sku"]
    if filters.get("function_tag"):
        return ["category", "key_driver", "sku"]
    return []


def _query_ec_drill_bundle(plan, aliases: list[str]) -> dict:
    dimensions = _ec_bundle_dimensions(plan.filters)
    if not dimensions:
        return query_ec_followup_table(
            plan.brand, plan.period["raw"], plan.group_by, plan.filters,
            plan.metrics, 50, aliases,
        )
    with ThreadPoolExecutor(max_workers=len(dimensions) + 1, thread_name_prefix="ec-drill") as executor:
        tasks = {
            "summary": executor.submit(
                query_ec_followup_table, plan.brand, plan.period["raw"], [],
                plan.filters, ["gmv_actual", "gmv_evol", "unit_actual", "unit_evol", "atv_actual"], 1, aliases,
            )
        }
        for dimension in dimensions:
            tasks[dimension] = executor.submit(
                query_ec_followup_table, plan.brand, plan.period["raw"], [dimension],
                plan.filters, ["gmv_actual", "gmv_evol"], 20, aliases,
            )
        results = {name: task.result() for name, task in tasks.items()}
    summary = results.pop("summary")
    if summary.get("error"):
        return summary
    titles = {"category": "品类结构", "key_driver": "Key Driver结构", "series": "系列结构", "sku": "Top链接"}
    tables = [
        {"title": titles[dimension], "rows": result.get("rows") or [], "metrics": ["gmv_actual", "gmv_evol"]}
        for dimension, result in results.items() if not result.get("error")
    ]
    summary["tables"] = tables
    summary["evidence"] = [
        {"evidence_id": f"{dimension}_{index}", **row}
        for dimension, result in results.items()
        for index, row in enumerate(result.get("rows") or [], 1)
    ]
    summary["missing"] = [
        {"dimension": dimension, "message": result.get("message")}
        for dimension, result in results.items() if result.get("error")
    ]
    return summary


def run_followup_v2_chain(
    text: str,
    state: SessionState,
    *,
    brand: str | None = None,
    period: str | None = None,
    brand_aliases: list[str] | None = None,
) -> dict:
    started = time.perf_counter()
    try:
        plan = build_followup_plan(text, state, brand=brand, period=period)
    except ValueError as exc:
        if str(exc) == "missing_brand":
            return {"markdown": "你想分析哪个品牌？", "meta": {"document_ready": False, "awaiting": "brand"}}
        if str(exc) == "missing_period":
            return {"markdown": "你想分析哪个时间段？", "meta": {"document_ready": False, "awaiting": "period"}}
        return {"markdown": str(exc), "meta": {"document_ready": False}}
    plan_dict = plan.to_dict()
    inherited_aliases = (
        state.bet_context.brand_aliases
        if plan.domain == "bet"
        else state.ec_context.brand_aliases
    )
    aliases = list(dict.fromkeys([*(brand_aliases or []), *(inherited_aliases or [])]))
    source_brands, methods = {}, {}
    if plan.domain in {"bet", "ec_bet"}:
        sources = _required_sources(plan)
        source_brands, methods = _source_brands(plan.brand, aliases, sources)
    if plan.mode == "trend_alignment" or plan.domain == "ec_bet":
        result = query_ec_bet_monthly(plan.brand, plan.period["raw"], aliases, source_brands)
    elif plan.mode == "change_attribution":
        dimension = next((d for d in plan.group_by if d != "month"), None)
        if not dimension:
            dimension = "category" if plan.domain == "ec" else ("tier" if any(k in plan.filters for k in ("tier", "kol_type", "platform")) else "ait")
        result = query_change_contribution(plan.domain, plan.brand, plan.period["raw"], dimension, plan.filters, 50, aliases, source_brands)
    elif plan.domain == "ec":
        if plan.skill == "analysis_drill" and plan.mode in {"performance", "composition"} and not plan.group_by:
            result = _query_ec_drill_bundle(plan, aliases)
        else:
            result = query_ec_followup_table(plan.brand, plan.period["raw"], plan.group_by, plan.filters, plan.metrics, 50, aliases)
    else:
        result = query_bet_followup_table(plan.brand, plan.period["raw"], plan.group_by, plan.filters, plan.metrics, 50, source_brands)
    result = _sort_and_limit(result, plan)
    formatted = format_followup_result(plan_dict, result)
    elapsed = round(time.perf_counter() - started, 3)
    formatted["meta"].update({"plan": plan_dict, "source_brands": source_brands, "brand_match_methods": methods, "elapsed_seconds": elapsed})
    log.info("[followup_v2] plan=%s rows=%s missing=%s elapsed=%.3fs", plan_dict, len(result.get("rows") or []), result.get("missing"), elapsed)
    return formatted
