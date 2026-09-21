from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import os
import re
import time

from bot.followup_formatter import format_followup_result
from bot.followup_plan import (
    asks_strategy_or_external_cause,
    build_followup_plan,
    requests_web_search,
)
from bot.media_brand import resolve_source_brand
from bot.opportunity_insight import (
    asks_for_opportunity,
    format_opportunity_result,
    generate_opportunity_bullets,
    select_opportunity_candidates,
)
from bot.runtime_config import bounded_query_workers
from bot.session import SessionState
from bot.ec_report_evidence import evidence_from_cache
from bot.tools import (
    query_bet_followup_table,
    query_change_contribution,
    query_ec_bet_monthly,
    query_ec_followup_table,
)
from bot.tools.query_douyin_followup_table import (
    query_douyin_drill_bundle, query_douyin_followup_table,
)
from bot.tools.query_jd_business import query_jd_business


log = logging.getLogger(__name__)


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_evidence_row(row: dict, dimension: str | None = None) -> dict:
    value = dict(row)
    if dimension and dimension not in value:
        if dimension == "category":
            value[dimension] = value.get("category_cn") or value.get("category")
        elif dimension == "sku":
            value[dimension] = value.get("sku") or value.get("item_id")
    aliases = {
        "gmv_current": "gmv_actual", "gmv_change": "gmv_diff", "evol": "gmv_evol",
    }
    for source, target in aliases.items():
        if target not in value and source in value:
            value[target] = value.get(source)
    return value


def _ec_evidence(state: SessionState) -> dict:
    ctx = state.ec_context
    if isinstance(ctx.latest_report_evidence, dict) and ctx.latest_report_evidence:
        return ctx.latest_report_evidence
    if ctx.recent_evidence and isinstance(ctx.recent_evidence[-1], dict):
        candidate = ctx.recent_evidence[-1]
        if candidate.get("schema_version") == "ec-report-evidence.v2":
            return candidate
    return evidence_from_cache(
        ctx.report_cache or state.last_result_cache or {}, brand=ctx.brand,
        period=ctx.period, platform=ctx.platform,
    )


def _evidence_result(plan, state: SessionState, text: str = "") -> dict | None:
    """Return a standard result only when cached evidence covers the requested grain."""
    evidence = _ec_evidence(state)
    if not evidence:
        return None
    # Reports produced before the leaf-category policy contain inflated totals.
    # Query fresh product evidence instead of reusing those historical numbers.
    if str(plan.platform or "").upper() == "DY" and not any(
        source.get("product_scope") == "level4_nonempty_v1"
        for source in evidence.get("data_sources") or []
    ):
        return None
    if (
        str(evidence.get("brand") or "").casefold() != plan.brand.casefold()
        or str((evidence.get("period") or {}).get("raw") or "") != str(plan.period.get("raw") or "")
        or str(evidence.get("platform") or "").upper() != str(plan.platform or "").upper()
    ):
        return None
    refresh = any(token in str(text or "") for token in ("刷新", "最新", "重新查"))
    if refresh:
        return None
    group = plan.group_by[0] if len(plan.group_by) == 1 else None
    category = str(plan.filters.get("category") or "")
    driver = str(plan.filters.get("key_driver") or "")
    rows: list[dict] = []
    if group == "category":
        rows = [_normalize_evidence_row(row, "category") for row in evidence.get("categories") or []]
    elif group == "key_driver":
        rows = [_normalize_evidence_row(row, "key_driver") for row in evidence.get("key_drivers") or []]
    elif category and not plan.group_by:
        series = (evidence.get("series_scopes") or {}).get(category) or []
        products = (evidence.get("product_scopes") or {}).get(category) or []
        category_row = next((
            row for row in evidence.get("categories") or []
            if str(row.get("category") or row.get("category_cn") or "") == category
        ), None)
        if not category_row or not series or not products:
            return None
        summary = _normalize_evidence_row(category_row, "category")
        result = {
            "query_meta": {"domain": "ec", "platform": plan.platform, "source": "report_evidence"},
            "filters": plan.filters, "totals": summary, "rows": [summary], "missing": [],
            "tables": [
                {"title": "系列结构", "rows": [_normalize_evidence_row(row, "series") for row in series]},
                {"title": "Top商品标题", "rows": [_normalize_evidence_row(row, "sku") for row in products]},
            ],
        }
        result["evidence"] = [summary, *series, *products]
        return result
    elif driver and not plan.group_by:
        rows = [
            _normalize_evidence_row(row, "key_driver") for row in evidence.get("key_drivers") or []
            if str(row.get("key_driver") or "") == driver
        ]
        if not rows:
            return None
    elif not plan.group_by and not plan.filters:
        overall = _normalize_evidence_row(evidence.get("overall_metrics") or {})
        rows = [overall] if overall else []
    if not rows:
        return None
    return {
        "query_meta": {"domain": "ec", "platform": plan.platform, "source": "report_evidence"},
        "filters": plan.filters, "totals": {}, "rows": rows, "missing": [],
        "evidence": [{"evidence_id": f"cached_{index}", **row} for index, row in enumerate(rows, 1)],
    }


def _query_jd_followup(plan, aliases: list[str]) -> dict:
    unsupported = set(plan.group_by) & {"key_driver", "series", "sku"}
    if unsupported or any(plan.filters.get(key) for key in ("key_driver", "series", "function_tag")):
        return {
            "error": "unsupported_scope",
            "message": "京东当前只支持整体和品类追问，暂不支持Key Driver、系列或SKU下钻。",
        }
    result = query_jd_business(plan.brand, plan.period["raw"], brand_aliases=aliases)
    if result.get("error"):
        return result
    rows = [_normalize_evidence_row(row, "category") for row in result.get("all_categories") or result.get("categories") or []]
    category = str(plan.filters.get("category") or "")
    if category:
        rows = [row for row in rows if str(row.get("category") or "") == category]
    if not plan.group_by and not category:
        rows = [_normalize_evidence_row(result.get("brand_result") or {})]
    return {
        "query_meta": {"domain": "ec", "platform": "JD", "source": "jd_business"},
        "filters": plan.filters, "rows": rows, "totals": result.get("brand_result") or {},
        "missing": [], "evidence": [{"evidence_id": f"jd_{i}", **row} for i, row in enumerate(rows, 1)],
    }


def _input_metric_mismatch_note(text: str, plan, result: dict) -> str | None:
    match = re.search(r"([+\-]?\d+(?:\.\d+)?)\s*%", str(text or ""))
    if not match:
        return None
    metric = next(
        (name for name in ("spend_evol", "gmv_evol", "search_evol", "cost_evol") if name in plan.metrics),
        None,
    )
    if not metric:
        return None
    observed = next(
        (row.get(metric) for row in (result.get("rows") or []) if row.get(metric) is not None),
        None,
    )
    if observed is None:
        return None
    stated_pct = float(match.group(1))
    observed_pct = float(observed) * 100
    if abs(stated_pct - observed_pct) < 0.05:
        return None
    sign = "+" if observed_pct > 0 else ""
    return (
        f"你提到的{stated_pct:+.1f}%与当前同口径查询结果"
        f"{sign}{observed_pct:.1f}%不一致；以下分析以数据库本次查询结果为准，"
        "在口径核对前不使用用户给出的比例推断原因。"
    )


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
    with ThreadPoolExecutor(max_workers=bounded_query_workers(len(sources)), thread_name_prefix="followup-brand") as executor:
        tasks = {source: executor.submit(copy_context().run, resolve_source_brand, brand, source, brand_aliases=aliases) for source in sources}
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
    with ThreadPoolExecutor(max_workers=bounded_query_workers(len(dimensions) + 1), thread_name_prefix="ec-drill") as executor:
        tasks = {
            "summary": executor.submit(copy_context().run,
                query_ec_followup_table, plan.brand, plan.period["raw"], [],
                plan.filters, ["gmv_actual", "gmv_evol", "unit_actual", "unit_evol", "atv_actual"], 1, aliases,
            )
        }
        for dimension in dimensions:
            tasks[dimension] = executor.submit(copy_context().run,
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


def _run_opportunity_insight(
    text: str, state: SessionState, *, brand: str | None, period: str | None,
) -> dict | None:
    """Growth-opportunity synthesis over evidence a completed EC report already
    produced. Returns None when there is no cached report evidence to reason
    over, so the caller can fall back to the normal followup plan (which will
    surface a "missing brand/period" clarification instead of a silent guess)."""
    resolved_brand = brand or state.ec_context.brand or state.drilldown_ctx.brand
    resolved_period = period or state.ec_context.period or state.drilldown_ctx.period
    if not resolved_brand or not resolved_period:
        return None
    evidence = _ec_evidence(state)
    if not evidence or str(evidence.get("brand") or "").casefold() != str(resolved_brand).casefold():
        return {
            "markdown": (
                f"我还没有{resolved_brand}在{resolved_period}的生意报告证据，"
                "找不到增长机会点。请先让我生成一份完整的生意分析报告，我会在这份报告的基础上给建议。"
            ),
            "meta": {"document_ready": False, "report_type": "opportunity_insight", "domain": "ec"},
        }
    candidates = select_opportunity_candidates(evidence)
    bullets, used_llm = generate_opportunity_bullets(
        text, candidates, brand=resolved_brand, period=resolved_period,
    )
    result = format_opportunity_result(
        resolved_brand, resolved_period, evidence.get("platform"), candidates, bullets,
    )
    result["meta"]["used_llm"] = used_llm
    return result


def run_followup_v2_chain(
    text: str,
    state: SessionState,
    *,
    brand: str | None = None,
    period: str | None = None,
    brand_aliases: list[str] | None = None,
    on_progress=None,
) -> dict:
    started = time.perf_counter()
    if requests_web_search(text):
        return {
            "markdown": (
                "当前 Bot 的追问链路尚未启用联网搜索，不能核实该期间发生的外部事件。"
                "现有内部数据只能说明GMV、媒体花费和费比如何变化，不能据此证明公司采取了什么策略。"
            ),
            "meta": {
                "brand": brand,
                "period": period,
                "report_type": "followup_v2",
                "document_ready": False,
                "external_research_required": True,
            },
        }
    if asks_for_opportunity(text):
        opportunity_result = _run_opportunity_insight(text, state, brand=brand, period=period)
        if opportunity_result is not None:
            return opportunity_result
    try:
        plan = build_followup_plan(text, state, brand=brand, period=period)
    except ValueError as exc:
        if str(exc) == "missing_brand":
            return {"markdown": "你想分析哪个品牌？", "meta": {"document_ready": False, "awaiting": "brand"}}
        if str(exc) == "missing_period":
            return {"markdown": "你想分析哪个时间段？", "meta": {"document_ready": False, "awaiting": "period"}}
        if str(exc) == "missing_platform":
            return {
                "markdown": "这个追问要继续看天猫、抖音还是京东？",
                "meta": {"document_ready": False, "awaiting": "platform", "domain": "ec"},
            }
        return {"markdown": str(exc), "meta": {"document_ready": False}}
    plan_dict = plan.to_dict()
    inherited_context = state.bet_context if plan.domain == "bet" else state.ec_context
    inherited_aliases = (
        inherited_context.brand_aliases
        if str(inherited_context.brand or "").strip().casefold() == plan.brand.casefold()
        else []
    )
    aliases = list(dict.fromkeys([*(brand_aliases or []), *(inherited_aliases or [])]))
    source_brands, methods = {}, {}
    if plan.domain in {"bet", "ec_bet"}:
        sources = _required_sources(plan)
        source_brands, methods = _source_brands(plan.brand, aliases, sources)
    used_cached_evidence = False
    if plan.domain == "ec":
        result = _evidence_result(plan, state, text)
        used_cached_evidence = result is not None
    else:
        result = None
    if result is not None:
        pass
    elif plan.mode == "trend_alignment" or plan.domain == "ec_bet":
        result = query_ec_bet_monthly(plan.brand, plan.period["raw"], aliases, source_brands)
    elif plan.mode == "change_attribution":
        dimension = next((d for d in plan.group_by if d != "month"), None)
        if not dimension:
            dimension = "category" if plan.domain == "ec" else ("tier" if any(k in plan.filters for k in ("tier", "kol_type", "platform")) else "ait")
        result = query_change_contribution(plan.domain, plan.brand, plan.period["raw"], dimension, plan.filters, 50, aliases, source_brands)
    elif plan.domain == "ec":
        if plan.platform == "DY":
            if not _flag("DOUYIN_FOLLOWUP_TOOL_ENABLED"):
                result = {
                    "error": "unsupported_scope",
                    "message": "抖音追问工具尚未启用；已保留原报告范围，不会降级查询天猫数据。",
                }
            else:
                if on_progress:
                    on_progress(
                        f"已继承{plan.brand}、{plan.period['raw']}和抖音范围，"
                        "正在补充当前追问缺少的品类、系列与商品证据…"
                    )
                if plan.skill == "analysis_drill" and plan.mode in {"performance", "composition"} and not plan.group_by:
                    result = query_douyin_drill_bundle(plan.brand, plan.period["raw"], plan.filters, aliases)
                else:
                    result = query_douyin_followup_table(
                        plan.brand, plan.period["raw"], plan.group_by, plan.filters,
                        plan.metrics, 50, aliases,
                    )
        elif plan.platform == "JD":
            result = _query_jd_followup(plan, aliases)
        elif plan.platform == "TM":
            if plan.skill == "analysis_drill" and plan.mode in {"performance", "composition"} and not plan.group_by:
                result = _query_ec_drill_bundle(plan, aliases)
            else:
                result = query_ec_followup_table(plan.brand, plan.period["raw"], plan.group_by, plan.filters, plan.metrics, 50, aliases)
        else:
            result = {
                "error": "unsupported_scope",
                "message": "三平台汇总证据不支持直接品类或SKU下钻，请先指定天猫、抖音或京东。",
            }
    else:
        result = query_bet_followup_table(plan.brand, plan.period["raw"], plan.group_by, plan.filters, plan.metrics, 50, source_brands)
    result = _sort_and_limit(result, plan)
    formatted = format_followup_result(plan_dict, result)
    mismatch_note = _input_metric_mismatch_note(text, plan, result)
    if mismatch_note:
        formatted["markdown"] = f"{mismatch_note}\n\n{formatted['markdown']}"
        formatted["meta"]["input_metric_mismatch"] = True
    if asks_strategy_or_external_cause(text):
        formatted["markdown"] += (
            "\n\n### 判断边界\n\n"
            "内部数据可以确认费用规模及同比变化，但不能单凭费用与GMV同向增长，"
            "推断公司采用了具体营销策略。若要回答活动、代言、新品或平台投放动作，"
            "需要品牌官方信息或外部事件资料支持。"
        )
        formatted["meta"]["external_research_required"] = True
    elapsed = round(time.perf_counter() - started, 3)
    formatted["meta"].update({
        "plan": plan_dict, "source_brands": source_brands,
        "brand_match_methods": methods, "elapsed_seconds": elapsed,
        "platform": plan.platform, "evidence_reused": used_cached_evidence,
    })
    log.info("[followup_v2] plan=%s rows=%s missing=%s elapsed=%.3fs", plan_dict, len(result.get("rows") or []), result.get("missing"), elapsed)
    return formatted
