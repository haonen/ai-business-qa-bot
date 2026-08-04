from __future__ import annotations

import logging
import os
from typing import TypedDict

from bot.chains.default_chain import run_default_chain
from bot.chains.media_chain import run_media_chain
from bot.chains.skill_chain import run_filter_update
from bot.router import route
from bot.session import SessionState, set_cache, set_pending_request, update_context, update_domain_context
from bot.skills.loader import load_meta_answers
from bot.followup_plan import is_narrow_followup
from bot.chains.followup_v2_chain import run_followup_v2_chain
from bot.chains.market_chain import run_market_chain
from bot.market_plan import MarketPlan
from bot.session import update_market_context


log = logging.getLogger(__name__)


CALIBER_REJECT_TEXT = (
    "数据口径相关的问题（刷单打标规则、情报通与驾驶舱差异等）"
    "请联系数据团队确认，我这边按既定口径输出分析结果。"
)

GUIDE_TEXT = (
    "你可以这样问：谷雨2026年6月怎么样；或“分析2026年3月谷雨的媒体投资”。"
    "天猫生意追问可写“追问：T2的打法是什么”。"
)


class AgentState(TypedDict, total=False):
    open_id: str
    user_text: str
    session: SessionState
    route_type: str
    route: dict
    markdown: str
    meta: dict


def _run_direct(state: AgentState, on_progress=None) -> AgentState:
    open_id = state["open_id"]
    session = state["session"]
    route_result = route(state["user_text"], session)
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
            )
            log.info("[followup_v2_shadow] route=%s meta=%s", route_result.type, shadow.get("meta"))
        except Exception as exc:
            log.warning("[followup_v2_shadow] failed: %s", exc)

    if route_result.type == "meta":
        state["markdown"] = load_meta_answers()
        state["meta"] = {}
        return state
    if route_result.type == "caliber_reject":
        state["markdown"] = CALIBER_REJECT_TEXT
        state["meta"] = {}
        return state
    if route_result.type == "guide":
        state["markdown"] = GUIDE_TEXT
        state["meta"] = {}
        return state
    if route_result.type == "market_parameter_error":
        state["markdown"] = route_result.message or "大盘参数暂不支持。"
        state["meta"] = {"document_ready": False, "domain": "market"}
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
    if route_result.type == "clarify_market_period":
        set_pending_request(open_id, {
            "intent": "market_brand_ranking" if route_result.market_view == "top_brands" else "market_analysis",
            "segment": route_result.segment or "PURE MASS",
            "platform": route_result.platform or "TTL",
            "market_view": route_result.market_view or "summary",
            "ranking_metric": route_result.ranking_metric or "gmv_growth",
        })
        state["markdown"] = "你想看哪个时间段的大盘？例如：2026年1—6月，或2026年7月1日到7月10日。"
        state["meta"] = {"document_ready": False, "awaiting": "period", "domain": "market"}
        return state
    if route_result.type == "default_chain":
        set_pending_request(open_id, None)
        result = run_default_chain(
            route_result.brand or "",
            route_result.period or "",
            brand_aliases=route_result.brand_aliases,
            on_progress=on_progress,
        )
        state["markdown"] = result["markdown"]
        state["meta"] = result.get("meta", {})
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
        if state["meta"].get("last_result_cache"):
            set_cache(open_id, state["meta"]["last_result_cache"])
        update_domain_context(
            open_id, "ec", brand=state["meta"].get("brand"),
            period=state["meta"].get("period"), brand_aliases=state["meta"].get("brand_aliases") or [],
            filters={"category": state["meta"].get("selected_category"), "series": state["meta"].get("selected_series")},
            report_cache=state["meta"].get("last_result_cache"),
        )
        return state
    if route_result.type == "media_analysis":
        result = run_media_chain(
            route_result.brand or "",
            route_result.period,
            brand_aliases=route_result.brand_aliases,
            media_scope=route_result.media_scope,
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
            report_cache=state["meta"].get("last_result_cache"),
        )
        return state
    if route_result.type in {"market_analysis", "market_brand_ranking"}:
        set_pending_request(open_id, None)
        plan = MarketPlan(
            intent=route_result.type,
            period=route_result.period,
            segment=route_result.segment or "PURE MASS",
            platform=route_result.platform or "TTL",
            view=route_result.market_view or ("top_brands" if route_result.type == "market_brand_ranking" else "summary"),
            ranking_metric=route_result.ranking_metric or "gmv_growth",
        )
        result = run_market_chain(plan)
        state["markdown"] = result["markdown"]
        state["meta"] = result.get("meta", {})
        # 即使当前口径因数据覆盖不足而失败，也保留用户刚指定的时间和
        # Segment。这样用户紧接着说“那就看天猫Top 5”时可以继承原期间。
        update_market_context(
            open_id,
            period=route_result.period,
            segment=plan.segment,
            platform=plan.platform,
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
                source_brands=state["meta"].get("source_brands") or {},
                recent_evidence=state["meta"].get("evidence") or [],
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
        return state

    def route_condition(state: AgentState) -> str:
        return state.get("route_type", "guide")

    graph.add_node("router", router_node)
    graph.add_node("meta_reply", lambda s: {**s, "markdown": load_meta_answers(), "meta": {}})
    graph.add_node("caliber_reject", lambda s: {**s, "markdown": CALIBER_REJECT_TEXT, "meta": {}})
    graph.add_node("guide", lambda s: {**s, "markdown": GUIDE_TEXT, "meta": {}})
    graph.add_node("market_parameter_error", lambda s: _run_direct(s))
    graph.add_node("clarify_period", lambda s: _run_direct(s))
    graph.add_node("clarify_market_period", lambda s: _run_direct(s))
    graph.add_node("default_chain", lambda s: _run_direct(s))
    graph.add_node("media_analysis", lambda s: _run_direct(s))
    graph.add_node("market_analysis", lambda s: _run_direct(s))
    graph.add_node("market_brand_ranking", lambda s: _run_direct(s))
    graph.add_node("filter_update", lambda s: _run_direct(s))
    graph.add_node("skill_dispatch", lambda s: _run_direct(s))
    graph.set_entry_point("router")
    graph.add_conditional_edges("router", route_condition, {
        "meta": "meta_reply",
        "caliber_reject": "caliber_reject",
        "guide": "guide",
        "market_parameter_error": "market_parameter_error",
        "clarify_period": "clarify_period",
        "clarify_market_period": "clarify_market_period",
        "default_chain": "default_chain",
        "media_analysis": "media_analysis",
        "market_analysis": "market_analysis",
        "market_brand_ranking": "market_brand_ranking",
        "filter_update": "filter_update",
        "skill_dispatch": "skill_dispatch",
    })
    for node in [
        "meta_reply", "caliber_reject", "guide", "market_parameter_error", "clarify_period", "default_chain",
        "media_analysis", "market_analysis", "market_brand_ranking", "clarify_market_period", "filter_update", "skill_dispatch",
    ]:
        graph.add_edge(node, END)
    return graph.compile()


def run_agent(open_id: str, user_text: str, session: SessionState, on_progress=None) -> dict:
    state: AgentState = {"open_id": open_id, "user_text": user_text, "session": session}
    # Keep direct runner as source of truth so progress callbacks work consistently.
    return _run_direct(state, on_progress=on_progress)
