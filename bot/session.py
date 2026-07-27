from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class DrilldownContext:
    brand: str | None = None
    period: str | None = None
    category: str | None = None
    series: str | None = None
    kol_driver: str | None = None
    link_type: str | None = None
    function_tag: str | None = None
    last_analysis_view: str | None = None


@dataclass
class SessionState:
    history: list[dict] = field(default_factory=list)
    drilldown_ctx: DrilldownContext = field(default_factory=DrilldownContext)
    last_result_cache: dict | None = None
    last_updated: datetime = field(default_factory=datetime.utcnow)


_SESSIONS: dict[str, SessionState] = {}


def get_session(open_id: str) -> SessionState:
    if open_id not in _SESSIONS:
        _SESSIONS[open_id] = SessionState()
    return _SESSIONS[open_id]


def add_message(open_id: str, role: str, content: str):
    state = get_session(open_id)
    state.history.append({"role": role, "content": content})
    state.history = state.history[-20:]
    state.last_updated = datetime.utcnow()


def update_context(open_id: str, **kwargs):
    state = get_session(open_id)
    ctx = state.drilldown_ctx
    if "brand" in kwargs and kwargs.get("brand") and kwargs.get("brand") != ctx.brand:
        ctx.category = ctx.series = ctx.kol_driver = ctx.link_type = ctx.function_tag = None
    if "period" in kwargs and kwargs.get("period") and kwargs.get("period") != ctx.period:
        ctx.category = ctx.series = ctx.kol_driver = ctx.link_type = ctx.function_tag = None
    if "category" in kwargs and kwargs.get("category") and kwargs.get("category") != ctx.category:
        ctx.series = ctx.kol_driver = ctx.link_type = ctx.function_tag = None
    for key, value in kwargs.items():
        if hasattr(ctx, key) and value is not None:
            setattr(ctx, key, value)
    state.last_updated = datetime.utcnow()


def set_cache(open_id: str, cache: dict):
    state = get_session(open_id)
    state.last_result_cache = cache
    state.last_updated = datetime.utcnow()
