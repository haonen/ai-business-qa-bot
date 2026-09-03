from __future__ import annotations

"""Semantic turn relation and deterministic task-slot merge.

The LLM may classify how a turn relates to the active task, but entities are
accepted only from the existing deterministic resolvers and allow-listed values.
"""

from dataclasses import dataclass, field
from datetime import date
import json
import logging
import os
from typing import Any

from bot.business_analysis_spec import build_business_analysis_spec
from bot.entity_resolution import build_comparison_spec, resolve_entities
from bot.market_plan import explicit_platform
from bot.router_v2 import build_route_decision
from bot.session import BusinessTaskContext, SessionState
from bot.utils import extract_json_object, llm_client


log = logging.getLogger(__name__)

RELATIONS = {"NEW_TASK", "SUPPLY_MISSING_SLOT", "MODIFY_SCOPE", "ADD_GOAL", "FOLLOW_UP_RESULT"}
GOALS = {
    "EC_BUSINESS", "PRODUCT_ANALYSIS", "BET", "COMPARE_PLATFORMS",
    "DRILL_SELECTED_PLATFORM", "MARKET_ANALYSIS",
}
COMPARISON_METRICS = {"gmv_actual", "gmv_growth", "evol", "growth_contribution"}


@dataclass
class TaskTurnResolution:
    relation: str
    brand: str | None
    brand_aliases: list[str]
    period: str | None
    platform: str | None
    intents: list[str]
    goals: list[str]
    comparison_metric: str | None = None
    time_scope: dict = field(default_factory=dict)
    comparison_spec: dict = field(default_factory=dict)
    business_spec: dict = field(default_factory=dict)
    media_mode: str | None = None
    media_channels: list[str] = field(default_factory=list)
    original_question: str | None = None
    combined_question: str | None = None
    missing_slots: list[str] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)


def _frame_from_state(state: SessionState) -> BusinessTaskContext:
    frame = state.task_context
    pending = state.pending_request or {}
    if frame.awaiting_slot or not pending:
        return frame
    # Backward-compatible hydration for sessions created before Task Context V2.
    awaiting = {
        "business_platform_selection": "platform",
        "brand_business_investment_analysis": "platform",
        "v2_brand": "brand", "v2_period": "period",
        "default_analysis": "period", "douyin_business_analysis": "period",
        "jd_business_analysis": "period",
    }.get(str(pending.get("intent") or ""))
    if not awaiting:
        return frame
    intents = list(pending.get("intents") or [])
    if not intents:
        intents = (
            ["EC_BUSINESS", "BET"]
            if pending.get("intent") == "brand_business_investment_analysis"
            else ["EC_BUSINESS"]
        )
    if pending.get("include_bet") and "BET" not in intents:
        intents.append("BET")
    legacy_platform = pending.get("platform") or {
        "default_analysis": "TM",
        "douyin_business_analysis": "DY",
        "jd_business_analysis": "JD",
    }.get(str(pending.get("intent") or ""))
    return BusinessTaskContext(
        original_question=pending.get("original_text"), brand=pending.get("brand"),
        brand_aliases=list(pending.get("brand_aliases") or []), period=pending.get("period"),
        platform=legacy_platform, intents=intents,
        goals=list(intents), media_mode=pending.get("media_mode"),
        media_channels=list(pending.get("media_channels") or []),
        time_scope=dict(pending.get("time_scope") or {}),
        comparison_spec=dict(pending.get("comparison_spec") or {}),
        business_spec=dict(pending.get("business_spec") or {}),
        awaiting_slot=awaiting, status="awaiting",
    )


def _semantic_classification(text: str, frame: BusinessTaskContext, explicit: dict[str, Any]) -> dict:
    if not os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("TASK_CONTEXT_LLM_ENABLED", "1") != "1":
        return {}
    prompt = f"""
你是品牌与市场生意分析Bot的Turn Relation分类器。只输出JSON。
当前任务：{json.dumps({
        'brand': frame.brand, 'period': frame.period, 'platform': frame.platform,
        'intents': frame.intents, 'goals': frame.goals, 'awaiting_slot': frame.awaiting_slot,
        'original_question': frame.original_question,
    }, ensure_ascii=False)}
当前句已验证实体：{json.dumps(explicit, ensure_ascii=False, default=str)}
用户当前句：{text}

输出：
{{"relation":"SUPPLY_MISSING_SLOT|MODIFY_SCOPE|ADD_GOAL|FOLLOW_UP_RESULT|NEW_TASK",
  "goals":["EC_BUSINESS|PRODUCT_ANALYSIS|BET|COMPARE_PLATFORMS|DRILL_SELECTED_PLATFORM|MARKET_ANALYSIS"],
  "comparison_metric":"gmv_actual|gmv_growth|evol|growth_contribution|null"}}
判断“补充/修改/追加目标”时，未在当前句明确修改的品牌、时间和平台必须继承。
不得生成实体、SQL、工具名或列表外的值。
"""
    try:
        response = llm_client(max_retries=0).chat.completions.create(
            model=os.environ.get("DASHSCOPE_ROUTER_MODEL", "qwen3.7-plus"),
            messages=[{"role": "user", "content": prompt}], temperature=0,
            max_tokens=220, timeout=float(os.environ.get("TASK_CONTEXT_LLM_TIMEOUT", "6")),
            response_format={"type": "json_object"}, extra_body={"enable_thinking": False},
        )
        return extract_json_object(response.choices[0].message.content or "") or {}
    except Exception as exc:
        log.warning("[task_context] semantic classifier failed; using deterministic merge: %s", exc)
        return {}


def resolve_task_turn(text: str, state: SessionState) -> TaskTurnResolution | None:
    frame = _frame_from_state(state)
    if not frame.awaiting_slot:
        return None
    entity = resolve_entities(text, current_year=date.today().year)
    explicit_brand = entity.brand.surface if entity.brand.status == "resolved" else None
    explicit_period = str(entity.time_scope.focus_period) if entity.time_scope.focus_period else None
    if (
        explicit_period and not state.task_context.awaiting_slot and state.pending_request
        and entity.time_scope.mentions
    ):
        # Preserve the legacy report-facing spelling while the structured time
        # object remains canonical. This compatibility path disappears with
        # legacy pending sessions.
        explicit_period = entity.time_scope.mentions[0].raw_text or explicit_period
    explicit_platform_value = explicit_platform(text)
    supplied = {
        "brand": explicit_brand, "period": explicit_period,
        "platform": explicit_platform_value,
    }
    awaited_value = supplied.get(frame.awaiting_slot)
    if frame.awaiting_slot not in {"brand", "period", "platform"} or not awaited_value:
        return None
    # A conflicting explicit brand or period is a new task; let the normal
    # router build a new frame instead of inheriting stale state.
    if explicit_brand and frame.brand and explicit_brand.casefold() != frame.brand.casefold():
        def normalized_names(values) -> set[str]:
            return {
                "".join(str(value or "").casefold().split())
                for value in values if str(value or "").strip()
            }

        frame_brand = resolve_entities(frame.brand, current_year=date.today().year).brand
        explicit_names = normalized_names([
            explicit_brand, entity.brand.canonical_brand_key, *entity.brand.aliases,
        ])
        frame_names = normalized_names([
            frame.brand, *frame.brand_aliases,
            frame_brand.canonical_brand_key, *frame_brand.aliases,
        ])
        if not explicit_names.intersection(frame_names):
            return None
    if explicit_period and frame.period and explicit_period != frame.period and frame.awaiting_slot != "period":
        return None

    current_decision = build_route_decision(
        text, explicit_period, brand_surface=explicit_brand, time_detected=bool(explicit_period),
    )
    semantic = _semantic_classification(text, frame, supplied)
    relation = str(semantic.get("relation") or "SUPPLY_MISSING_SLOT").upper()
    if relation == "NEW_TASK":
        # The current sentence must go through the normal router without any
        # inherited slots. The application will establish a fresh task frame
        # from that complete route decision.
        return None
    if relation not in RELATIONS:
        relation = "SUPPLY_MISSING_SLOT"
    goals = list(dict.fromkeys([*frame.goals, *frame.intents]))
    semantic_goals = [str(value).upper() for value in semantic.get("goals") or []]
    goals.extend(value for value in semantic_goals if value in GOALS and value not in goals)
    if current_decision:
        goals.extend(value for value in current_decision.intents if value in GOALS and value not in goals)
        if current_decision.question_mode in {"ranking", "comparison"} and explicit_platform_value == "TTL":
            goals.append("COMPARE_PLATFORMS")
    # Deterministic safety fallback understands the analytical operation, not
    # conversational prefixes; entity inheritance never depends on wording.
    lowered = text.casefold()
    if explicit_platform_value == "TTL" and any(token in lowered for token in ("最好", "最大", "最高", "比较", "对比")):
        goals.append("COMPARE_PLATFORMS")
    if any(token in lowered for token in ("下钻", "往下看", "深入分析")):
        goals.append("DRILL_SELECTED_PLATFORM")
    if any(token in lowered for token in ("主推商品", "商品", "sku")):
        goals.append("PRODUCT_ANALYSIS")
    # Explicit cancellation is a slot mutation, not an additional goal. This
    # keeps historical BET/product objectives from leaking into a narrowed turn.
    if any(token in lowered for token in ("不看bet", "不要bet", "不用bet", "去掉bet")):
        goals = [goal for goal in goals if goal != "BET"]
    if any(token in lowered for token in ("不看商品", "不要商品", "不用商品")):
        goals = [goal for goal in goals if goal != "PRODUCT_ANALYSIS"]
    goals = list(dict.fromkeys(goals))
    metric = semantic.get("comparison_metric")
    if metric not in COMPARISON_METRICS:
        metric = None
    intents = list(dict.fromkeys([
        *[value for value in frame.intents if value not in {"BET", "EC_BUSINESS"}],
        *(["EC_BUSINESS"] if "EC_BUSINESS" in goals else []),
        *(["BET"] if "BET" in goals else []),
    ]))
    missing_slots = []
    resolved_brand = explicit_brand or frame.brand
    resolved_period = explicit_period or frame.period
    resolved_platform = explicit_platform_value or frame.platform
    if any(goal in goals for goal in ("EC_BUSINESS", "BET", "PRODUCT_ANALYSIS")) and not resolved_brand:
        missing_slots.append("brand")
    if any(goal in goals for goal in ("EC_BUSINESS", "BET", "PRODUCT_ANALYSIS", "MARKET_ANALYSIS")) and not resolved_period:
        missing_slots.append("period")
    if "EC_BUSINESS" in goals and not resolved_platform:
        missing_slots.append("platform")
    root_question = frame.original_question or text
    combined = root_question if text.strip() == root_question.strip() else f"{root_question}；{text}"
    resolved_time_scope = (
        entity.time_scope.to_dict() if explicit_period else dict(frame.time_scope)
    )
    resolved_comparison_spec = (
        build_comparison_spec(entity.time_scope, text)
        if explicit_period else dict(frame.comparison_spec)
    )
    resolved_business_spec = dict(frame.business_spec)
    if resolved_platform in {"TM", "DY", "JD", "TTL"}:
        subject_scope = (
            "market"
            if "MARKET" in intents or "MARKET_ANALYSIS" in goals
            else "brand"
        )
        resolved_business_spec = build_business_analysis_spec(
            subject_scope=subject_scope,
            brand=resolved_brand,
            platform=resolved_platform,
            segment=frame.segment,
            category=frame.category,
            time_scope=resolved_time_scope,
            comparison_spec=resolved_comparison_spec,
            analysis_mode=(
                current_decision.question_mode if current_decision else "report"
            ),
        ).to_dict()
    return TaskTurnResolution(
        relation=relation,
        brand=resolved_brand,
        brand_aliases=list(frame.brand_aliases),
        period=resolved_period,
        platform=resolved_platform,
        intents=intents, goals=goals,
        comparison_metric=metric or frame.comparison_metric,
        time_scope=resolved_time_scope, comparison_spec=resolved_comparison_spec,
        business_spec=resolved_business_spec,
        media_mode=frame.media_mode, media_channels=list(frame.media_channels),
        original_question=root_question, combined_question=combined,
        missing_slots=missing_slots,
        reason_codes=["ACTIVE_TASK_SLOT_MERGE", f"TURN_RELATION_{relation}"],
    )
