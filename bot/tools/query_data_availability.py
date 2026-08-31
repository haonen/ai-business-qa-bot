from __future__ import annotations

from datetime import date

from bot.db.connection import fetch_one
from bot.entity_resolution import resolve_entities
from bot.session import SessionState
from bot.tools.query_media_investment import latest_media_investment_month


PLATFORM_LABELS = {"TM": "天猫", "DY": "抖音", "JD": "京东"}


def _scope(text: str, state: SessionState) -> tuple[str | None, str | None, str]:
    value = str(text or "").casefold()
    domain = "bet" if "bet" in value or "媒体" in value else "ec"
    context = state.bet_context if domain == "bet" else state.ec_context
    platform = (
        "TM" if "天猫" in value or "tmall" in value else
        "DY" if "抖音" in value or "douyin" in value else
        "JD" if "京东" in value or " jd" in f" {value}" else
        context.platform or state.task_context.platform
    )
    entity_brand = resolve_entities(str(text or ""), current_year=date.today().year).brand
    explicit_brand = entity_brand.surface if entity_brand.status == "resolved" else None
    return explicit_brand or context.brand or state.task_context.brand, platform, domain


def query_data_availability(text: str, state: SessionState) -> dict:
    """Read only the latest covered date for the active, explicitly scoped source."""
    brand, platform, domain = _scope(text, state)
    if not brand:
        return {
            "markdown": "你想查哪个品牌的数据更新时间？",
            "meta": {"document_ready": False, "awaiting": "brand", "meta_subtype": "DATA_AVAILABILITY"},
        }
    if domain == "bet":
        source_brand = str(
            (state.bet_context.source_brands or {}).get("topline")
            or (state.bet_context.source_brands or {}).get("media_topline")
            or brand
        )
        latest = latest_media_investment_month(source_brand, date.today().year)
        if not latest:
            return {
                "markdown": f"暂时没有查到{brand}{date.today().year}年BET数据的最新覆盖月份。",
                "meta": {"document_ready": False, "meta_subtype": "DATA_AVAILABILITY", "brand": brand, "domain": "bet"},
            }
        return {
            "markdown": f"{brand}的BET Topline数据当前最新到{latest[:7]}。",
            "meta": {"document_ready": False, "meta_subtype": "DATA_AVAILABILITY", "brand": brand, "domain": "bet", "latest_date": latest},
        }
    if platform not in PLATFORM_LABELS:
        return {
            "markdown": "你想查天猫、抖音还是京东的数据更新时间？",
            "meta": {"document_ready": False, "awaiting": "platform", "meta_subtype": "DATA_AVAILABILITY", "brand": brand},
        }
    source_brand = str((state.ec_context.source_brands or {}).get(platform) or brand)
    sql = {
        "TM": "SELECT MAX(CAST(bus_date AS DATE)) AS max_date FROM ai_bot_tmall_product_link WHERE brand_name = :brand",
        "DY": "SELECT MAX(`业务日期`) AS max_date FROM ai_bot_dy_product_link WHERE `商品品牌` = :brand",
        "JD": "SELECT MAX(CAST(bus_date AS DATE)) AS max_date FROM jd_store_ranking_selfrun_day_jiashicang WHERE TRIM(brand_name) = :brand",
    }[platform]
    try:
        latest = fetch_one(sql, {"brand": source_brand}).get("max_date")
    except Exception as exc:
        return {
            "markdown": "数据覆盖查询失败，请稍后再试。",
            "meta": {"document_ready": False, "meta_subtype": "DATA_AVAILABILITY", "error": str(exc)},
        }
    if not latest:
        markdown = f"没有查到{brand}在{PLATFORM_LABELS[platform]}的数据覆盖日期。"
    else:
        markdown = f"{brand}的{PLATFORM_LABELS[platform]}数据当前最新到{str(latest)[:10]}。"
    return {
        "markdown": markdown,
        "meta": {
            "document_ready": False, "meta_subtype": "DATA_AVAILABILITY",
            "brand": brand, "platform": platform, "domain": "ec",
            "latest_date": str(latest)[:10] if latest else None,
        },
    }
