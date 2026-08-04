from __future__ import annotations

from bot.db.connection import fetch_df
from bot.tools.common import ec_query_context, tool
from bot.tools.ecip_ttl_gmv_blend import query_blended_tmall_ttl_gmv
from bot.utils import safe_evol


_TTL_CATEGORY_VALUES = (
    "Skincare",
    "Hair",
    "Makeup + Fragrance",
    "Makeup+Fragrance",
)


@tool
def query_ecip_tmall_gmv(
    brand: str,
    period: str,
    brand_aliases: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """Query ECIP MASS Pure Mass Market Ranking (TTL Beauty) Tmall TTL GMV."""
    try:
        context = ec_query_context(brand, period, brand_aliases=brand_aliases)
        blended = query_blended_tmall_ttl_gmv(
            fetch_df,
            brand=context["source_brand"],
            current_start=context["current_start"],
            current_end=context["current_end"],
            prior_start=context["prior_start"],
            prior_end=context["prior_end"],
        )
        current = blended["totals"]["current"]
        prior = blended["totals"]["prior"]
        if current["row_count"] == 0:
            return {
                "error": "missing_current",
                "message": (
                    f"品牌“{context['source_brand']}”在ECIP MASS Pure Mass Market "
                    "Ranking (TTL Beauty)本期没有"
                    "Skincare、Hair或Makeup + Fragrance数据，报告未生成。"
                ),
            }
        if prior["row_count"] == 0:
            return {
                "error": "missing_prior",
                "message": (
                    f"品牌“{context['source_brand']}”在ECIP MASS Pure Mass Market "
                    "Ranking (TTL Beauty)去年同期没有"
                    "Skincare、Hair或Makeup + Fragrance数据，报告未生成。"
                ),
            }
        return {
            "brand": context.get("input_brand", brand),
            "source_brand": context["source_brand"],
            "period_meta": context,
            "categories": list(_TTL_CATEGORY_VALUES[:3]),
            "current_row_count": current["row_count"],
            "prior_row_count": prior["row_count"],
            "monthly_rows": blended["rows"],
            "coverage": blended["coverage"],
            "source_tables": blended["sql_meta"],
            "total": {
                "gmv_current": round(current["gmv"]),
                "gmv_prior": round(prior["gmv"]),
                "evol": safe_evol(current["gmv"], prior["gmv"]),
            },
        }
    except Exception as exc:
        return {"error": "execution_error", "message": str(exc)}
