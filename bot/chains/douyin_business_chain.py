from __future__ import annotations

from bot.douyin_business_formatter import format_douyin_business_report
from bot.tools.query_douyin_business import query_douyin_business


def run_douyin_business_chain(
    brand: str,
    period: str,
    brand_aliases: list[str] | tuple[str, ...] | None = None,
    on_progress=None,
) -> dict:
    if not period:
        return {
            "ok": False,
            "markdown": "请明确需要分析的时间段。",
            "meta": {"brand": brand, "period": None, "document_ready": False},
        }
    if on_progress:
        on_progress("正在汇总抖音品牌GMV和四级类目…")
    result = query_douyin_business(
        brand=brand,
        period=period,
        brand_aliases=brand_aliases,
    )
    if result.get("error"):
        return {
            "ok": False,
            "markdown": result.get("message") or "抖音品牌生意分析失败。",
            "meta": {
                "brand": brand,
                "period": period,
                "document_ready": False,
                "domain": "douyin_business",
            },
        }
    if on_progress:
        on_progress("已完成品牌及类目汇总，正在整理品牌级渠道表现…")
    markdown = format_douyin_business_report(result)
    return {
        "ok": True,
        "markdown": markdown,
        "meta": {
            "brand": result.get("brand") or brand,
            "douyin_brand": result.get("source_brand"),
            "brand_aliases": list(brand_aliases or []),
            "period": period,
            "period_meta": result.get("period_meta") or {},
            "document_ready": True,
            "domain": "douyin_business",
            "selected_category": result.get("selected_category"),
            "last_result_cache": {
                "douyin_business_result": result,
            },
        },
    }
