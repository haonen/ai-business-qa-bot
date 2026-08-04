from __future__ import annotations

import logging
import time

from bot.market_formatter import format_market_result
from bot.market_plan import MarketPlan
from bot.skills.loader import load_skill
from bot.tools.query_market_top_brands import query_market_top_brands
from bot.tools.query_market_trend import query_market_trend


log = logging.getLogger(__name__)


def run_market_chain(plan: MarketPlan) -> dict:
    """Execute a validated market plan without model-generated SQL or tool calls."""
    started = time.monotonic()
    load_skill("market_analysis")  # Keep the executable chain tied to its business contract.
    if not plan.period:
        return {"ok": False, "markdown": "你想看哪个时间段的大盘？", "meta": {"document_ready": False, "awaiting": "period"}}
    if plan.intent == "market_brand_ranking":
        raw = query_market_top_brands(plan.period, plan.segment, plan.platform, plan.ranking_metric, 5)
    else:
        raw = query_market_trend(plan.period, plan.segment, plan.platform, plan.view)
    coverage_sources = sorted({
        (str(row.get("period_key")), str(row.get("month")), str(row.get("source")))
        for row in (raw.get("coverage") or [])
    })
    log.info("[market_chain] plan=%s elapsed=%.3fs error=%s coverage=%s",
             plan.to_dict(), time.monotonic() - started, raw.get("error"), coverage_sources)
    return format_market_result(raw)
