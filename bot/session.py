from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
import math
import os
import logging
from threading import RLock
from typing import Any
from uuid import uuid4

from bot.runtime_config import queue_enabled


log = logging.getLogger(__name__)


@dataclass
class DrilldownContext:
    brand: str | None = None
    tmall_brand: str | None = None
    brand_aliases: list[str] = field(default_factory=list)
    period: str | None = None
    category: str | None = None
    series: str | None = None
    key_driver: str | None = None
    function_tag: str | None = None
    last_analysis_view: str | None = None


@dataclass
class DomainContext:
    brand: str | None = None
    period: str | None = None
    platform: str | None = None
    brand_aliases: list[str] = field(default_factory=list)
    source_brands: dict[str, str | None] = field(default_factory=dict)
    filters: dict[str, str | None] = field(default_factory=dict)
    recent_evidence: list[dict] = field(default_factory=list)
    report_cache: dict | None = None


@dataclass
class MarketContext:
    period: str | None = None
    segment: str = "PURE MASS"
    platform: str = "TTL"
    category: str = "TOTAL BEAUTY"
    last_view: str | None = None
    recent_result: dict | None = None
    top_brands: list[str] = field(default_factory=list)


@dataclass
class ActivePlanState:
    plan_id: str | None = None
    original_question: str | None = None
    goal: str | None = None
    entities: dict = field(default_factory=dict)
    status: str | None = None
    completed_steps: list[str] = field(default_factory=list)
    failed_steps: list[str] = field(default_factory=list)
    top_brands: list[str] = field(default_factory=list)
    platform_matrix: list[dict] = field(default_factory=list)
    selected_platforms: list[dict] = field(default_factory=list)
    pending_slots: list[str] = field(default_factory=list)


@dataclass
class BusinessTaskContext:
    """Persistent business task frame; clarification state never owns the task."""
    task_id: str = field(default_factory=lambda: uuid4().hex)
    state_version: int = 0
    original_question: str | None = None
    brand: str | None = None
    brand_aliases: list[str] = field(default_factory=list)
    period: str | None = None
    platform: str | None = None
    intents: list[str] = field(default_factory=list)
    goals: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    media_mode: str | None = None
    media_channels: list[str] = field(default_factory=list)
    segment: str | None = None
    category: str | None = None
    comparison_metric: str | None = None
    time_scope: dict = field(default_factory=dict)
    comparison_spec: dict = field(default_factory=dict)
    business_spec: dict = field(default_factory=dict)
    awaiting_slot: str | None = None
    status: str = "active"
    last_relation: str | None = None
    turn_count: int = 0
    recent_turns: list[str] = field(default_factory=list)


@dataclass
class SessionState:
    history: list[dict] = field(default_factory=list)
    drilldown_ctx: DrilldownContext = field(default_factory=DrilldownContext)
    last_result_cache: dict | None = None
    pending_request: dict | None = None
    ec_context: DomainContext = field(default_factory=DomainContext)
    bet_context: DomainContext = field(default_factory=DomainContext)
    market_context: MarketContext = field(default_factory=MarketContext)
    active_plan: ActivePlanState = field(default_factory=ActivePlanState)
    task_context: BusinessTaskContext = field(default_factory=BusinessTaskContext)
    last_updated: datetime = field(default_factory=datetime.utcnow)


_SESSIONS: dict[str, SessionState] = {}
_SESSION_LOCK = RLock()

KEEP = "KEEP"
SET = "SET"
CLEAR = "CLEAR"


@dataclass(frozen=True)
class SlotUpdate:
    """Explicit slot mutation; None is never overloaded to mean two things."""
    op: str = KEEP
    value: Any = None


@dataclass
class TaskContextPatch:
    relation: str = "CONTINUE_TASK"
    slots: dict[str, SlotUpdate] = field(default_factory=dict)
    add_intents: list[str] = field(default_factory=list)
    remove_intents: list[str] = field(default_factory=list)
    set_intents: list[str] | None = None
    add_goals: list[str] = field(default_factory=list)
    remove_goals: list[str] = field(default_factory=list)
    set_goals: list[str] | None = None
    current_turn: str | None = None
    reason_codes: list[str] = field(default_factory=list)


_TASK_SLOT_NAMES = {
    "original_question", "brand", "brand_aliases", "period", "platform",
    "dimensions", "media_mode", "media_channels", "segment", "category",
    "comparison_metric", "time_scope", "comparison_spec", "business_spec",
    "awaiting_slot", "status", "last_relation",
}


def reduce_task_context(current: BusinessTaskContext, patch: TaskContextPatch) -> BusinessTaskContext:
    """Apply one turn as an explicit, deterministic state transition."""
    relation = str(patch.relation or "CONTINUE_TASK").upper()
    task = BusinessTaskContext() if relation == "NEW_TASK" else BusinessTaskContext(**asdict(current))
    defaults = BusinessTaskContext()
    for name, update in patch.slots.items():
        if name not in _TASK_SLOT_NAMES or not isinstance(update, SlotUpdate):
            continue
        op = str(update.op or KEEP).upper()
        if op == KEEP:
            continue
        if op == CLEAR:
            setattr(task, name, getattr(defaults, name))
        elif op == SET:
            value = update.value
            if name in {"brand_aliases", "dimensions", "media_channels"}:
                value = list(value or [])
            elif name in {"time_scope", "comparison_spec", "business_spec"}:
                value = dict(value or {})
            setattr(task, name, value)
    intents = list(patch.set_intents) if patch.set_intents is not None else task.intents
    goals = list(patch.set_goals) if patch.set_goals is not None else task.goals
    intents = [item for item in intents if item not in set(patch.remove_intents)]
    goals = [item for item in goals if item not in set(patch.remove_goals)]
    task.intents = list(dict.fromkeys([*intents, *patch.add_intents]))
    task.goals = list(dict.fromkeys([*goals, *patch.add_goals]))
    if patch.current_turn:
        task.recent_turns = [*task.recent_turns, str(patch.current_turn)][-6:]
    task.last_relation = relation
    task.turn_count += 1
    task.state_version += 1
    return task


def _redis_session_enabled() -> bool:
    return queue_enabled() or os.environ.get("BOT_SESSION_BACKEND", "").strip().lower() == "redis"


def _session_key(open_id: str) -> str:
    digest = hashlib.sha256(open_id.encode("utf-8")).hexdigest()
    return f"ai-bot:session:{digest}"


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    item_method = getattr(value, "item", None)
    if callable(item_method):
        try:
            return _json_safe(item_method())
        except Exception:
            pass
    return str(value)


def _state_payload(state: SessionState) -> dict:
    return _json_safe(asdict(state))


def _state_from_payload(payload: dict) -> SessionState:
    last_updated = payload.get("last_updated")
    try:
        parsed_updated = datetime.fromisoformat(last_updated) if last_updated else datetime.utcnow()
    except (TypeError, ValueError):
        parsed_updated = datetime.utcnow()
    return SessionState(
        history=list(payload.get("history") or []),
        drilldown_ctx=DrilldownContext(**(payload.get("drilldown_ctx") or {})),
        last_result_cache=payload.get("last_result_cache"),
        pending_request=payload.get("pending_request"),
        ec_context=DomainContext(**(payload.get("ec_context") or {})),
        bet_context=DomainContext(**(payload.get("bet_context") or {})),
        market_context=MarketContext(**(payload.get("market_context") or {})),
        active_plan=ActivePlanState(**(payload.get("active_plan") or {})),
        task_context=BusinessTaskContext(**(payload.get("task_context") or {})),
        last_updated=parsed_updated,
    )


def _encode_session(state: SessionState) -> bytes:
    payload = _state_payload(state)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    max_bytes = int(os.environ.get("BOT_MAX_SESSION_BYTES", str(2 * 1024 * 1024)))
    if len(encoded) <= max_bytes:
        return encoded

    payload["last_result_cache"] = None
    for domain in ("ec_context", "bet_context"):
        payload[domain]["report_cache"] = None
        payload[domain]["recent_evidence"] = list(payload[domain].get("recent_evidence") or [])[-50:]
    payload["market_context"]["recent_result"] = None
    if payload.get("active_plan"):
        payload["active_plan"]["platform_matrix"] = list(
            payload["active_plan"].get("platform_matrix") or []
        )[:15]
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > max_bytes:
        payload["history"] = list(payload.get("history") or [])[-10:]
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > max_bytes:
        raise ValueError(f"session exceeds BOT_MAX_SESSION_BYTES={max_bytes} after cache trimming")
    return encoded


def _store_session(open_id: str, state: SessionState) -> None:
    if not _redis_session_enabled():
        _SESSIONS[open_id] = state
        return
    from bot.redis_client import get_redis

    ttl = int(os.environ.get("BOT_SESSION_TTL_SECONDS", "1209600"))
    get_redis().set(_session_key(open_id), _encode_session(state), ex=ttl)


def _mutate_session(open_id: str, mutator) -> SessionState:
    """Atomically apply a session mutation.

    Redis sessions use optimistic locking so two nearby user turns cannot
    silently overwrite each other's task slots. In-memory tests/processes use
    one re-entrant lock around the same read-modify-write contract.
    """
    if not _redis_session_enabled():
        with _SESSION_LOCK:
            state = _SESSIONS.setdefault(open_id, SessionState())
            mutator(state)
            state.last_updated = datetime.utcnow()
            _SESSIONS[open_id] = state
            return state

    from bot.redis_client import get_redis

    client = get_redis()
    key = _session_key(open_id)
    ttl = int(os.environ.get("BOT_SESSION_TTL_SECONDS", "1209600"))
    if not hasattr(client, "pipeline"):
        # Lightweight test doubles and minimal Redis adapters may not expose
        # transactions. Keep the same mutation contract under the process lock.
        with _SESSION_LOCK:
            raw = client.get(key)
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            try:
                state = _state_from_payload(json.loads(raw)) if raw else SessionState()
            except (TypeError, ValueError, json.JSONDecodeError):
                state = SessionState()
            mutator(state)
            state.last_updated = datetime.utcnow()
            client.set(key, _encode_session(state), ex=ttl)
            return state
    for _attempt in range(5):
        pipe = client.pipeline()
        try:
            pipe.watch(key)
            raw = pipe.get(key)
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            try:
                state = _state_from_payload(json.loads(raw)) if raw else SessionState()
            except (TypeError, ValueError, json.JSONDecodeError):
                state = SessionState()
            mutator(state)
            state.last_updated = datetime.utcnow()
            pipe.multi()
            pipe.set(key, _encode_session(state), ex=ttl)
            pipe.execute()
            return state
        except Exception as exc:
            if exc.__class__.__name__ != "WatchError":
                raise
        finally:
            pipe.reset()
    raise RuntimeError("session update conflicted repeatedly; please retry the request")


def get_session(open_id: str) -> SessionState:
    if _redis_session_enabled():
        from bot.redis_client import get_redis

        raw = get_redis().get(_session_key(open_id))
        if not raw:
            return SessionState()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            return _state_from_payload(json.loads(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            return SessionState()
    if open_id not in _SESSIONS:
        _SESSIONS[open_id] = SessionState()
    return _SESSIONS[open_id]


def add_message(open_id: str, role: str, content: str):
    def mutate(state: SessionState):
        state.history.append({"role": role, "content": content})
        state.history = state.history[-20:]
    _mutate_session(open_id, mutate)


def update_context(open_id: str, **kwargs):
    def mutate(state: SessionState):
        ctx = state.drilldown_ctx
        if "brand" in kwargs and kwargs.get("brand") and kwargs.get("brand") != ctx.brand:
            ctx.category = ctx.series = ctx.key_driver = ctx.function_tag = None
            ctx.tmall_brand = None
            ctx.brand_aliases = []
        if "period" in kwargs and kwargs.get("period") and kwargs.get("period") != ctx.period:
            ctx.category = ctx.series = ctx.key_driver = ctx.function_tag = None
        if "category" in kwargs and kwargs.get("category") and kwargs.get("category") != ctx.category:
            ctx.series = ctx.key_driver = ctx.function_tag = None
        for key, value in kwargs.items():
            if hasattr(ctx, key) and value is not None:
                setattr(ctx, key, value)
    _mutate_session(open_id, mutate)


def set_cache(open_id: str, cache: dict):
    _mutate_session(open_id, lambda state: setattr(state, "last_result_cache", cache))


def set_pending_request(open_id: str, request: dict | None):
    def mutate(state: SessionState):
        state.pending_request = dict(request) if request else None
        before = state.task_context
        if request:
            request_brand = str(request.get("brand") or "").strip()
            normalize_brand = lambda value: "".join(str(value or "").casefold().split())
            request_brand_names = {
                normalize_brand(value)
                for value in [request_brand, *(request.get("brand_aliases") or [])]
                if str(value or "").strip()
            }
            active_brand_names = {
                normalize_brand(value)
                for value in [before.brand, *before.brand_aliases]
                if str(value or "").strip()
            }
            brand_conflict = bool(
                request_brand and before.brand
                and not request_brand_names.intersection(active_brand_names)
            )
            relation = (
                "NEW_TASK"
                if brand_conflict
                else "SUPPLY_MISSING_SLOT"
            )
            awaiting = {
                "business_platform_selection": "platform",
                "brand_business_investment_analysis": "platform",
                "v2_brand": "brand", "three_platform_competitor_analysis": (
                    "brand" if not request.get("brand") else "period"
                ),
                "v2_period": "period", "default_analysis": "period",
                "douyin_business_analysis": "period", "jd_business_analysis": "period",
                "v2_time_roles": "time_roles",
                "v2_media_scope": "media_scope", "v2_market_scope": "market_scope",
                "analysis_preflight": "confirmation",
            }.get(str(request.get("intent") or ""))
            slots: dict[str, SlotUpdate] = {
                "awaiting_slot": SlotUpdate(SET, awaiting) if awaiting else SlotUpdate(CLEAR),
                "status": SlotUpdate(SET, "awaiting" if awaiting else "active"),
            }
            for key in (
                "original_question", "brand", "brand_aliases", "period", "platform",
                "media_mode", "media_channels", "segment", "category", "dimensions",
                "comparison_metric", "time_scope", "comparison_spec", "business_spec",
            ):
                source_key = "original_text" if key == "original_question" else key
                if source_key in request and request.get(source_key) is not None:
                    slots[key] = SlotUpdate(SET, request.get(source_key))
            request_intents = list(request.get("intents") or [])
            if not request_intents and request.get("intent") == "brand_business_investment_analysis":
                request_intents = ["EC_BUSINESS", "BET"]
            elif not request_intents and request.get("intent") in {
                "business_platform_selection", "default_analysis", "douyin_business_analysis",
                "jd_business_analysis", "three_platform_competitor_analysis",
            }:
                request_intents = ["EC_BUSINESS"]
            if request.get("include_bet") and "BET" not in request_intents:
                request_intents.append("BET")
            state.task_context = reduce_task_context(before, TaskContextPatch(
                relation=relation, slots=slots, add_intents=request_intents,
                add_goals=request_intents, current_turn=request.get("original_text"),
                reason_codes=["CLARIFICATION_OPENED"],
            ))
        else:
            state.task_context = reduce_task_context(before, TaskContextPatch(
                slots={
                    "awaiting_slot": SlotUpdate(CLEAR),
                    "status": SlotUpdate(SET, "active"),
                },
                reason_codes=["CLARIFICATION_CLOSED"],
            ))
        _log_task_transition(before, state.task_context, "set_pending_request")
    _mutate_session(open_id, mutate)


def _log_task_transition(before: BusinessTaskContext, after: BusinessTaskContext, source: str) -> None:
    if os.environ.get("TASK_CONTEXT_TRACE_ENABLED", "1") != "1":
        return
    fields = (
        "brand", "period", "platform", "intents", "goals", "media_mode",
        "media_channels", "segment", "category", "comparison_metric",
        "time_scope", "comparison_spec", "business_spec",
        "awaiting_slot", "status",
    )
    log.info("[task_context_trace] %s", json.dumps({
        "source": source, "task_id": after.task_id,
        "before_version": before.state_version, "after_version": after.state_version,
        "relation": after.last_relation,
        "before": {name: getattr(before, name) for name in fields},
        "after": {name: getattr(after, name) for name in fields},
    }, ensure_ascii=False, default=str))


def apply_task_context_patch(open_id: str, patch: TaskContextPatch) -> BusinessTaskContext:
    result: BusinessTaskContext | None = None
    def mutate(state: SessionState):
        nonlocal result
        before = state.task_context
        result = reduce_task_context(before, patch)
        state.task_context = result
        _log_task_transition(before, result, "task_patch")
    _mutate_session(open_id, mutate)
    return result or BusinessTaskContext()


def update_task_context(open_id: str, **kwargs):
    relation = str(kwargs.pop("relation", kwargs.get("last_relation") or "CONTINUE_TASK"))
    current_turn = kwargs.pop("current_turn", None)
    explicit_intents = kwargs.pop("intents", None)
    explicit_goals = kwargs.pop("goals", None)
    kwargs.pop("turn_count", None)
    slots = {
        key: SlotUpdate(SET, value)
        for key, value in kwargs.items()
        if key in _TASK_SLOT_NAMES and value is not None
    }
    return apply_task_context_patch(open_id, TaskContextPatch(
        relation=relation, slots=slots,
        set_intents=list(explicit_intents) if explicit_intents is not None else None,
        set_goals=list(explicit_goals) if explicit_goals is not None else None,
        current_turn=current_turn,
    ))


def update_domain_context(open_id: str, domain: str, **kwargs):
    def mutate(state: SessionState):
        ctx = state.ec_context if domain == "ec" else state.bet_context
        identity_changed = (
            (kwargs.get("brand") and kwargs["brand"] != ctx.brand)
            or (kwargs.get("period") and kwargs["period"] != ctx.period)
            or (kwargs.get("platform") and kwargs["platform"] != ctx.platform)
        )
        if identity_changed:
            ctx.filters = {}
            ctx.recent_evidence = []
            ctx.report_cache = None
        for key, value in kwargs.items():
            if hasattr(ctx, key) and value is not None:
                setattr(ctx, key, value)
    _mutate_session(open_id, mutate)


def update_market_context(open_id: str, **kwargs):
    def mutate(state: SessionState):
        for key, value in kwargs.items():
            if hasattr(state.market_context, key) and value is not None:
                setattr(state.market_context, key, value)
    _mutate_session(open_id, mutate)


def update_active_plan(open_id: str, **kwargs):
    def mutate(state: SessionState):
        for key, value in kwargs.items():
            if hasattr(state.active_plan, key) and value is not None:
                setattr(state.active_plan, key, value)
    _mutate_session(open_id, mutate)
