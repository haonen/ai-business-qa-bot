from __future__ import annotations

import logging
import os
import re
import time
from datetime import date

from bot.market_formatter import format_market_result
from bot.market_plan import MarketPlan
from bot.skills.loader import load_skill
from bot.tools.query_market_top_brands import query_market_top_brands
from bot.tools.query_market_brand_deep_dive import query_market_brand_deep_dive
from bot.tools.query_market_trend import query_market_trend
from bot.chains.media_chain import run_media_chain
from bot.chains.default_chain import run_default_chain
from bot.chains.douyin_business_chain import run_douyin_business_chain
from bot.chains.jd_business_chain import run_jd_business_chain


log = logging.getLogger(__name__)


PLATFORM_LABELS = {"TM": "天猫", "DY": "抖音", "JD": "京东"}


def _platform_fanout_enabled() -> bool:
    return os.environ.get("AGENT_PLAN_MARKET_PLATFORM_FANOUT_ENABLED", "1").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _select_growth_platform(brand: dict) -> dict | None:
    """Choose the supported platform with the largest absolute GMV change."""
    candidates = [
        row for row in (brand.get("platforms") or [])
        if str(row.get("platform") or "").upper() in PLATFORM_LABELS
        and (float(row.get("gmv_actual") or 0) != 0 or float(row.get("gmv_prior") or 0) != 0)
    ]
    if not candidates:
        return None
    selected = max(candidates, key=lambda row: float(row.get("gmv_growth") or 0))
    return {**selected, "platform": str(selected.get("platform") or "").upper()}


def _run_platform_template(brand: str, period: str, platform: str) -> dict:
    runners = {
        "TM": run_default_chain,
        "DY": run_douyin_business_chain,
        "JD": run_jd_business_chain,
    }
    runner = runners[platform]
    return runner(brand, period, brand_aliases=[brand])


def _append_platform_template_fanout(
    formatted: dict, raw: dict, plan: MarketPlan, on_progress=None,
) -> None:
    if not _platform_fanout_enabled():
        formatted.setdefault("meta", {})["platform_template_fanout"] = "disabled"
        return
    brands = list(raw.get("brands") or [])[: min(plan.ranking_limit, 3)]
    selections = []
    sections = []
    reports = {}
    for index, brand_row in enumerate(brands, 1):
        brand = str(brand_row.get("brand") or "").strip()
        selected = _select_growth_platform(brand_row)
        if not brand or not selected:
            continue
        platform = selected["platform"]
        label = PLATFORM_LABELS[platform]
        growth = float(selected.get("gmv_growth") or 0)
        selections.append({
            "brand": brand, "platform": platform,
            "gmv_actual": selected.get("gmv_actual"),
            "gmv_prior": selected.get("gmv_prior"),
            "gmv_growth": growth, "evol": selected.get("evol"),
            "selection_rule": "max_gmv_growth",
        })
        if on_progress:
            on_progress(
                f"已完成品牌×平台矩阵，正在下钻第{index}/{len(brands)}个品牌"
                f"【{brand}】的{label}生意…"
            )
        try:
            result = _run_platform_template(brand, str(plan.period or ""), platform)
        except Exception as exc:
            log.exception("market platform template failed brand=%s platform=%s", brand, platform)
            result = {
                "ok": False,
                "markdown": f"{label}生意模板执行失败：{exc}",
                "meta": {"error": "platform_template_execution_error"},
            }
        reports[brand] = {
            "platform": platform, "ok": bool(result.get("ok")),
            "meta": result.get("meta") or {},
        }
        direction = "GMV增长额最大" if growth >= 0 else "三平台均下降时GMV降幅最小"
        sections.extend([
            f"## {brand}｜选择{label}继续下钻",
            f"选择规则：{label}是该品牌{direction}的平台，GMV变化额为{growth / 1_000_000:.1f}M。",
            result.get("markdown") or f"{label}模板未返回可用结果。",
        ])
    if sections:
        formatted["markdown"] = "\n\n".join([
            formatted.get("markdown") or "",
            "# 按品牌最大增长平台继续下钻",
            *sections,
        ])
    formatted.setdefault("meta", {}).update({
        "selected_platform_by_brand": selections,
        "platform_template_by_brand": reports,
        "platform_template_fanout": "completed",
    })


def run_market_chain(plan: MarketPlan, on_progress=None) -> dict:
    """Execute a validated market plan without model-generated SQL or tool calls."""
    started = time.monotonic()
    load_skill("market_analysis")  # Keep the executable chain tied to its business contract.
    if not plan.period:
        return {"ok": False, "markdown": "你想看哪个时间段的大盘？", "meta": {"document_ready": False, "awaiting": "period"}}
    if plan.intent == "market_brand_deep_dive":
        if on_progress:
            on_progress("正在确定Top品牌，并拆解三平台结构、月度节奏和商品证据…")
        raw = query_market_brand_deep_dive(
            plan.period, plan.segment, min(plan.ranking_limit, 3),
            plan.platform, plan.ranking_metric, plan.category,
        )
    elif plan.intent == "market_brand_ranking":
        raw = query_market_top_brands(
            plan.period, plan.segment, plan.platform,
            plan.ranking_metric, plan.ranking_limit, plan.category,
        )
    else:
        raw = query_market_trend(
            plan.period, plan.segment, plan.platform, plan.view, plan.category,
        )
    coverage_sources = sorted({
        (str(row.get("period_key")), str(row.get("month")), str(row.get("source")))
        for row in (raw.get("coverage") or [])
    })
    log.info("[market_chain] plan=%s elapsed=%.3fs error=%s coverage=%s",
             plan.to_dict(), time.monotonic() - started, raw.get("error"), coverage_sources)
    formatted = format_market_result(raw)
    if not formatted.get("ok") or plan.intent != "market_brand_deep_dive":
        return formatted

    _append_platform_template_fanout(formatted, raw, plan, on_progress=on_progress)

    if not plan.include_bet:
        return formatted

    top_brands = list((formatted.get("meta") or {}).get("top_brands") or [])[:3]
    if not top_brands:
        return formatted
    year_match = re.search(r"20\d{2}", str(plan.period or ""))
    year = int(year_match.group(0)) if year_match else date.today().year
    end_month = date.today().month if plan.bet_latest_ytd and year == date.today().year else 12
    bet_period = f"{year}年1-{end_month}月"
    bet_sections = []
    bet_meta = {}
    for index, brand in enumerate(top_brands, 1):
        if on_progress:
            on_progress(
                f"已完成Top品牌生意拆解，正在查询第{index}/{len(top_brands)}个品牌"
                f"【{brand}】今年截至最新可用月的BET…"
            )
        result = run_media_chain(
            brand, bet_period, brand_aliases=[brand],
            media_scope="full_bet", media_mode="OVERALL_BET",
        )
        bet_sections.extend([
            f"## {index}. {brand}｜今年截至最新可用月BET",
            result.get("markdown") or "BET未返回可用结果。",
        ])
        bet_meta[brand] = result.get("meta") or {}

    formatted["markdown"] = "\n\n".join([
        formatted.get("markdown") or "",
        "# Top品牌BET补充分析",
        *bet_sections,
    ])
    formatted.setdefault("meta", {}).update({
        "include_bet": True,
        "bet_requested_period": bet_period,
        "bet_by_brand": bet_meta,
        "document_ready": True,
        "document_title": "Pure Mass Top品牌三平台生意与BET复合分析",
    })
    return formatted
