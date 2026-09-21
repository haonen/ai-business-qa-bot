from __future__ import annotations

import logging
import os
import re
from dataclasses import asdict, replace
from datetime import date
from typing import TypedDict

from bot.chains.default_chain import run_default_chain
from bot.chains.brand_business_investment_chain import run_brand_business_investment_chain
from bot.chains.douyin_business_chain import run_douyin_business_chain
from bot.chains.jd_business_chain import run_jd_business_chain
from bot.chains.three_platform_competitor_chain import run_three_platform_competitor_chain
from bot.chains.media_chain import run_media_chain
from bot.chains.skill_chain import run_filter_update
from bot.router import route
from bot.session import (
    CLEAR, SET, SessionState, SlotUpdate, TaskContextPatch,
    apply_task_context_patch, get_session, set_cache, set_pending_request, update_active_plan,
    update_context, update_domain_context, update_task_context, publish_ec_report_context,
)
from bot.skills.loader import load_meta_answers
from bot.meta_capability import dynamic_meta_enabled, render_meta_answer
from bot.tools.query_data_availability import query_data_availability
from bot.followup_plan import is_narrow_followup
from bot.chains.followup_v2_chain import run_followup_v2_chain
from bot.chains.market_chain import run_market_chain
from bot.market_plan import MarketPlan
from bot.session import update_market_context
from bot.agent_plan import (
    agent_plan_enabled,
    agent_plan_shadow_enabled,
    compile_agent_plan,
    finalizing_progress,
    log_plan,
    planning_progress,
)
from bot.executable_plan import (
    can_execute_plan,
    ec_followup_plan_enabled,
    ec_followup_plan_shadow_enabled,
    executable_plan_enabled,
    executable_plan_shadow_enabled,
    execute_agent_plan,
)
from bot.ec_report_evidence import build_ec_report_evidence
from bot.semantic_analysis import reasoning_v3_enabled, reasoning_v3_shadow_enabled


log = logging.getLogger(__name__)


_EC_PENDING_INTENTS = {
    "TM": "default_analysis",
    "DY": "douyin_business_analysis",
    "JD": "jd_business_analysis",
    "TTL": "three_platform_competitor_analysis",
}


CALIBER_REJECT_TEXT = (
    "数据口径相关的问题（刷单打标规则、情报通与驾驶舱差异等）"
    "请联系数据团队确认，我这边按既定口径输出分析结果。"
)

GUIDE_TEXT = (
    "你可以这样问：谷雨2026年6月怎么样；“生成韩束2026年6月抖音生意分析报告”；"
    "“生成珀莱雅2026年6月京东品牌生意分析”；"
    "或“分析2026年3月谷雨的媒体投资”。"
    "要继续下钻时直接问“T2主要靠哪些品类”即可，不需要加特定前缀。"
)


def _meta_result(user_text: str, session: SessionState) -> dict:
    if dynamic_meta_enabled():
        return render_meta_answer(user_text, session)
    return {"markdown": load_meta_answers(), "meta": {"document_ready": False}}


def _persist_ec_report(open_id: str, meta: dict, platform: str) -> dict:
    """Atomically publish the latest completed EC report as follow-up evidence."""
    evidence = build_ec_report_evidence(
        meta, platform, task_id=getattr(getattr(get_session(open_id), "task_context", None), "task_id", None),
    )
    cache = dict(meta.get("last_result_cache") or {})
    if evidence:
        cache["ec_report_evidence"] = evidence
        meta["ec_report_evidence"] = evidence
        meta["last_result_cache"] = cache
    filters = {
        "category": meta.get("selected_category") or evidence.get("selected_category"),
        "series": meta.get("selected_series"),
    }
    publish_ec_report_context(
        open_id, evidence=evidence, cache=cache, brand=meta.get("brand"),
        period=meta.get("period"), platform=platform,
        brand_aliases=meta.get("brand_aliases") or [], filters=filters,
        source_brands={
            key: value for key, value in {
                "TM": meta.get("tmall_brand"), "DY": meta.get("douyin_brand"),
                "JD": meta.get("jd_brand"),
            }.items() if value
        },
    )
    return evidence


def _mark_ec_execution_started(
    open_id: str, *, brand: str | None, period: str | None,
    platform: str, original_question: str | None,
    brand_aliases: list[str] | None = None,
) -> None:
    """Keep the task identity while its template is executing."""
    from bot.session import _mutate_session
    def clear_report(ss):
        ctx=ss.ec_context
        ctx.brand=brand;ctx.period=period;ctx.platform=platform
        ctx.brand_aliases=list(brand_aliases or [])
        ctx.latest_report_evidence=None;ctx.recent_evidence=[];ctx.report_cache={}
        ctx.filters={};ctx.source_brands={};ss.last_result_cache={}
    _mutate_session(open_id,clear_report)
    apply_task_context_patch(open_id, TaskContextPatch(
        relation="EXECUTE_TASK",
        slots={
            "original_question": SlotUpdate(SET, original_question) if original_question else SlotUpdate(),
            "brand": SlotUpdate(SET, brand) if brand else SlotUpdate(),
            "brand_aliases": (
                SlotUpdate(SET, brand_aliases) if brand_aliases is not None else SlotUpdate()
            ),
            "period": SlotUpdate(SET, period) if period else SlotUpdate(),
            "platform": SlotUpdate(SET, platform),
            "rejected_period": SlotUpdate(CLEAR),
            "failure_kind": SlotUpdate(CLEAR),
            "latest_available_date": SlotUpdate(CLEAR),
            "awaiting_slot": SlotUpdate(CLEAR),
            "status": SlotUpdate(SET, "executing"),
        },
        add_intents=["EC_BUSINESS"], add_goals=["EC_BUSINESS"],
        reason_codes=["EC_TEMPLATE_EXECUTION_STARTED"],
    ))


def _apply_ec_execution_outcome(
    open_id: str, *, meta: dict, brand: str | None, period: str | None,
    platform: str, original_question: str | None,
    brand_aliases: list[str] | None = None,
) -> None:
    """Apply a structured terminal transition without erasing unaffected slots."""
    failure_kind = meta.get("failure_kind")
    retry_slot = meta.get("retry_slot")
    if not failure_kind:
        apply_task_context_patch(open_id, TaskContextPatch(
            relation="TASK_SUCCEEDED",
            slots={
                "brand": SlotUpdate(SET, meta.get("brand") or brand) if (meta.get("brand") or brand) else SlotUpdate(),
                "period": SlotUpdate(SET, meta.get("period") or period) if (meta.get("period") or period) else SlotUpdate(),
                "platform": SlotUpdate(SET, platform),
                "rejected_period": SlotUpdate(CLEAR),
                "failure_kind": SlotUpdate(CLEAR),
                "latest_available_date": SlotUpdate(CLEAR),
                "awaiting_slot": SlotUpdate(CLEAR),
                "status": SlotUpdate(SET, "active"),
            },
            reason_codes=["EC_TEMPLATE_EXECUTION_SUCCEEDED"],
        ))
        set_pending_request(open_id, None)
        return

    if retry_slot == "period":
        rejected = meta.get("requested_period") or period
        apply_task_context_patch(open_id, TaskContextPatch(
            relation="RECOVERABLE_DATA_FAILURE",
            slots={
                "brand": SlotUpdate(SET, brand) if brand else SlotUpdate(),
                "brand_aliases": (
                    SlotUpdate(SET, brand_aliases) if brand_aliases is not None else SlotUpdate()
                ),
                "period": SlotUpdate(CLEAR),
                "platform": SlotUpdate(SET, platform),
                "rejected_period": SlotUpdate(SET, rejected) if rejected else SlotUpdate(),
                "failure_kind": SlotUpdate(SET, failure_kind),
                "latest_available_date": (
                    SlotUpdate(SET, meta.get("latest_available_date"))
                    if meta.get("latest_available_date") else SlotUpdate(CLEAR)
                ),
                "awaiting_slot": SlotUpdate(SET, "period"),
                "status": SlotUpdate(SET, "awaiting"),
            },
            reason_codes=["INVALID_PERIOD_CLEARED", "EC_SCOPE_PRESERVED"],
        ))
        set_pending_request(open_id, {
            "intent": _EC_PENDING_INTENTS[platform],
            "original_text": original_question,
            "brand": brand,
            "brand_aliases": brand_aliases or [],
            "platform": platform,
        })
        return

    # Infrastructure/timeout failures keep every validated input so an exact
    # retry does not require another clarification. They never invent a latest date.
    apply_task_context_patch(open_id, TaskContextPatch(
        relation="TASK_FAILED_RETRYABLE",
        slots={
            "failure_kind": SlotUpdate(SET, failure_kind),
            "latest_available_date": SlotUpdate(CLEAR),
            "awaiting_slot": SlotUpdate(CLEAR),
            "status": SlotUpdate(SET, "failed_retryable"),
        },
        reason_codes=["EC_EXECUTION_FAILED_RETRYABLE", "EC_SCOPE_PRESERVED"],
    ))


class AgentState(TypedDict, total=False):
    open_id: str
    user_text: str
    session: SessionState
    route_type: str
    route: dict
    markdown: str
    meta: dict
    _route_result: object


def _confirm_ec_scope(state: AgentState):
    from bot.brand_query import enabled, QueryScope, use_query_scope
    if not enabled():
        return None
    from bot.brand_scope_confirmation import confirm_route, CATEGORIES
    from bot.session import _mutate_session
    route_result=state.get('_route_result') or route(state['user_text'],state['session'])
    state['_route_result']=route_result
    confirmation=confirm_route(route_result,state['session'],state['user_text'])
    if confirmation is not None:
        result,context=confirmation
        def save(ss):
            ss.brand_lookup_context=context
            if result is None:
                task=ss.task_context
                task.brand=context['input'];task.period=context['period'];task.platform=context['platform']
                task.category=context['context']['category'];task.awaiting_slot=None;task.status='active'
        _mutate_session(state['open_id'],save)
        state['session']=get_session(state['open_id'])
        if result is not None:
            state.update(markdown=result['markdown'],meta=result.get('meta',{}),route_type=route_result.type,route=route_result.to_dict())
            return state
    return None


def _run_direct(state: AgentState, on_progress=None) -> AgentState:
    from bot.brand_query import enabled, QueryScope, use_query_scope
    from bot.brand_scope_confirmation import CATEGORIES
    confirmation=_confirm_ec_scope(state)
    if confirmation is not None:return confirmation
    if not enabled():return _run_direct_unscoped(state,on_progress=on_progress)
    task=state['session'].task_context
    with use_query_scope(QueryScope(task.brand,CATEGORIES.get(task.category or ''),task.platform,str(task.period))):
        return _run_direct_unscoped(state,on_progress=on_progress)


def _run_direct_unscoped(state: AgentState, on_progress=None) -> AgentState:
    open_id = state["open_id"]
    session = state["session"]
    route_result = state.pop("_route_result", None) or route(state["user_text"], session)
    state["route"] = route_result.to_dict()
    state["route_type"] = route_result.type
    if (
        os.environ.get("FOLLOWUP_SKILL_V2_ENABLED", "1") != "1"
        and os.environ.get("FOLLOWUP_SKILL_V2_SHADOW", "0") == "1"
        and is_narrow_followup(state["user_text"])
        and route_result.type != "skill_dispatch"
    ):
        try:
            shadow = run_followup_v2_chain(
                state["user_text"], session,
                brand=route_result.brand, period=route_result.period,
                brand_aliases=route_result.brand_aliases,
                on_progress=None,
            )
            log.info("[followup_v2_shadow] route=%s meta=%s", route_result.type, shadow.get("meta"))
        except Exception as exc:
            log.warning("[followup_v2_shadow] failed: %s", exc)

    if route_result.type == "meta":
        result = _meta_result(state["user_text"], session)
        state["markdown"] = result["markdown"]
        state["meta"] = result.get("meta", {})
        return state
    if route_result.type == "data_availability":
        result = query_data_availability(state["user_text"], session)
        state["markdown"] = result["markdown"]
        state["meta"] = result.get("meta", {})
        return state
    if route_result.type == "caliber_reject":
        state["markdown"] = CALIBER_REJECT_TEXT
        state["meta"] = {}
        return state
    if route_result.type == "guide":
        state["markdown"] = GUIDE_TEXT
        state["meta"] = {}
        return state
    if route_result.type == "opportunity_analysis":
        fallback_type = {
            "TM": "default_chain", "DY": "douyin_business_analysis",
            "JD": "jd_business_analysis", "TTL": "three_platform_competitor_analysis",
        }.get(str(route_result.platform or "").upper())
        if not fallback_type:
            state["markdown"] = "已保留品牌和时间，但当前平台无法执行生意机会分析。"
            state["meta"] = {"document_ready": False, "fallback_reason": "unsupported_platform"}
            return state
        route_result.type = fallback_type
        state["route_type"] = fallback_type
        state["_route_result"] = route_result
        return _run_direct(state, on_progress=on_progress)
    if route_result.type == "market_parameter_error":
        state["markdown"] = route_result.message or "大盘参数暂不支持。"
        state["meta"] = {"document_ready": False, "domain": "market"}
        return state
    if route_result.type == "unsupported_scope":
        state["markdown"] = route_result.message or "当前数据工具不支持这个参数组合；我不会忽略参数后改用其他口径执行。"
        state["meta"] = {
            "document_ready": False, "unsupported_scope": True,
            "route_decision": route_result.route_decision or {},
        }
        return state
    if route_result.type in {
        "confirm_current_year", "provide_campaign_window", "confirm_brand_candidate",
    }:
        decision = route_result.route_decision or {}
        entity = decision.get("entity_resolution") or {}
        existing = session.pending_request or {}
        pending_intent = route_result.type
        pending = {
            "intent": pending_intent,
            "original_text": entity.get("text") or existing.get("original_text") or state["user_text"],
        }
        if pending_intent == "confirm_current_year":
            mentions = ((entity.get("time_scope") or {}).get("mentions") or [])
            canonical = (mentions[0].get("canonical") if mentions else "") or ""
            match = re.search(r"20\d{2}", canonical)
            pending["proposed_year"] = int(match.group(0)) if match else date.today().year
        if pending_intent == "confirm_brand_candidate":
            pending["candidates"] = ((entity.get("brand") or {}).get("candidates") or existing.get("candidates") or [])
        set_pending_request(open_id, pending)
        state["markdown"] = route_result.message or "请补充或确认实体信息。"
        state["meta"] = {
            "document_ready": False,
            "awaiting": pending_intent,
            "route_decision": decision,
        }
        return state
    if route_result.type == "clarify_time_roles":
        set_pending_request(open_id, None)
        state["markdown"] = route_result.message or "请明确主分析期和对比期。"
        state["meta"] = {"document_ready": False, "awaiting": "time_roles"}
        return state
    if route_result.type == "clarify_media_scope":
        decision = route_result.route_decision or {}
        set_pending_request(open_id, {
            "intent": "v2_media_scope",
            "intents": decision.get("intents") or ["BET"],
            "brand": route_result.brand, "period": route_result.period,
            "platform": route_result.platform, "question_mode": route_result.question_mode,
        })
        state["markdown"] = route_result.message or "请确认要看整体BET，还是只看抖音相关BET。"
        state["meta"] = {
            "document_ready": False, "awaiting": "media_scope", "domain": "bet",
            "route_decision": decision,
        }
        return state
    if route_result.type == "clarify_v2_period":
        decision = route_result.route_decision or {}
        set_pending_request(open_id, {
            "intent": "v2_period", "intents": decision.get("intents") or ["BET"],
            "brand": route_result.brand, "platform": route_result.platform,
            "media_mode": route_result.media_mode, "media_channels": route_result.media_channels or [],
            "question_mode": route_result.question_mode,
        })
        state["markdown"] = f"你想分析{route_result.brand or '这个品牌'}的哪个时间段？请一次回复具体月份、季度或起止日期。"
        state["meta"] = {"document_ready": False, "awaiting": "period", "route_decision": decision}
        return state
    if route_result.type == "clarify_v2_brand":
        decision = route_result.route_decision or {}
        entity = decision.get("entity_resolution") or {}
        existing = session.pending_request or {}
        set_pending_request(open_id, {
            "intent": "v2_brand", "intents": decision.get("intents") or ["EC_BUSINESS"],
            "period": route_result.period, "platform": route_result.platform,
            "media_mode": route_result.media_mode, "media_channels": route_result.media_channels or [],
            "question_mode": route_result.question_mode,
            "original_text": entity.get("text") or existing.get("original_text") or state["user_text"],
            "time_scope": route_result.time_scope or existing.get("time_scope"),
        })
        state["markdown"] = "请提供需要分析的品牌名称。"
        state["meta"] = {"document_ready": False, "awaiting": "brand", "route_decision": decision}
        return state
    if route_result.type == "clarify_market_scope":
        decision = route_result.route_decision or {}
        original_text = route_result.original_text or state["user_text"]
        intents = decision.get("intents") or ["MARKET"]
        set_pending_request(open_id, {
            "intent": "v2_market_scope",
            "original_text": original_text,
            "intents": intents,
            "preflight_target": (
                "market_brand_deep_dive"
                if route_result.ranking_metric and (
                    "BET" in intents
                    or route_result.question_mode in {"strategy", "explanation"}
                    or any(token in original_text for token in (
                        "再往下", "选品", "生意节奏", "增长最多的平台",
                    ))
                ) else None
            ),
            "include_bet": "BET" in intents,
            "bet_latest_ytd": "BET_LATEST_YTD_REQUESTED" in (decision.get("reason_codes") or []),
            "period": route_result.period,
            "platform": route_result.platform, "segment": route_result.segment,
            "category": route_result.category, "question_mode": route_result.question_mode,
            "ranking_metric": route_result.ranking_metric, "ranking_limit": route_result.ranking_limit,
        })
        state["markdown"] = route_result.message or "请补充完整的大盘口径。"
        state["meta"] = {
            "document_ready": False, "awaiting": "market_scope", "domain": "market",
            "route_decision": decision,
        }
        return state
    if route_result.type == "clarify_analysis_scope":
        set_pending_request(open_id, {
            "intent": "analysis_preflight",
            "original_text": route_result.original_text or state["user_text"],
            "target": route_result.preflight_target,
            "brand": route_result.brand,
            "period": route_result.period,
            "brand_aliases": route_result.brand_aliases or [],
            "platform": route_result.platform,
            "segment": route_result.segment,
            "market_view": route_result.market_view,
            "ranking_metric": route_result.ranking_metric,
            "ranking_limit": route_result.ranking_limit,
            "media_mode": route_result.media_mode,
            "media_channels": route_result.media_channels or [],
            "question_mode": route_result.question_mode,
            "task_bindings": route_result.task_bindings or [],
            "include_bet": route_result.include_bet,
            "bet_latest_ytd": route_result.bet_latest_ytd,
        })
        state["markdown"] = route_result.message or "请确认分析范围后再开始查询。"
        state["meta"] = {
            "brand": route_result.brand,
            "period": route_result.period,
            "platform": route_result.platform,
            "document_ready": False,
            "awaiting": "analysis_scope_confirmation",
        }
        return state
    if route_result.type == "clarify_period":
        set_pending_request(open_id, {
            "intent": "default_analysis",
            "brand": route_result.brand,
            "brand_aliases": route_result.brand_aliases or [],
        })
        state["markdown"] = (
            f"你想分析{route_result.brand or '这个品牌'}的哪个时间段？"
            "例如：2026年6月、2026年1—6月，或2026年7月1日到7月19日。"
        )
        state["meta"] = {
            "brand": route_result.brand,
            "period": None,
            "document_ready": False,
            "awaiting": "period",
        }
        return state
    if route_result.type == "clarify_business_platform":
        set_pending_request(open_id, {
            "intent": "brand_business_investment_analysis",
            "brand": route_result.brand,
            "period": route_result.period,
            "brand_aliases": route_result.brand_aliases or [],
        })
        state["markdown"] = (
            f"你想看{route_result.brand or '这个品牌'}在哪个平台的生意和主推商品？"
            "请回复：天猫、抖音或京东。BET媒体投资会同时按同一期间输出。"
        )
        state["meta"] = {
            "brand": route_result.brand,
            "period": route_result.period,
            "document_ready": False,
            "awaiting": "platform",
            "domain": "business_bet",
        }
        return state
    if route_result.type == "clarify_ec_platform":
        set_pending_request(open_id, {
            "intent": "business_platform_selection",
            "brand": route_result.brand,
            "period": route_result.period,
            "brand_aliases": route_result.brand_aliases or [],
            "original_text": route_result.original_text or state["user_text"],
            "include_bet": (
                route_result.include_bet
                or bool(re.search(r"bet|媒体投资|媒体花费|投资情况|投资表现|投放", state["user_text"], re.I))
            ),
        })
        state["markdown"] = (
            f"你想分析{route_result.brand or '这个品牌'}在{route_result.period or '指定时间段'}的哪个平台生意？"
            "请回复：三平台、天猫、抖音或京东。"
        )
        state["meta"] = {
            "brand": route_result.brand,
            "period": route_result.period,
            "document_ready": False,
            "awaiting": "business_platform",
            "domain": "ec_business",
        }
        return state
    if route_result.type == "clarify_douyin_period":
        set_pending_request(open_id, {
            "intent": "douyin_business_analysis",
            "brand": route_result.brand,
            "brand_aliases": route_result.brand_aliases or [],
        })
        state["markdown"] = (
            f"你想分析{route_result.brand or '这个品牌'}的哪个抖音生意时间段？"
            "例如：2026年6月，或2026年7月1日到7月19日。"
        )
        state["meta"] = {
            "brand": route_result.brand,
            "period": None,
            "document_ready": False,
            "awaiting": "period",
            "domain": "douyin_business",
        }
        return state
    if route_result.type == "clarify_jd_period":
        set_pending_request(open_id, {
            "intent": "jd_business_analysis",
            "brand": route_result.brand,
            "brand_aliases": route_result.brand_aliases or [],
        })
        state["markdown"] = (
            f"你想分析{route_result.brand or '这个品牌'}的哪个京东生意时间段？"
            "例如：2026年6月，或2026年7月1日到7月19日。"
        )
        state["meta"] = {
            "brand": route_result.brand,
            "period": None,
            "document_ready": False,
            "awaiting": "period",
            "domain": "jd_business",
        }
        return state
    if route_result.type in {"clarify_three_platform_brand", "clarify_three_platform_period"}:
        set_pending_request(open_id, {
            "intent": "three_platform_competitor_analysis",
            "brand": route_result.brand,
            "period": route_result.period,
            "brand_aliases": route_result.brand_aliases or [],
        })
        missing_brand = route_result.type == "clarify_three_platform_brand"
        state["markdown"] = (
            "请提供需要分析的品牌名称，例如：按三平台生意分析模板，分析珀莱雅2026年Q2。"
            if missing_brand else
            f"你想分析{route_result.brand or '这个品牌'}的哪个时间段？例如：2026年Q2或2026年7月1日至15日。"
        )
        state["meta"] = {
            "brand": route_result.brand,
            "period": route_result.period,
            "document_ready": False,
            "awaiting": "brand" if missing_brand else "period",
            "domain": "three_platform_competitor",
        }
        return state
    if route_result.type == "clarify_market_period":
        set_pending_request(open_id, {
            "intent": route_result.type if route_result.type in {"market_brand_ranking", "market_brand_deep_dive"} else "market_analysis",
            "segment": route_result.segment or "PURE MASS",
            "platform": route_result.platform or "TTL",
            "market_view": route_result.market_view or "summary",
            "ranking_metric": route_result.ranking_metric or "gmv_actual",
            "ranking_limit": route_result.ranking_limit or 5,
        })
        state["markdown"] = "你想看哪个时间段的大盘？例如：2026年1—6月，或2026年7月1日到7月10日。"
        state["meta"] = {"document_ready": False, "awaiting": "period", "domain": "market"}
        return state
    if route_result.type == "default_chain":
        set_pending_request(open_id, None)
        _mark_ec_execution_started(
            open_id, brand=route_result.brand, period=route_result.period,
            platform="TM", original_question=route_result.original_text or state["user_text"],
            brand_aliases=route_result.brand_aliases,
        )
        result = run_default_chain(
            route_result.brand or "",
            route_result.period or "",
            brand_aliases=route_result.brand_aliases,
            on_progress=on_progress,
        )
        state["markdown"] = result["markdown"]
        state["meta"] = result.get("meta", {})
        _apply_ec_execution_outcome(
            open_id, meta=state["meta"], brand=route_result.brand,
            period=route_result.period, platform="TM",
            original_question=route_result.original_text or state["user_text"],
            brand_aliases=route_result.brand_aliases,
        )
        if not state["meta"].get("failure_kind"):
            update_context(
                open_id,
                brand=state["meta"].get("brand"),
                tmall_brand=state["meta"].get("tmall_brand"),
                brand_aliases=state["meta"].get("brand_aliases"),
                period=state["meta"].get("period"),
                category=state["meta"].get("selected_category"),
                series=state["meta"].get("selected_series"),
                last_analysis_view="default_analysis",
            )
        if not state["meta"].get("failure_kind") and state["meta"].get("last_result_cache"):
            _persist_ec_report(open_id, state["meta"], "TM")
        return state
    if route_result.type == "brand_business_investment_analysis":
        set_pending_request(open_id, None)
        result = run_brand_business_investment_chain(
            route_result.brand or "",
            route_result.period or "",
            route_result.platform or "",
            business_period=route_result.business_period,
            bet_period=route_result.bet_period,
            brand_aliases=route_result.brand_aliases,
            media_mode=route_result.media_mode,
            media_channels=route_result.media_channels,
            on_progress=on_progress,
        )
        state["markdown"] = result["markdown"]
        state["meta"] = result.get("meta", {})
        if state["meta"].get("last_result_cache"):
            set_cache(open_id, state["meta"]["last_result_cache"])
        return state
    if route_result.type == "douyin_business_analysis":
        set_pending_request(open_id, None)
        _mark_ec_execution_started(
            open_id, brand=route_result.brand, period=route_result.period,
            platform="DY", original_question=route_result.original_text or state["user_text"],
            brand_aliases=route_result.brand_aliases,
        )
        result = run_douyin_business_chain(
            route_result.brand or "",
            route_result.period or "",
            brand_aliases=route_result.brand_aliases,
            on_progress=on_progress,
        )
        state["markdown"] = result["markdown"]
        state["meta"] = result.get("meta", {})
        _apply_ec_execution_outcome(
            open_id, meta=state["meta"], brand=route_result.brand,
            period=route_result.period, platform="DY",
            original_question=route_result.original_text or state["user_text"],
            brand_aliases=route_result.brand_aliases,
        )
        if not state["meta"].get("failure_kind"):
            update_context(
                open_id, brand=state["meta"].get("brand"),
                brand_aliases=state["meta"].get("brand_aliases"),
                period=state["meta"].get("period"),
                category=state["meta"].get("selected_category"),
                last_analysis_view="douyin_business_analysis",
            )
        if not state["meta"].get("failure_kind") and state["meta"].get("last_result_cache"):
            _persist_ec_report(open_id, state["meta"], "DY")
        return state
    if route_result.type == "jd_business_analysis":
        set_pending_request(open_id, None)
        _mark_ec_execution_started(
            open_id, brand=route_result.brand, period=route_result.period,
            platform="JD", original_question=route_result.original_text or state["user_text"],
            brand_aliases=route_result.brand_aliases,
        )
        result = run_jd_business_chain(
            route_result.brand or "",
            route_result.period or "",
            brand_aliases=route_result.brand_aliases,
            on_progress=on_progress,
        )
        state["markdown"] = result["markdown"]
        state["meta"] = result.get("meta", {})
        _apply_ec_execution_outcome(
            open_id, meta=state["meta"], brand=route_result.brand,
            period=route_result.period, platform="JD",
            original_question=route_result.original_text or state["user_text"],
            brand_aliases=route_result.brand_aliases,
        )
        if not state["meta"].get("failure_kind"):
            update_context(
                open_id, brand=state["meta"].get("brand"),
                brand_aliases=state["meta"].get("brand_aliases"),
                period=state["meta"].get("period"),
                category=state["meta"].get("selected_category"),
                last_analysis_view="jd_business_analysis",
            )
        if not state["meta"].get("failure_kind") and state["meta"].get("last_result_cache"):
            _persist_ec_report(open_id, state["meta"], "JD")
        return state
    if route_result.type in {"three_platform_competitor_analysis", "brand_platform_deep_dive"}:
        set_pending_request(open_id, None)
        _mark_ec_execution_started(
            open_id, brand=route_result.brand, period=route_result.period,
            platform="TTL", original_question=route_result.original_text or state["user_text"],
            brand_aliases=route_result.brand_aliases,
        )
        result = run_three_platform_competitor_chain(
            route_result.brand or "",
            route_result.period or "",
            on_progress=on_progress,
        )
        state["markdown"] = result["markdown"]
        state["meta"] = result.get("meta", {})
        _apply_ec_execution_outcome(
            open_id, meta=state["meta"], brand=route_result.brand,
            period=route_result.period, platform="TTL",
            original_question=route_result.original_text or state["user_text"],
            brand_aliases=route_result.brand_aliases,
        )
        if not state["meta"].get("failure_kind"):
            update_context(
                open_id, brand=state["meta"].get("brand"),
                period=state["meta"].get("period"),
                last_analysis_view="three_platform_competitor_analysis",
            )
        if not state["meta"].get("failure_kind") and state["meta"].get("last_result_cache"):
            _persist_ec_report(open_id, state["meta"], "TTL")
        return state
    if route_result.type == "media_analysis":
        # A resolved BET request supersedes any earlier clarification. Leaving
        # stale pending state here can redirect the next period-only reply to EC.
        set_pending_request(open_id, None)
        result = run_media_chain(
            route_result.brand or "",
            route_result.period,
            brand_aliases=route_result.brand_aliases,
            media_scope=route_result.media_scope,
            media_mode=route_result.media_mode,
            media_channels=route_result.media_channels,
            on_progress=on_progress,
        )
        state["markdown"] = result["markdown"]
        state["meta"] = result.get("meta", {})
        update_context(
            open_id,
            brand=state["meta"].get("brand"),
            period=state["meta"].get("period"),
            last_analysis_view="media_analysis",
        )
        if state["meta"].get("last_result_cache"):
            set_cache(open_id, state["meta"]["last_result_cache"])
        update_domain_context(
            open_id, "bet", brand=state["meta"].get("brand"),
            period=state["meta"].get("period"), brand_aliases=route_result.brand_aliases or [],
            source_brands=state["meta"].get("resolved_brands") or {},
            filters={
                "media_mode": route_result.media_mode or "OVERALL_BET",
                "media_channels": list(route_result.media_channels or []),
            },
            report_cache=state["meta"].get("last_result_cache"),
        )
        return state
    if route_result.type in {"market_analysis", "market_brand_ranking", "market_brand_deep_dive"}:
        set_pending_request(open_id, None)
        plan = MarketPlan(
            intent=route_result.type,
            period=route_result.period,
            segment=route_result.segment or "PURE MASS",
            platform=route_result.platform or "TTL",
            category=route_result.category or "TOTAL BEAUTY",
            view=route_result.market_view or ("top_brands" if route_result.type == "market_brand_ranking" else "summary"),
            ranking_metric=route_result.ranking_metric or "gmv_actual",
            ranking_limit=route_result.ranking_limit or 5,
            include_bet=route_result.include_bet,
            bet_latest_ytd=route_result.bet_latest_ytd,
        )
        result = run_market_chain(plan, on_progress=on_progress)
        state["markdown"] = result["markdown"]
        state["meta"] = result.get("meta", {})
        # 即使当前口径因数据覆盖不足而失败，也保留用户刚指定的时间和
        # Segment。这样用户紧接着说“那就看天猫Top 5”时可以继承原期间。
        update_market_context(
            open_id,
            period=route_result.period,
            segment=plan.segment,
            platform=plan.platform,
            category=plan.category,
            last_view=plan.view,
        )
        if result.get("ok"):
            update_market_context(
                open_id, recent_result=state["meta"].get("market_result"),
                top_brands=state["meta"].get("top_brands") or [],
            )
        return state
    if route_result.type == "filter_update":
        update = route_result.update.__dict__ if route_result.update else {}
        update_context(open_id, **update)
        result = run_filter_update(session, update)
        state["markdown"] = result["markdown"]
        state["meta"] = result.get("meta", {})
        return state
    if route_result.type == "skill_dispatch":
        result = run_followup_v2_chain(
            route_result.followup_text or "", session,
            brand=route_result.brand, period=route_result.period,
            brand_aliases=route_result.brand_aliases,
            on_progress=on_progress,
        )
        state["markdown"] = result["markdown"]
        state["meta"] = result.get("meta", {})
        if state["meta"].get("awaiting"):
            set_pending_request(open_id, {
                "intent": "followup_v2",
                "awaiting": state["meta"]["awaiting"],
                "brand": route_result.brand,
                "period": route_result.period,
                "brand_aliases": route_result.brand_aliases or [],
                "followup_text": route_result.followup_text or state["user_text"],
            })
        else:
            set_pending_request(open_id, None)
        domain = state["meta"].get("domain")
        if domain in {"ec", "bet"}:
            update_domain_context(
                open_id, domain, brand=state["meta"].get("brand"),
                period=state["meta"].get("period"),
                platform=state["meta"].get("platform"),
                source_brands=state["meta"].get("source_brands") or {},
                filters=(state["meta"].get("plan") or {}).get("filters") or {},
                recent_evidence=state["meta"].get("evidence") or [],
            )
            followup_filters = (state["meta"].get("plan") or {}).get("filters") or {}
            update_context(
                open_id,
                brand=state["meta"].get("brand"),
                period=state["meta"].get("period"),
                category=followup_filters.get("category"),
                series=followup_filters.get("series"),
                key_driver=followup_filters.get("key_driver"),
                last_analysis_view=f"{domain}_followup",
            )
        return state

    state["markdown"] = GUIDE_TEXT
    state["meta"] = {}
    return state


def build_graph():
    try:
        from langgraph.graph import END, StateGraph
    except Exception:
        return None

    graph = StateGraph(AgentState)

    def router_node(state: AgentState):
        session = state["session"]
        result = route(state["user_text"], session)
        state["route"] = result.to_dict()
        state["route_type"] = result.type
        state["_route_result"] = result
        return state

    def route_condition(state: AgentState) -> str:
        return state.get("route_type", "guide")

    graph.add_node("router", router_node)
    graph.add_node("meta_reply", lambda s: {
        **s, **_meta_result(s["user_text"], s["session"]),
    })
    graph.add_node("data_availability", lambda s: _run_direct(s))
    graph.add_node("caliber_reject", lambda s: {**s, "markdown": CALIBER_REJECT_TEXT, "meta": {}})
    graph.add_node("guide", lambda s: {**s, "markdown": GUIDE_TEXT, "meta": {}})
    graph.add_node("opportunity_analysis", lambda s: _run_direct(s))
    graph.add_node("market_parameter_error", lambda s: _run_direct(s))
    graph.add_node("unsupported_scope", lambda s: _run_direct(s))
    graph.add_node("confirm_current_year", lambda s: _run_direct(s))
    graph.add_node("provide_campaign_window", lambda s: _run_direct(s))
    graph.add_node("confirm_brand_candidate", lambda s: _run_direct(s))
    graph.add_node("clarify_time_roles", lambda s: _run_direct(s))
    graph.add_node("clarify_media_scope", lambda s: _run_direct(s))
    graph.add_node("clarify_v2_period", lambda s: _run_direct(s))
    graph.add_node("clarify_v2_brand", lambda s: _run_direct(s))
    graph.add_node("clarify_market_scope", lambda s: _run_direct(s))
    graph.add_node("clarify_analysis_scope", lambda s: _run_direct(s))
    graph.add_node("clarify_period", lambda s: _run_direct(s))
    graph.add_node("clarify_business_platform", lambda s: _run_direct(s))
    graph.add_node("clarify_ec_platform", lambda s: _run_direct(s))
    graph.add_node("clarify_douyin_period", lambda s: _run_direct(s))
    graph.add_node("clarify_jd_period", lambda s: _run_direct(s))
    graph.add_node("clarify_three_platform_brand", lambda s: _run_direct(s))
    graph.add_node("clarify_three_platform_period", lambda s: _run_direct(s))
    graph.add_node("clarify_market_period", lambda s: _run_direct(s))
    graph.add_node("default_chain", lambda s: _run_direct(s))
    graph.add_node("brand_business_investment_analysis", lambda s: _run_direct(s))
    graph.add_node("douyin_business_analysis", lambda s: _run_direct(s))
    graph.add_node("jd_business_analysis", lambda s: _run_direct(s))
    graph.add_node("three_platform_competitor_analysis", lambda s: _run_direct(s))
    graph.add_node("media_analysis", lambda s: _run_direct(s))
    graph.add_node("market_analysis", lambda s: _run_direct(s))
    graph.add_node("market_brand_ranking", lambda s: _run_direct(s))
    graph.add_node("market_brand_deep_dive", lambda s: _run_direct(s))
    graph.add_node("brand_platform_deep_dive", lambda s: _run_direct(s))
    graph.add_node("filter_update", lambda s: _run_direct(s))
    graph.add_node("skill_dispatch", lambda s: _run_direct(s))
    graph.set_entry_point("router")
    graph.add_conditional_edges("router", route_condition, {
        "meta": "meta_reply",
        "data_availability": "data_availability",
        "caliber_reject": "caliber_reject",
        "guide": "guide",
        "opportunity_analysis": "opportunity_analysis",
        "market_parameter_error": "market_parameter_error",
        "unsupported_scope": "unsupported_scope",
        "confirm_current_year": "confirm_current_year",
        "provide_campaign_window": "provide_campaign_window",
        "confirm_brand_candidate": "confirm_brand_candidate",
        "clarify_time_roles": "clarify_time_roles",
        "clarify_media_scope": "clarify_media_scope",
        "clarify_v2_period": "clarify_v2_period",
        "clarify_v2_brand": "clarify_v2_brand",
        "clarify_market_scope": "clarify_market_scope",
        "clarify_analysis_scope": "clarify_analysis_scope",
        "clarify_period": "clarify_period",
        "clarify_business_platform": "clarify_business_platform",
        "clarify_ec_platform": "clarify_ec_platform",
        "clarify_douyin_period": "clarify_douyin_period",
        "clarify_jd_period": "clarify_jd_period",
        "clarify_three_platform_brand": "clarify_three_platform_brand",
        "clarify_three_platform_period": "clarify_three_platform_period",
        "clarify_market_period": "clarify_market_period",
        "default_chain": "default_chain",
        "brand_business_investment_analysis": "brand_business_investment_analysis",
        "douyin_business_analysis": "douyin_business_analysis",
        "jd_business_analysis": "jd_business_analysis",
        "three_platform_competitor_analysis": "three_platform_competitor_analysis",
        "media_analysis": "media_analysis",
        "market_analysis": "market_analysis",
        "market_brand_ranking": "market_brand_ranking",
        "market_brand_deep_dive": "market_brand_deep_dive",
        "brand_platform_deep_dive": "brand_platform_deep_dive",
        "filter_update": "filter_update",
        "skill_dispatch": "skill_dispatch",
    })
    for node in [
        "meta_reply", "caliber_reject", "guide", "opportunity_analysis", "market_parameter_error", "unsupported_scope", "confirm_current_year", "provide_campaign_window", "confirm_brand_candidate", "clarify_time_roles", "clarify_media_scope", "clarify_v2_period", "clarify_v2_brand", "clarify_market_scope", "clarify_analysis_scope", "clarify_period", "clarify_business_platform", "clarify_ec_platform", "clarify_douyin_period", "clarify_jd_period", "clarify_three_platform_brand", "clarify_three_platform_period", "default_chain", "brand_business_investment_analysis", "douyin_business_analysis", "jd_business_analysis", "three_platform_competitor_analysis",
        "media_analysis", "market_analysis", "market_brand_ranking", "market_brand_deep_dive", "brand_platform_deep_dive", "clarify_market_period", "filter_update", "skill_dispatch",
    ]:
        graph.add_edge(node, END)
    return graph.compile()


from bot.brand_request_cache import request_cached

@request_cached(fresh=True)
def run_agent(open_id: str, user_text: str, session: SessionState, on_progress=None, *, received_at=None) -> dict:
    from bot.conversation_control import handle_control
    controlled = handle_control(open_id, user_text, session)
    if controlled is not None:
        return controlled
    from bot.execution_policy import enabled as optimized_execution
    from bot.category_market_lookup import try_category_lookup
    category_answer = None if optimized_execution() else try_category_lookup(open_id, user_text, session, received_at)
    if category_answer is not None:
        return category_answer
    from bot.market_lookup import try_lookup
    market_answer = None if optimized_execution() else try_lookup(open_id, user_text, session, received_at)
    if market_answer is not None:
        return market_answer
    # A short category answer completes the explicit pending request, not an
    # older report in ec_context. Consume it before routing/planning followups.
    from bot.brand_scope_confirmation import pending_category_route
    pending_route = pending_category_route(session, user_text)
    if pending_route is not None:
        return _run_direct({
            "open_id": open_id, "user_text": user_text, "session": session,
            "_route_result": pending_route,
        }, on_progress=on_progress)
    # Route exactly once.  The controlled plan compiles from this immutable
    # decision; existing chains remain the only execution adapters in V1.
    before_slots = asdict(session.task_context)
    route_result = route(user_text, session, received_at=received_at)
    if optimized_execution():
        from bot.execution_policy import dispatch_atomic, clear_atomic_contexts
        atomic = (route_result.route_decision or {}).get("atomic_request")
        if atomic is not None:
            return dispatch_atomic(atomic, open_id, user_text, session, received_at)
        # Preserve task scope for general ranking/followups, but do not retain
        # an unrelated atomic conversation that can capture later short replies.
        if (not route_result.type.startswith(("clarify", "confirm"))
                and route_result.type not in {"guide", "meta"}):
            clear_atomic_contexts(open_id, session)
    from bot.brand_scope_confirmation import merge_route_category
    merge_route_category(route_result, session, user_text)
    route_decision = route_result.route_decision or {}
    # A new/cancelled task invalidates the separate category clarification.
    if route_decision.get("relation") in {"NEW_TASK", "CANCEL"}:
        from bot.session import set_brand_lookup_context
        set_brand_lookup_context(open_id, {})
        session.brand_lookup_context = {}
    patch_payload = route_decision.get("task_context_patch") or {}
    task_payload = route_decision.get("task_context") or {}
    if patch_payload:
        slots = {}
        for key, instruction in (patch_payload.get("slot_ops") or {}).items():
            op = str((instruction or {}).get("op") or "KEEP").upper()
            slots[key] = SlotUpdate(
                op, (instruction or {}).get("value"),
                source=(instruction or {}).get("source"),
                span=(instruction or {}).get("span"),
                turn_id=(instruction or {}).get("turn_id"),
            )
        apply_task_context_patch(open_id, TaskContextPatch(
            relation=patch_payload.get("relation") or "NEW_TASK",
            slots=slots,
            add_intents=list(patch_payload.get("intents") or []),
            add_goals=list(patch_payload.get("goals") or []),
            current_turn=user_text,
            reason_codes=list(patch_payload.get("reason_codes") or []),
        ))
    elif task_payload:
        update_task_context(
            open_id, **task_payload,
            relation=task_payload.get("last_relation") or "CONTINUE_TASK",
            current_turn=user_text,
        )
    elif route_result.type in {
        "default_chain", "douyin_business_analysis", "jd_business_analysis",
        "three_platform_competitor_analysis", "brand_platform_deep_dive",
        "brand_business_investment_analysis", "media_analysis", "market_analysis",
        "market_brand_ranking", "market_brand_deep_dive",
    }:
        # V1 compatibility: a directly executable complete request must also
        # establish the active task; otherwise the next elliptical follow-up
        # can inherit an older clarification frame.
        intents = (
            ["MARKET", *( ["BET"] if route_result.include_bet else [])]
            if route_result.type.startswith("market_")
            else ["BET"] if route_result.type == "media_analysis"
            else ["EC_BUSINESS", "BET"]
            if route_result.type == "brand_business_investment_analysis"
            else ["EC_BUSINESS"]
        )
        values = {
            "original_question": route_result.original_text or user_text,
            "brand": route_result.brand, "brand_aliases": route_result.brand_aliases or [],
            "period": route_result.period, "platform": route_result.platform,
            "media_mode": route_result.media_mode,
            "media_channels": route_result.media_channels or [],
            "segment": route_result.segment, "category": route_result.category,
            "comparison_metric": route_result.ranking_metric,
            "awaiting_slot": None, "status": "active",
        }
        apply_task_context_patch(open_id, TaskContextPatch(
            relation="SUPPLY_MISSING_SLOT" if session.pending_request else "NEW_TASK",
            slots={
                key: SlotUpdate(CLEAR if value is None else SET, value)
                for key, value in values.items()
                if value is not None or key == "awaiting_slot"
            },
            add_intents=intents, add_goals=intents, current_turn=user_text,
            reason_codes=["LEGACY_ROUTE_ESTABLISHED_TASK"],
        ))
    state: AgentState = {
        "open_id": open_id,
        "user_text": user_text,
        "session": session,
        "_route_result": route_result,
    }
    confirmation=_confirm_ec_scope(state)
    if confirmation is not None:return confirmation
    session=state['session']
    # New brand requests and their slot replies must share the same adapter.
    plan = None
    reasoning_shadow_plan = None
    plan_error = None
    if (
        agent_plan_enabled() or agent_plan_shadow_enabled()
        or executable_plan_enabled() or executable_plan_shadow_enabled()
        or ec_followup_plan_enabled() or ec_followup_plan_shadow_enabled()
        or reasoning_v3_enabled() or reasoning_v3_shadow_enabled()
    ):
        try:
            # A confirmation such as "确认" must not erase the dimensions in
            # the original long question. The route carries that wording forward.
            plan_text = route_result.original_text or user_text
            plan = compile_agent_plan(route_result, plan_text)
            log_plan(plan, shadow=not agent_plan_enabled())
            if (
                reasoning_v3_shadow_enabled() and not reasoning_v3_enabled()
                and route_decision.get("shadow_route_type") == "opportunity_analysis"
            ):
                shadow_route = replace(
                    route_result, type="opportunity_analysis",
                    response_strategy="MULTI_STEP_ANALYSIS",
                )
                reasoning_shadow_plan = compile_agent_plan(shadow_route, plan_text)
                log_plan(reasoning_shadow_plan, shadow=True)
        except Exception as exc:
            # The plan layer is a rollout boundary. A compiler or validator
            # problem must not take down the existing production answer path.
            log.exception("controlled agent plan compilation failed: %s", exc)
            plan = None
            if os.environ.get("LEGACY_PIPELINE_EMERGENCY", "0").strip().lower() not in {
                "1", "true", "yes", "on",
            }:
                plan_error = str(exc)

    if plan_error:
        return {
            "route_type": route_result.type,
            "route": route_result.to_dict(),
            "markdown": "我已经理解了你的分析范围，但暂时无法生成通过安全校验的执行计划。当前会话范围已保留，请稍后重试。",
            "meta": {
                "document_ready": False,
                "pipeline_stage": "plan",
                "pipeline_error": plan_error,
                "response_strategy": route_result.response_strategy,
            },
        }

    if agent_plan_enabled() and plan and on_progress and not can_execute_plan(plan):
        message = planning_progress(plan)
        if message:
            on_progress(message)

    if plan and can_execute_plan(plan):
        standard_ec = route_result.type in {'default_chain','douyin_business_analysis','jd_business_analysis','three_platform_competitor_analysis'}
        ec_last_meta = {}
        def report_event(event, platform, brand, period, report):
            if not standard_ec:return
            if (brand,str(period),platform)!=(route_result.brand,str(route_result.period),route_result.platform):
                raise ValueError('Formal report scope differs from confirmed task')
            kwargs=dict(brand=brand,period=period,platform=platform,original_question=route_result.original_text or user_text,brand_aliases=route_result.brand_aliases)
            if event=='start':
                set_pending_request(open_id,None)
                _mark_ec_execution_started(open_id,**kwargs)
                return
            meta=dict((report or {}).get('meta') or {})
            if not (report or {}).get('ok',True):meta.setdefault('failure_kind','EC_TEMPLATE_FAILED')
            ec_last_meta.clear();ec_last_meta.update(meta)
            _apply_ec_execution_outcome(open_id,meta=meta,**kwargs)
            if not meta.get('failure_kind') and meta.get('last_result_cache'):
                meta.setdefault('brand',brand);meta.setdefault('period',period)
                _persist_ec_report(open_id,meta,platform)
                report['meta']=meta
        try:
            from bot.brand_query import enabled, QueryScope, use_query_scope
            from contextlib import nullcontext
            from bot.brand_scope_confirmation import CATEGORIES
            task=get_session(open_id).task_context
            guard=use_query_scope(QueryScope(task.brand,CATEGORIES.get(task.category or ''),
                task.platform,str(task.period))) if enabled() else nullcontext()
            from bot.report_events import listen
            with guard, listen(report_event):
                result = execute_agent_plan(plan, on_progress=on_progress, session=session)
            result["route_type"] = route_result.type
            result["route"] = route_result.to_dict()
            meta = result.setdefault("meta", {})
            execution = meta.get("plan_execution") or {}
            steps = list(execution.get("steps") or [])
            selected = list(meta.get("selected_platform_by_brand") or [])
            market_result = meta.get("market_result") or {}
            update_active_plan(
                open_id,
                plan_id=plan.plan_id,
                original_question=plan_text,
                goal=plan.title,
                entities={
                    "brand": route_result.brand,
                    "period": route_result.period,
                    "segment": route_result.segment,
                    "category": route_result.category,
                    "platform": route_result.platform,
                },
                status=execution.get("status") or "success",
                completed_steps=[row["step_id"] for row in steps if row.get("status") in {"success", "partial"}],
                failed_steps=[row["step_id"] for row in steps if row.get("status") == "failed"],
                top_brands=list(meta.get("top_brands") or []),
                platform_matrix=list(market_result.get("rows") or []),
                selected_platforms=selected,
                pending_slots=[],
            )
            if route_result.type == "market_brand_deep_dive":
                update_market_context(
                    open_id,
                    period=route_result.period,
                    segment=route_result.segment or "PURE MASS",
                    platform=route_result.platform or "TTL",
                    category=route_result.category or "TOTAL BEAUTY",
                    last_view="top_brands",
                    recent_result=market_result,
                    top_brands=list(meta.get("top_brands") or []),
                )
            elif route_result.type == "skill_dispatch":
                followup_meta = result.get("meta") or {}
                domain = followup_meta.get("domain")
                filters = (followup_meta.get("plan") or {}).get("filters") or {}
                if domain in {"ec", "bet"}:
                    update_domain_context(
                        open_id, domain, brand=followup_meta.get("brand"),
                        period=followup_meta.get("period"), platform=followup_meta.get("platform"),
                        filters=filters, recent_evidence=followup_meta.get("evidence") or [],
                    )
                    update_context(
                        open_id, brand=followup_meta.get("brand"), period=followup_meta.get("period"),
                        category=filters.get("category"), series=filters.get("series"),
                        key_driver=filters.get("key_driver"), last_analysis_view=f"{domain}_followup",
                    )
            elif route_result.type == "opportunity_analysis":
                opportunity_meta = result.get("meta") or {}
                evidence = dict(opportunity_meta.get("ec_report_evidence") or {})
                cache = dict(opportunity_meta.get("last_result_cache") or {})
                if evidence:
                    cache["ec_report_evidence"] = evidence
                    publish_ec_report_context(
                        open_id, evidence=evidence, cache=cache,
                        brand=opportunity_meta.get("brand") or route_result.brand,
                        period=opportunity_meta.get("period") or route_result.period,
                        platform=opportunity_meta.get("platform") or route_result.platform,
                        brand_aliases=route_result.brand_aliases or [],
                        filters={}, source_brands={},
                    )
                update_context(
                    open_id, brand=opportunity_meta.get("brand") or route_result.brand,
                    period=opportunity_meta.get("period") or route_result.period,
                    brand_aliases=route_result.brand_aliases or [],
                    last_analysis_view="opportunity_analysis",
                )
            elif not standard_ec:
                update_context(
                    open_id, brand=route_result.brand, period=route_result.period,
                    brand_aliases=route_result.brand_aliases or [],
                    last_analysis_view="brand_platform_deep_dive",
                )
                if meta.get("last_result_cache"):
                    set_cache(open_id, meta["last_result_cache"])
        except Exception as exc:
            log.exception("executable agent plan failed: %s", exc)
            if os.environ.get("LEGACY_PIPELINE_EMERGENCY", "0").strip().lower() in {
                "1", "true", "yes", "on",
            }:
                result = _run_direct(state, on_progress=on_progress)
                result.setdefault("meta", {})["executable_plan_fallback"] = str(exc)
            else:
                result = {
                    "ok": False,
                    "markdown": "分析计划执行暂时失败，当前会话范围已保留，请稍后重试。",
                    "meta": {
                        "document_ready": False,
                        **ec_last_meta,
                        "pipeline_stage": "execution",
                        "pipeline_error": str(exc),
                    },
                }
    else:
        result = _run_direct(state, on_progress=on_progress)
    result.setdefault("meta", {})["response_strategy"] = route_result.response_strategy
    if plan and (
        agent_plan_enabled() or executable_plan_enabled() or ec_followup_plan_enabled()
        or reasoning_v3_enabled()
    ):
        result.setdefault("meta", {})["agent_plan"] = plan.to_dict()
        message = None if can_execute_plan(plan) else finalizing_progress(plan)
        if message and on_progress:
            on_progress(message)
    if reasoning_shadow_plan:
        result.setdefault("meta", {})["agent_reasoning_v3_shadow"] = reasoning_shadow_plan.to_dict()
    # Compose the V3 trace from validated route, plan and evidence objects.
    # This keeps explicit SET/KEEP/CLEAR decisions and reasoning provenance
    # inspectable without logging database rows or secrets.
    route_payload = result.get("route") or route_result.to_dict()
    decision_payload = route_payload.get("route_decision") or {}
    trace = decision_payload.setdefault("trace", {})
    trace.update({
        "original_question": route_result.original_text or user_text,
        "before_slots": before_slots,
        "explicit_slot_patch": (route_decision.get("task_context_patch") or {}).get("slot_ops") or {},
        "after_slots": asdict(get_session(open_id).task_context),
    })
    if plan and plan.semantic_request:
        trace.update({
            "semantic_goals": list(plan.semantic_request.get("analysis_goals") or []),
            "selected_capabilities": list(plan.semantic_request.get("selected_capabilities") or []),
            "selected_skills": list(plan.semantic_request.get("selected_skills") or []),
            "plan_steps": [step.to_dict() for step in plan.steps],
        })
    if reasoning_shadow_plan and reasoning_shadow_plan.semantic_request:
        trace["reasoning_v3_shadow"] = {
            "semantic_goals": list(
                reasoning_shadow_plan.semantic_request.get("analysis_goals") or []
            ),
            "selected_capabilities": list(
                reasoning_shadow_plan.semantic_request.get("selected_capabilities") or []
            ),
            "selected_skills": list(
                reasoning_shadow_plan.semantic_request.get("selected_skills") or []
            ),
            "plan_steps": [step.to_dict() for step in reasoning_shadow_plan.steps],
        }
    result_meta = result.get("meta") or {}
    evidence = result_meta.get("ec_report_evidence") or {}
    if evidence:
        trace["evidence_manifest"] = evidence.get("manifest") or {}
    for key in ("evidence_requests", "used_evidence_ids", "validation_result"):
        if key in result_meta:
            trace[key] = result_meta.get(key)
    if result_meta.get("executable_plan_fallback"):
        trace["fallback_reason"] = result_meta.get("executable_plan_fallback")
    decision_payload["trace"] = trace
    route_payload["route_decision"] = decision_payload
    result["route"] = route_payload
    return result
