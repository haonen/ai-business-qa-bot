from __future__ import annotations

from bot.tools.query_category import query_category
from bot.tools.query_compare import query_compare
from bot.tools.query_driver import query_driver
from bot.tools.query_ec_nso import query_ec_nso
from bot.tools.query_ecip_tmall_gmv import query_ecip_tmall_gmv
from bot.tools.query_kol_performance import query_kol_performance
from bot.tools.query_media_investment import query_media_investment
from bot.tools.query_scene_tag import query_scene_tag
from bot.tools.query_social_search import query_social_search
from bot.tools.query_series import query_series
from bot.tools.query_sku_list import query_sku_list
from bot.tools.query_tmall_gmv import query_tmall_gmv
from bot.tools.query_douyin_gmv import query_douyin_gmv
from bot.tools.query_ec_followup_table import query_ec_followup_table
from bot.tools.query_bet_followup_table import query_bet_followup_table
from bot.tools.query_change_contribution import query_change_contribution
from bot.tools.query_ec_bet_monthly import query_ec_bet_monthly
from bot.tools.query_market_trend import query_market_trend
from bot.tools.query_market_top_brands import query_market_top_brands


TOOL_REGISTRY = {
    "query_category": query_category,
    "query_driver": query_driver,
    "query_sku_list": query_sku_list,
    "query_series": query_series,
    "query_compare": query_compare,
    "query_scene_tag": query_scene_tag,
    "query_ec_nso": query_ec_nso,
    "query_ecip_tmall_gmv": query_ecip_tmall_gmv,
    "query_social_search": query_social_search,
    "query_media_investment": query_media_investment,
    "query_tmall_gmv": query_tmall_gmv,
    "query_douyin_gmv": query_douyin_gmv,
    "query_kol_performance": query_kol_performance,
    "query_ec_followup_table": query_ec_followup_table,
    "query_bet_followup_table": query_bet_followup_table,
    "query_change_contribution": query_change_contribution,
    "query_ec_bet_monthly": query_ec_bet_monthly,
    "query_market_trend": query_market_trend,
    "query_market_top_brands": query_market_top_brands,
}


def get_langchain_tools():
    try:
        from langchain_core.tools import StructuredTool
    except Exception:
        return []
    return [
        StructuredTool.from_function(fn, name=name, description=(fn.__doc__ or name))
        for name, fn in TOOL_REGISTRY.items()
    ]
