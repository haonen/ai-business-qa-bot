from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from bot.chains.default_chain import run_default_chain
from bot.chains.douyin_business_chain import run_douyin_business_chain
from bot.chains.jd_business_chain import run_jd_business_chain
from bot.chains.media_chain import run_media_chain


PLATFORM_LABELS = {"TM": "天猫", "DY": "抖音", "JD": "京东"}


def run_brand_business_investment_chain(
    brand: str,
    period: str,
    platform: str,
    *,
    brand_aliases: list[str] | tuple[str, ...] | None = None,
    on_progress=None,
) -> dict:
    """Run one explicitly selected commerce report and the full BET report."""
    platform = str(platform or "").upper()
    business_runners = {
        "TM": run_default_chain,
        "DY": run_douyin_business_chain,
        "JD": run_jd_business_chain,
    }
    if platform not in business_runners:
        return {
            "ok": False,
            "markdown": "请先确认要看天猫、抖音还是京东的生意表现。",
            "meta": {"brand": brand, "period": period, "document_ready": False},
        }
    if on_progress:
        on_progress(f"正在并行查询{PLATFORM_LABELS[platform]}生意与BET媒体投资…")
    aliases = list(brand_aliases or [])
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="business-bet") as executor:
        business_future = executor.submit(
            business_runners[platform], brand, period,
            brand_aliases=aliases,
        )
        bet_future = executor.submit(
            run_media_chain, brand, period,
            brand_aliases=aliases,
            media_scope="full_bet",
        )
        business_result = business_future.result()
        bet_result = bet_future.result()

    sections = [f"# {brand} {period} {PLATFORM_LABELS[platform]}生意与BET投资联合分析"]
    sections.extend([
        f"## 第一部分｜{PLATFORM_LABELS[platform]}生意与主推商品",
        business_result.get("markdown") or "生意数据未返回可用结果。",
        "## 第二部分｜BET媒体投资",
        bet_result.get("markdown") or "BET数据未返回可用结果。",
    ])
    business_meta = business_result.get("meta") or {}
    bet_meta = bet_result.get("meta") or {}
    resolved_brand = business_meta.get("brand") or bet_meta.get("brand") or brand
    return {
        "ok": bool(business_result.get("ok") or bet_result.get("ok")),
        "markdown": "\n\n".join(sections),
        "meta": {
            "brand": resolved_brand,
            "period": period,
            "platform": platform,
            "brand_aliases": aliases,
            "document_ready": bool(business_result.get("ok") or bet_result.get("ok")),
            "document_title": f"{resolved_brand} {period} {PLATFORM_LABELS[platform]}生意与BET投资联合分析",
            "domain": "business_bet",
            "business_ok": bool(business_result.get("ok")),
            "bet_ok": bool(bet_result.get("ok")),
            "business_meta": business_meta,
            "bet_meta": bet_meta,
            "last_result_cache": {
                "business": business_meta.get("last_result_cache"),
                "bet": bet_meta.get("last_result_cache"),
            },
        },
    }
