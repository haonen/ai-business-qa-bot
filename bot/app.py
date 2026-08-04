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
    graph.add_node("clarify_period", lambda s: _run_direct(s))
    graph.add_node("default_chain", lambda s: _run_direct(s))
    graph.add_node("media_analysis", lambda s: _run_direct(s))
    graph.add_node("filter_update", lambda s: _run_direct(s))
    graph.add_node("skill_dispatch", lambda s: _run_direct(s))
    graph.set_entry_point("router")
    graph.add_conditional_edges("router", route_condition, {
        "meta": "meta_reply",
        "caliber_reject": "caliber_reject",
        "guide": "guide",
        "clarify_period": "clarify_period",
        "default_chain": "default_chain",
        "media_analysis": "media_analysis",
        "filter_update": "filter_update",
        "skill_dispatch": "skill_dispatch",
    })
    for node in [
        "meta_reply", "caliber_reject", "guide", "clarify_period", "default_chain",
        "media_analysis", "filter_update", "skill_dispatch",
    ]:
        graph.add_edge(node, END)
    return graph.compile()


def run_agent(open_id: str, user_text: str, session: SessionState, on_progress=None) -> dict:
    state: AgentState = {"open_id": open_id, "user_text": user_text, "session": session}
    # Keep direct runner as source of truth so progress callbacks work consistently.
    return _run_direct(state, on_progress=on_progress)
