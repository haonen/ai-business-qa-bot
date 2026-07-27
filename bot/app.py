from __future__ import annotations

from typing import TypedDict

from bot.chains.default_chain import run_default_chain
from bot.chains.skill_chain import run_filter_update, run_skill_chain
from bot.router import route
from bot.session import SessionState, set_cache, update_context
from bot.skills.loader import load_meta_answers


CALIBER_REJECT_TEXT = (
    "数据口径相关的问题（刷单打标规则、情报通与驾驶舱差异等）"
    "请联系数据团队确认，我这边按既定口径输出分析结果。"
)

GUIDE_TEXT = "你可以这样问：谷雨618怎么样。追问请以“追问：”开头，例如“追问：T2的打法是什么”。"


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
    if route_result.type == "default_chain":
        result = run_default_chain(route_result.brand or "", route_result.period or "618", on_progress=on_progress)
        state["markdown"] = result["markdown"]
        state["meta"] = result.get("meta", {})
        update_context(
            open_id,
            brand=state["meta"].get("brand"),
            period=state["meta"].get("period"),
            category=state["meta"].get("selected_category"),
            series=state["meta"].get("selected_series"),
            last_analysis_view="default_analysis",
        )
        if state["meta"].get("last_result_cache"):
            set_cache(open_id, state["meta"]["last_result_cache"])
        return state
    if route_result.type == "filter_update":
        update = route_result.update.__dict__ if route_result.update else {}
        update_context(open_id, **update)
        result = run_filter_update(session, update)
        state["markdown"] = result["markdown"]
        state["meta"] = result.get("meta", {})
        return state
    if route_result.type == "skill_dispatch":
        result = run_skill_chain(route_result.followup_text or "", session)
        state["markdown"] = result["markdown"]
        state["meta"] = result.get("meta", {})
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
    graph.add_node("default_chain", lambda s: _run_direct(s))
    graph.add_node("filter_update", lambda s: _run_direct(s))
    graph.add_node("skill_dispatch", lambda s: _run_direct(s))
    graph.set_entry_point("router")
    graph.add_conditional_edges("router", route_condition, {
        "meta": "meta_reply",
        "caliber_reject": "caliber_reject",
        "guide": "guide",
        "default_chain": "default_chain",
        "filter_update": "filter_update",
        "skill_dispatch": "skill_dispatch",
    })
    for node in ["meta_reply", "caliber_reject", "guide", "default_chain", "filter_update", "skill_dispatch"]:
        graph.add_edge(node, END)
    return graph.compile()


def run_agent(open_id: str, user_text: str, session: SessionState, on_progress=None) -> dict:
    state: AgentState = {"open_id": open_id, "user_text": user_text, "session": session}
    # Keep direct runner as source of truth so progress callbacks work consistently.
    return _run_direct(state, on_progress=on_progress)
