from __future__ import annotations

from bot.tools.query_category import query_category
from bot.tools.query_compare import query_compare
from bot.tools.query_driver import query_driver
from bot.tools.query_kol import query_kol
from bot.tools.query_scene_tag import query_scene_tag
from bot.tools.query_series import query_series
from bot.tools.query_sku_list import query_sku_list


TOOL_REGISTRY = {
    "query_category": query_category,
    "query_driver": query_driver,
    "query_sku_list": query_sku_list,
    "query_series": query_series,
    "query_kol": query_kol,
    "query_compare": query_compare,
    "query_scene_tag": query_scene_tag,
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

