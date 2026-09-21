from __future__ import annotations

"""LLM-first turn understanding.

The model owns natural-language interpretation.  This module only validates
the structured contract, calendar values, brand registry membership and
allow-listed business enums.  It deliberately contains no keyword router.
"""

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo
import json
import logging
import os
import time
from typing import Any

from bot.entity_resolution import BrandResolution, TimeScope, resolve_brand
from bot.routing_contracts import CAPABILITY_REGISTRY, RESPONSE_STRATEGIES
from bot.session import SessionState
from bot.utils import extract_json_object, llm_client, llm_model


log = logging.getLogger(__name__)

RELATIONS = {
    "NEW_TASK", "SUPPLY_MISSING_SLOT", "MODIFY_SCOPE", "ADD_GOAL",
    "FOLLOW_UP_RESULT", "CANCEL",
}
INTENTS = {"EC_BUSINESS", "BET", "MARKET", "FOLLOWUP", "META", "DATA_AVAILABILITY"}
QUESTION_MODES = {"lookup", "report", "ranking", "explanation", "strategy", "comparison"}
PLATFORMS = {"TM", "DY", "JD", "TTL"}
MEDIA_MODES = {"OVERALL_BET", "CHANNEL_ONLY"}
MEDIA_CHANNELS = {"DOUYIN", "RED"}
RANKING_METRICS = {"gmv_actual", "gmv_growth", "evol", "growth_contribution"}
RANKING_METRIC_ALIASES = {
    "gmv": "gmv_actual", "gmv_value": "gmv_actual",
    "sales": "gmv_actual", "sales_value": "gmv_actual", "revenue": "gmv_actual",
    "growth": "gmv_growth", "growth_amount": "gmv_growth",
    "gmv_increment": "gmv_growth", "absolute_growth": "gmv_growth",
    "growth_rate": "evol", "gmv_growth_rate": "evol",
    "yoy": "evol", "yoy_growth": "evol",
    "contribution": "growth_contribution",
    "growth_contribution_rate": "growth_contribution",
}
SLOT_OPS = {"SET", "KEEP", "CLEAR"}
MIN_SLOT_CONFIDENCE = float(os.environ.get("LLM_ROUTER_MIN_SLOT_CONFIDENCE", "0.75"))


class RoutingEnvelopeError(ValueError):
    pass


@dataclass(frozen=True)
class PeriodSlot:
    raw: str
    start_date: str
    end_date: str
    role: str = "FOCUS"
    granularity: str | None = None
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RoutingEnvelope:
    relation: str
    intents: tuple[str, ...]
    goals: tuple[str, ...]
    question_mode: str
    response_strategy: str
    brand_surface: str | None = None
    brand_aliases: tuple[str, ...] = ()
    focus_period: PeriodSlot | None = None
    comparison_periods: tuple[PeriodSlot, ...] = ()
    platforms: tuple[str, ...] = ()
    media_mode: str | None = None
    media_channels: tuple[str, ...] = ()
    segment: str | None = None
    category: str | None = None
    dimensions: tuple[str, ...] = ()
    metrics: tuple[str, ...] = ()
    ranking_metric: str | None = None
    ranking_limit: int | None = None
    slot_ops: dict[str, dict[str, Any]] = field(default_factory=dict)
    missing_slots: tuple[str, ...] = ()
    unsupported_requests: tuple[str, ...] = ()
    clarification_question: str | None = None
    confidence: float = 1.0
    brand_resolution: dict[str, Any] | None = None
    execution_profile: str = "dynamic"
    atomic_request: dict[str, Any] | None = None
    source: str = "llm_router_v1"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["intents"] = list(self.intents)
        value["goals"] = list(self.goals)
        value["comparison_periods"] = [item.to_dict() for item in self.comparison_periods]
        value["platforms"] = list(self.platforms)
        value["media_channels"] = list(self.media_channels)
        value["dimensions"] = list(self.dimensions)
        value["metrics"] = list(self.metrics)
        value["missing_slots"] = list(self.missing_slots)
        value["unsupported_requests"] = list(self.unsupported_requests)
        value["focus_period"] = self.focus_period.to_dict() if self.focus_period else None
        return value


def llm_router_enabled() -> bool:
    return os.environ.get("LEGACY_PIPELINE_EMERGENCY", "0").strip().lower() not in {
        "1", "true", "yes", "on",
    }


def _iso(value: object, name: str) -> str:
    try:
        parsed = date.fromisoformat(str(value or ""))
    except ValueError as exc:
        raise RoutingEnvelopeError(f"{name} must be a valid ISO date") from exc
    return parsed.isoformat()


def _confidence(value: object, *, default: float = 1.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    return min(max(result, 0.0), 1.0)


def _usage(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    if hasattr(usage, "model_dump"):
        return dict(usage.model_dump())
    return {
        key: getattr(usage, key) for key in (
            "prompt_tokens", "completion_tokens", "total_tokens",
        ) if getattr(usage, key, None) is not None
    }


def _period(value: object, role: str) -> PeriodSlot | None:
    if not isinstance(value, dict) or not value:
        return None
    raw = str(value.get("raw") or "").strip()
    start = _iso(value.get("start_date"), f"{role}.start_date")
    end = _iso(value.get("end_date"), f"{role}.end_date")
    if end < start:
        raise RoutingEnvelopeError(f"{role} ends before it starts")
    confidence = _confidence(value.get("confidence"))
    return PeriodSlot(
        raw=raw or f"{start}~{end}", start_date=start, end_date=end,
        role=role, granularity=str(value.get("granularity") or "") or None,
        confidence=confidence,
    )


def _brand_resolution(surface: str | None) -> BrandResolution:
    if not surface:
        return BrandResolution()
    # The resolver sees only the model-extracted surface, never the original
    # natural-language question.  It therefore validates registry membership
    # without becoming a second intent/entity parser.
    return resolve_brand(str(surface).strip(), TimeScope())


def _normalize_slot_ops(value: object) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    allowed_slots = {
        "original_question", "brand", "brand_aliases", "period", "platform",
        "dimensions", "media_mode", "media_channels", "segment", "category",
        "comparison_metric", "awaiting_slot", "status",
    }
    output: dict[str, dict[str, Any]] = {}
    for name, payload in value.items():
        if name not in allowed_slots or not isinstance(payload, dict):
            continue
        op = str(payload.get("op") or "KEEP").upper()
        if op not in SLOT_OPS:
            raise RoutingEnvelopeError(f"invalid slot operation for {name}")
        output[name] = {
            "op": op, "value": payload.get("value"),
            "source": str(payload.get("source") or "llm_router"),
        }
    return output


def _normalize_ranking_metric(
    value: object, *, question_mode: str, intents: tuple[str, ...], metrics: object,
) -> str | None:
    raw = str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")
    if raw in RANKING_METRICS:
        return raw
    if raw in RANKING_METRIC_ALIASES:
        return RANKING_METRIC_ALIASES[raw]
    for candidate in metrics if isinstance(metrics, list) else []:
        normalized = str(candidate or "").strip().casefold().replace("-", "_").replace(" ", "_")
        if normalized in RANKING_METRICS:
            return normalized
        if normalized in RANKING_METRIC_ALIASES:
            return RANKING_METRIC_ALIASES[normalized]
    # ranking_metric is an execution preference, not a required semantic slot.
    # A format variation must never discard an otherwise valid market request.
    if question_mode == "ranking" or "MARKET" in intents:
        if raw:
            log.warning(
                "[llm_router] unknown ranking_metric=%r normalized to gmv_actual", raw,
            )
        return "gmv_actual"
    return None


def validate_routing_payload(payload: dict[str, Any]) -> RoutingEnvelope:
    if not isinstance(payload, dict):
        raise RoutingEnvelopeError("router output is not a JSON object")
    from bot.execution_policy import enabled, validate_atomic
    if enabled() and payload.get("atomic_request") is not None:
        atomic = validate_atomic(payload["atomic_request"])
        return RoutingEnvelope(relation="NEW_TASK", intents=("MARKET",), goals=("MARKET_GMV",),
            question_mode="lookup", response_strategy="TARGETED_ANSWER", atomic_request=atomic)
    relation = str(payload.get("relation") or "NEW_TASK").upper()
    if relation not in RELATIONS:
        raise RoutingEnvelopeError("invalid relation")
    intents = tuple(dict.fromkeys(str(item).upper() for item in payload.get("intents") or []))
    if not intents or any(item not in INTENTS for item in intents):
        raise RoutingEnvelopeError("invalid or empty intents")
    question_mode = str(payload.get("question_mode") or "lookup").lower()
    if question_mode not in QUESTION_MODES:
        raise RoutingEnvelopeError("invalid question_mode")
    strategy = str(payload.get("response_strategy") or "FULL_REPORT").upper()
    if strategy not in RESPONSE_STRATEGIES:
        raise RoutingEnvelopeError("invalid response_strategy")

    slots = payload.get("slots") if isinstance(payload.get("slots"), dict) else {}
    brand = slots.get("brand") if isinstance(slots.get("brand"), dict) else {}
    brand_surface = str(brand.get("surface") or "").strip() or None
    brand_confidence = _confidence(brand.get("confidence"), default=0.0 if brand_surface else 1.0)
    resolution = _brand_resolution(brand_surface)
    focus = _period(slots.get("focus_period"), "FOCUS")
    comparisons = tuple(
        item for item in (
            _period(value, "COMPARISON") for value in (slots.get("comparison_periods") or [])
        ) if item
    )
    if focus and any(item.end_date >= focus.start_date for item in comparisons):
        raise RoutingEnvelopeError("comparison period must precede the focus period")
    platforms = tuple(dict.fromkeys(str(item).upper() for item in slots.get("platforms") or []))
    if any(item not in PLATFORMS for item in platforms):
        raise RoutingEnvelopeError("invalid commerce platform")
    media = slots.get("media_scope") if isinstance(slots.get("media_scope"), dict) else {}
    media_mode = str(media.get("mode") or "").upper() or None
    media_channels = tuple(dict.fromkeys(str(item).upper() for item in media.get("channels") or []))
    if media_mode and media_mode not in MEDIA_MODES:
        raise RoutingEnvelopeError("invalid media mode")
    if any(item not in MEDIA_CHANNELS for item in media_channels):
        raise RoutingEnvelopeError("invalid media channel")
    ranking_metric = _normalize_ranking_metric(
        payload.get("ranking_metric"), question_mode=question_mode,
        intents=intents, metrics=payload.get("metrics"),
    )
    ranking_limit = payload.get("ranking_limit")
    if ranking_limit is not None:
        try:
            ranking_limit = min(max(int(ranking_limit), 1), 20)
        except (TypeError, ValueError) as exc:
            raise RoutingEnvelopeError("invalid ranking limit") from exc

    missing = [str(item) for item in payload.get("missing_slots") or [] if str(item).strip()]
    unsupported = [
        str(item) for item in payload.get("unsupported_requests") or [] if str(item).strip()
    ]
    data_intents = set(intents) & {"EC_BUSINESS", "BET", "MARKET"}
    if data_intents and focus is None:
        missing.append("period")
    # In a MARKET composite plan, brands are produced by the ranking step and
    # become inputs to downstream EC/BET steps. They are not missing user slots.
    if set(intents) & {"EC_BUSINESS", "BET"} and "MARKET" not in intents and not brand_surface:
        missing.append("brand")
    if "EC_BUSINESS" in intents and not platforms:
        missing.append("platform")
    if "BET" in intents and not media_mode:
        missing.append("media_mode")
    if media_mode == "CHANNEL_ONLY" and not media_channels:
        missing.append("media_channels")
    if "MARKET" in intents:
        if not platforms:
            missing.append("platform")
        if not str(slots.get("segment") or "").strip():
            missing.append("segment")
        if not str(slots.get("category") or "").strip():
            missing.append("category")
    if brand_surface and (
        brand_confidence < MIN_SLOT_CONFIDENCE
        or resolution.status in {"ambiguous", "not_found"}
    ):
        missing.append("brand")
    if focus and focus.confidence < MIN_SLOT_CONFIDENCE:
        missing.append("period")
    if any(item.confidence < MIN_SLOT_CONFIDENCE for item in comparisons):
        missing.append("comparison_period")

    # Capability checks validate the model's structured selection. They do
    # not reinterpret the original sentence.
    intent_set = set(intents)
    candidates = [
        spec for spec in CAPABILITY_REGISTRY.values()
        if set(spec.intents) == intent_set and question_mode in spec.question_modes
    ]
    compatible = []
    for spec in candidates:
        if platforms and spec.commerce_platforms and not set(platforms).issubset(spec.commerce_platforms):
            continue
        if media_mode and spec.media_modes and media_mode not in spec.media_modes:
            continue
        if media_channels and spec.media_channels and not set(media_channels).issubset(spec.media_channels):
            continue
        compatible.append(spec)
    if data_intents and not compatible and not missing:
        unsupported.append("requested_scope_has_no_registered_capability")
    for spec in compatible:
        unsupported.extend(
            dimension for dimension in payload.get("dimensions") or []
            if str(dimension) in set(spec.unsupported)
        )
    missing = list(dict.fromkeys(missing))
    unsupported = list(dict.fromkeys(unsupported))

    aliases = list(brand.get("aliases") or [])
    if resolution.status == "resolved":
        brand_surface = resolution.surface or brand_surface
        aliases = list(dict.fromkeys([brand_surface, *resolution.aliases, *aliases]))
    goals = tuple(dict.fromkeys(str(item).upper() for item in payload.get("goals") or intents))
    return RoutingEnvelope(
        relation=relation, intents=intents, goals=goals,
        question_mode=question_mode, response_strategy=strategy,
        brand_surface=brand_surface, brand_aliases=tuple(str(item) for item in aliases if str(item).strip()),
        focus_period=focus, comparison_periods=comparisons, platforms=platforms,
        media_mode=media_mode, media_channels=media_channels,
        segment=str(slots.get("segment") or "").strip() or None,
        category=str(slots.get("category") or "").strip() or None,
        dimensions=tuple(str(item) for item in payload.get("dimensions") or [] if str(item).strip()),
        metrics=tuple(str(item) for item in payload.get("metrics") or [] if str(item).strip()),
        ranking_metric=ranking_metric, ranking_limit=ranking_limit,
        slot_ops=_normalize_slot_ops(payload.get("slot_ops")),
        missing_slots=tuple(missing),
        unsupported_requests=tuple(unsupported),
        clarification_question=str(payload.get("clarification_question") or "").strip() or None,
        confidence=_confidence(payload.get("confidence")),
        brand_resolution=asdict(resolution),
        execution_profile=str(payload.get("execution_profile") or "dynamic"),
    )


def _context(state: SessionState) -> dict[str, Any]:
    task = state.task_context
    return {
        "task": {
            "original_question": task.original_question,
            "brand": task.brand, "period": task.period, "platform": task.platform,
            "intents": list(task.intents), "goals": list(task.goals),
            "dimensions": list(task.dimensions), "media_mode": task.media_mode,
            "media_channels": list(task.media_channels), "segment": task.segment,
            "category": task.category, "comparison_metric": task.comparison_metric,
            "awaiting_slot": task.awaiting_slot, "status": task.status,
        },
        "latest_ec": {
            "brand": state.ec_context.brand, "period": state.ec_context.period,
            "platform": state.ec_context.platform, "filters": state.ec_context.filters,
        },
        "latest_bet": {
            "brand": state.bet_context.brand, "period": state.bet_context.period,
            "platform": state.bet_context.platform, "filters": state.bet_context.filters,
        },
        "latest_market": {
            "period": state.market_context.period, "segment": state.market_context.segment,
            "platform": state.market_context.platform, "category": state.market_context.category,
            "top_brands": list(state.market_context.top_brands[:10]),
        },
        "active_category_query": state.category_market_lookup_context,
        "active_market_query": state.market_lookup_context,
        "recent_turns": list(task.recent_turns[-6:]),
    }


def _capabilities() -> list[dict[str, Any]]:
    return [
        {
            "id": name, "intents": list(spec.intents),
            "required_slots": list(spec.required_slots),
            "platforms": list(spec.commerce_platforms),
            "media_modes": list(spec.media_modes), "dimensions": list(spec.dimensions),
            "limitations": list(spec.limitations),
        }
        for name, spec in CAPABILITY_REGISTRY.items()
    ]


def _message_day(received_at=None) -> str:
    zone = ZoneInfo("Asia/Shanghai")
    if isinstance(received_at, datetime):
        value = received_at.replace(tzinfo=zone) if received_at.tzinfo is None else received_at.astimezone(zone)
        return value.date().isoformat()
    if received_at:
        try:
            stamp = float(received_at)
            if stamp > 1e11:
                stamp /= 1000
            if stamp > 0:
                return datetime.fromtimestamp(stamp, zone).date().isoformat()
        except (ValueError, TypeError, OverflowError, OSError):
            pass
    return datetime.now(zone).date().isoformat()


def _prompt(user_text: str, state: SessionState, error: str | None, *, received_at=None) -> str:
    from bot.execution_policy import routing_instructions
    repair = routing_instructions(state) + (f"\n上次JSON未通过校验：{error}。请完整修正。" if error else "")
    return f"""你是品牌、电商、市场和媒体投资分析Bot的唯一自然语言Router。
消息接收日期是{_message_day(received_at)}（Asia/Shanghai）；所有相对日期以此日期为准。你必须理解完整语义和会话关系，不能依赖固定句式。

品类规则：明确的“全部生意/全部品类/全品类”表示category=TTL，不是缺失值。
同一品牌同一平台只修改日期（包括缺数后补日期）时保留已经确认的category；不得根据常识猜品类。
换品牌未明确指定品类时清除category，用户明确说“也看洗护”等才可指定。BET仍为整品牌，不为BET追问品类。

当前会话：
{json.dumps(_context(state), ensure_ascii=False, default=str)}

可执行能力：
{json.dumps(_capabilities(), ensure_ascii=False)}

当前用户原文：{user_text}

请返回一个JSON对象，结构如下：
{{
  "relation":"NEW_TASK|SUPPLY_MISSING_SLOT|MODIFY_SCOPE|ADD_GOAL|FOLLOW_UP_RESULT|CANCEL",
  "intents":["EC_BUSINESS|BET|MARKET|FOLLOWUP|META|DATA_AVAILABILITY"],
  "goals":["用户实际目标"],
  "question_mode":"lookup|report|ranking|explanation|strategy|comparison",
  "response_strategy":"FULL_REPORT|TARGETED_ANSWER|MULTI_STEP_ANALYSIS|META_ANSWER|CLARIFY",
  "slots":{{
    "brand":{{"surface":null,"aliases":[],"confidence":0.0}},
    "focus_period":{{"raw":"","start_date":"YYYY-MM-DD","end_date":"YYYY-MM-DD","granularity":"MONTH|DATE_RANGE|QUARTER|YTD|MTD","confidence":1.0}},
    "comparison_periods":[{{"raw":"","start_date":"YYYY-MM-DD","end_date":"YYYY-MM-DD","confidence":1.0}}],
    "platforms":["TM|DY|JD|TTL"],
    "media_scope":{{"mode":"OVERALL_BET|CHANNEL_ONLY|null","channels":["DOUYIN|RED"]}},
    "segment":null,"category":null
  }},
  "dimensions":[],"metrics":[],"ranking_metric":null,"ranking_limit":null,
  "slot_ops":{{"brand":{{"op":"SET|KEEP|CLEAR","value":null,"source":"current_explicit|task_context"}}}},
  "missing_slots":[],"clarification_question":null,"confidence":1.0
}}

规则：
- 2026年7月生意 vs 2025年7月生意表示FOCUS=2026-07-01..07-31，COMPARISON=2025-07-01..07-31，不得追问角色。
- 槽位未在当前句修改时按relation继承上下文并输出KEEP；禁止编造品牌、时间或平台。
- MARKET不要求品牌；META和DATA_AVAILABILITY不要求业务槽位。
- ranking_metric只能是gmv_actual、gmv_growth、evol或growth_contribution。用户未明确排序口径的Top榜默认gmv_actual。
- 用户表达多目标时必须全部保留；只有必需槽位确实缺失或有真实歧义才CLARIFY。
- 输出必须是合法JSON，不要解释。{repair}"""


def route_with_llm(user_text: str, state: SessionState, *, received_at=None) -> RoutingEnvelope:
    if not os.environ.get("DASHSCOPE_API_KEY"):
        raise RoutingEnvelopeError("DASHSCOPE_API_KEY is not configured")
    client = llm_client(max_retries=0)
    validation_error = None
    started = time.monotonic()
    for attempt in range(2):
        try:
            response = client.chat.completions.create(
                model=llm_model("router"),
                messages=[
                    {"role": "system", "content": "你是高精度语义Router，只输出JSON对象。"},
                    {"role": "user", "content": _prompt(user_text, state, validation_error, received_at=received_at)},
                ],
                max_tokens=int(os.environ.get("ROUTER_LLM_MAX_TOKENS", "1200")),
                timeout=float(os.environ.get("ROUTER_LLM_TIMEOUT", "15")),
                response_format={"type": "json_object"},
                extra_body={"enable_thinking": False},
            )
            payload = extract_json_object(response.choices[0].message.content or "")
            envelope = validate_routing_payload(payload)
            log.info(
                "[llm_router] model=%s attempt=%s elapsed_ms=%s intents=%s missing=%s "
                "unsupported=%s usage=%s",
                llm_model("router"), attempt + 1, int((time.monotonic() - started) * 1000),
                list(envelope.intents), list(envelope.missing_slots),
                list(envelope.unsupported_requests), _usage(response),
            )
            return envelope
        except Exception as exc:
            validation_error = str(exc)
            log.warning("[llm_router] attempt=%s rejected: %s", attempt + 1, validation_error)
    raise RoutingEnvelopeError(validation_error or "router failed")
