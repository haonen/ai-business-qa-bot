from __future__ import annotations


MARKET_DOCUMENT_ROUTES = frozenset({
    "market_analysis", "market_brand_ranking", "market_brand_deep_dive",
})

BRAND_DOCUMENT_ROUTES = frozenset({
    "default_chain", "brand_business_investment_analysis",
    "douyin_business_analysis", "jd_business_analysis",
    "three_platform_competitor_analysis", "brand_platform_deep_dive",
    "multi_period_business_analysis", "media_analysis", "skill_dispatch",
})

PLATFORM_LABELS = {
    "TM": "天猫",
    "DY": "抖音",
    "JD": "京东",
    "TTL": "三平台",
}


def should_generate_document(route_type: str | None, report_meta: dict) -> bool:
    """Keep document delivery policy independent from request execution."""
    if not report_meta.get("document_ready", True):
        return False
    if route_type in MARKET_DOCUMENT_ROUTES:
        return True
    return bool(route_type in BRAND_DOCUMENT_ROUTES and report_meta.get("brand"))


def document_title(route_type: str | None, report_meta: dict) -> str:
    brand = report_meta.get("brand")
    period = report_meta.get("period")
    if route_type in MARKET_DOCUMENT_ROUTES:
        return report_meta.get("document_title") or f"{period} 大盘分析"
    if route_type == "skill_dispatch":
        return report_meta.get("document_title") or f"{brand} {period} 数据分析"
    if route_type == "brand_business_investment_analysis":
        return report_meta.get("document_title") or f"{brand} {period} 生意与BET投资联合分析"
    if route_type == "media_analysis":
        return f"{brand} {report_meta.get('period_display') or period} BET媒体投资分析报告"
    if route_type == "douyin_business_analysis":
        return f"{brand} {period} 抖音生意分析报告"
    if route_type == "jd_business_analysis":
        return f"{brand} {period} 京东品牌生意分析"
    if route_type == "three_platform_competitor_analysis":
        return report_meta.get("document_title") or f"{brand} {period} 三平台生意分析"
    if route_type == "brand_platform_deep_dive":
        return report_meta.get("document_title") or f"{brand} {period} 三平台比较与动态下钻"
    if route_type == "multi_period_business_analysis":
        platform = PLATFORM_LABELS.get(str(report_meta.get("platform") or "").upper(), "")
        platform_title = f" {platform}" if platform else ""
        return report_meta.get("document_title") or f"{brand} {period}{platform_title}多时段同比分析"
    return f"{brand} {period} 生意分析报告"
