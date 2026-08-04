from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging
import time

from bot.media_brand import (
    latest_common_month,
    resolve_media_brand,
    resolve_source_brand,
)
from bot.media_formatter import format_media_report
from bot.media_period import parse_media_period, period_from_latest_month
from bot.tools import (
    query_ec_nso,
    query_kol_performance,
    query_media_investment,
    query_social_search,
)

log = logging.getLogger(__name__)


def run_media_chain(
    brand: str,
    period: str | None = None,
    *,
    brand_aliases: list[str] | None = None,
    media_scope: str | None = None,
    on_progress=None,
) -> dict:
    started_at = time.perf_counter()
    if on_progress:
        on_progress("正在检查BET品牌缓存和字典映射…")
    brand_result = resolve_media_brand(brand, brand_aliases=brand_aliases)
    brand_elapsed = time.perf_counter() - started_at
    log.info("[media_chain] brand resolution brand=%s elapsed=%.3fs", brand, brand_elapsed)
    if brand_result.get("error"):
        return {
            "ok": False,
            "markdown": brand_result["message"],
            "meta": {
                "brand": brand,
                "period": period,
                "report_type": "media_analysis",
                "document_ready": False,
            },
        }
    resolved = {
        source: brand_result["resolved"].get(source)
        for source in ("search", "topline", "ksi")
    }
    match_methods = {
        source: (brand_result.get("match_methods") or {}).get(source, "unknown")
        for source in ("search", "topline", "ksi")
    }
    nso_brand_result = resolve_source_brand(
        brand,
        "nso",
        brand_aliases=brand_aliases,
    )
    if nso_brand_result.get("error"):
        resolved["nso"] = None
        match_methods["nso"] = "not_found"
    else:
        resolved["nso"] = nso_brand_result.get("brand")
        match_methods["nso"] = nso_brand_result.get("match_method", "unknown")
    log.info(
        "[media_chain] brand aliases input=%s aliases=%s",
        brand,
        brand_aliases,
    )
    log.info(
        "[media_chain] resolved brands input=%s search=%s(%s) topline=%s(%s) "
        "ksi=%s(%s) nso=%s(%s)",
        brand,
        resolved["search"],
        match_methods.get("search", "unknown"),
        resolved["topline"],
        match_methods.get("topline", "unknown"),
        resolved["ksi"],
        match_methods.get("ksi", "unknown"),
        resolved.get("nso"),
        match_methods.get("nso", "unknown"),
    )

    try:
        if period:
            parsed = parse_media_period(period)
        else:
            latest = latest_common_month(resolved)
            if not latest:
                return {
                    "ok": False,
                    "markdown": (
                        f"无法为品牌“{brand}”确定Search、Topline、KSI和NSO"
                        "均有数据的最新共同月份，请明确指定2026年的分析月份。"
                    ),
                    "meta": {
                        "brand": brand,
                        "period": None,
                        "report_type": "media_analysis",
                        "document_ready": False,
                    },
                }
            parsed = period_from_latest_month(latest)
    except ValueError as exc:
        return {
            "ok": False,
            "markdown": str(exc),
            "meta": {
                "brand": brand,
                "period": period,
                "report_type": "media_analysis",
                "document_ready": False,
            },
        }

    period_data = parsed.to_dict()
    if on_progress:
        on_progress("品牌映射完成，正在并行查询可用的BET数据模块…")
    query_started = time.perf_counter()
    common_period = {
        "focus_start": parsed.focus_start,
        "focus_end": parsed.focus_end,
        "prior_start": parsed.prior_start,
        "prior_end": parsed.prior_end,
    }
    results = {}
    with ThreadPoolExecutor(max_workers=5, thread_name_prefix="bet-query") as executor:
        futures = {}
        if resolved.get("search"):
            futures["search"] = executor.submit(
                query_social_search,
                brand=resolved["search"],
                start_month=parsed.search_start,
                end_month=parsed.search_end,
            )
        else:
            results["search"] = {
                "error": "source_unavailable",
                "message": f"品牌“{brand}”在Social Search数据源没有对应品牌值。",
            }
        if resolved.get("topline"):
            futures["investment"] = executor.submit(
                query_media_investment,
                brand=resolved["topline"],
                **common_period,
            )
        else:
            results["investment"] = {
                "error": "source_unavailable",
                "message": f"品牌“{brand}”在Topline数据源没有对应品牌值。",
            }
        if resolved.get("nso"):
            futures["nso"] = executor.submit(
                query_ec_nso,
                brand=resolved["nso"],
                **common_period,
            )
        else:
            results["nso"] = {
                "error": "source_unavailable",
                "message": f"品牌“{brand}”在EC Consolidation没有对应NSO品牌值。",
            }
        if resolved.get("ksi"):
            futures["red"] = executor.submit(
                query_kol_performance,
                brand=resolved["ksi"],
                platform="red",
                top_n=10,
                **common_period,
            )
            futures["douyin"] = executor.submit(
                query_kol_performance,
                brand=resolved["ksi"],
                platform="douyin",
                top_n=10,
                **common_period,
            )
        else:
            ksi_missing = {
                "error": "source_unavailable",
                "message": f"品牌“{brand}”在KSI数据源没有对应品牌值。",
            }
            results["red"] = dict(ksi_missing)
            results["douyin"] = dict(ksi_missing)
        results.update({name: future.result() for name, future in futures.items()})
    query_elapsed = time.perf_counter() - query_started
    log.info("[media_chain] parallel queries brand=%s elapsed=%.3fs", brand, query_elapsed)
    search_result = results["search"]
    investment_result = results["investment"]
    nso_result = results["nso"]
    red_result = results["red"]
    douyin_result = results["douyin"]

    if on_progress:
        on_progress("正在生成BET媒体投资飞书报告…")
    markdown = format_media_report(
        display_brand=brand,
        period=period_data,
        search_result=search_result,
        investment_result=investment_result,
        nso_result=nso_result,
        red_result=red_result,
        douyin_result=douyin_result,
        resolved_brands=resolved,
        brand_match_methods=match_methods,
    )
    log.info(
        "[media_chain] completed brand=%s total_elapsed=%.3fs",
        brand,
        time.perf_counter() - started_at,
    )
    return {
        "ok": True,
        "markdown": markdown,
        "meta": {
            "brand": brand,
            "period": parsed.canonical,
            "period_display": parsed.display,
            "report_type": "media_analysis",
            "document_ready": True,
            "resolved_brands": resolved,
            "brand_match_methods": match_methods,
            "media_scope": media_scope or "full_bet",
            "last_result_cache": {
                "search_result": search_result,
                "investment_result": investment_result,
                "nso_result": nso_result,
                "red_result": red_result,
                "douyin_result": douyin_result,
            },
        },
    }
