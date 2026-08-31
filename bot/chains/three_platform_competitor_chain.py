from __future__ import annotations

from bot.skills.loader import load_skill
from bot.three_platform_competitor_formatter import format_three_platform_competitor_report
from bot.tools.query_three_platform_competitor import query_three_platform_competitor


def run_three_platform_competitor_chain(
    brand: str, period: str, on_progress=None,
) -> dict:
    load_skill("three_platform_competitor")
    if on_progress:
        on_progress("正在汇总三平台品牌GMV、大盘份额和Top 5类目…")
    result = query_three_platform_competitor(brand, period)
    if result.get("error"):
        return {
            "ok": False,
            "markdown": result.get("message") or "三平台生意分析失败。",
            "meta": {
                "brand": brand,
                "period": period,
                "domain": "three_platform_competitor",
                "document_ready": False,
            },
        }
    if on_progress:
        on_progress("取数已完成，正在生成三平台生意分析报告…")
    return {
        "ok": True,
        "markdown": format_three_platform_competitor_report(result),
        "meta": {
            "brand": result.get("brand") or brand,
            "period": period,
            "period_meta": result.get("period_meta") or {},
            "domain": "three_platform_competitor",
            "document_ready": True,
            "document_title": f"{result.get('brand') or brand} {period} 三平台生意分析",
            "last_result_cache": {"three_platform_competitor_result": result},
        },
    }
