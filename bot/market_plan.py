from __future__ import annotations

from dataclasses import asdict, dataclass
import re

from bot.media_period import normalize_media_period_hint


SEGMENT_ALIASES = {
    "PURE MASS": "PURE MASS", "PUREMASS": "PURE MASS", "PURE-MASS": "PURE MASS",
    "大众": "PURE MASS", "大众美妆": "PURE MASS", "纯大众": "PURE MASS",
    "SELECTIVE": "SELECTIVE", "高端": "SELECTIVE", "高端美妆": "SELECTIVE",
    "PROFESSIONAL": "PROFESSIONAL", "专业": "PROFESSIONAL", "专业美妆": "PROFESSIONAL",
}
PLATFORM_ALIASES = {
    "TTL": "TTL", "三平台": "TTL", "全平台": "TTL", "整体": "TTL",
    "TM": "TM", "TMALL": "TM", "天猫": "TM",
    "DY": "DY", "DOUYIN": "DY", "抖音": "DY",
    "JD": "JD", "京东": "JD",
}
MARKET_HINTS = ("大盘", "市场整体", "整体市场", "市场涨跌", "市场趋势")


@dataclass(frozen=True)
class MarketPlan:
    intent: str
    period: str | None
    segment: str = "PURE MASS"
    platform: str = "TTL"
    view: str = "summary"
    ranking_metric: str = "gmv_growth"

    def to_dict(self) -> dict:
        return asdict(self)


def is_market_question(text: str) -> bool:
    value = str(text or "")
    return any(hint in value for hint in MARKET_HINTS) or bool(
        re.search(
            r"哪些(?:品牌|牌子)|(?:品牌|牌子).*(?:涨得最好|涨幅最高|增速最快|"
            r"拉动最大|增长贡献最大|生意最好|Top\s*5)|"
            r"(?:生意|增长|涨幅|增速).*(?:最好|最高|最快).*(?:品牌|牌子)|"
            r"(?:天猫|抖音|京东|三平台)?\s*Top\s*5",
            value,
            re.IGNORECASE,
        )
    )


def normalize_segment(value: str | None, text: str = "") -> str:
    text_value = str(text or "").upper().replace("_", " ")
    for alias, canonical in SEGMENT_ALIASES.items():
        if alias.upper() in text_value:
            return canonical
    combined = str(value or "").upper().replace("_", " ").strip()
    for alias, canonical in SEGMENT_ALIASES.items():
        if alias.upper() in combined:
            return canonical
    return "PURE MASS"


def normalize_platform(value: str | None, text: str = "") -> str:
    combined = f"{value or ''} {text}".upper().strip()
    # Explicit platform names beat generic words such as “整体”.
    for alias in ("天猫", "TMALL", "TM", "抖音", "DOUYIN", "DY", "京东", "JD"):
        if alias.upper() in combined:
            return PLATFORM_ALIASES[alias]
    for alias, canonical in PLATFORM_ALIASES.items():
        if alias.upper() in combined:
            return canonical
    return "TTL"


def explicit_platform(text: str) -> str | None:
    """Return only a platform explicitly written in the current user turn."""
    combined = str(text or "").upper()
    for alias in ("天猫", "TMALL", "TM", "抖音", "DOUYIN", "DY", "京东", "JD"):
        if alias.upper() in combined:
            return PLATFORM_ALIASES[alias]
    for alias in ("三平台", "全平台", "TTL"):
        if alias.upper() in combined:
            return "TTL"
    return None


def build_market_plan(
    text: str,
    *,
    period: str | None = None,
    segment: str | None = None,
    platform: str | None = None,
    intent: str | None = None,
    view: str | None = None,
    ranking_metric: str | None = None,
) -> MarketPlan:
    value = str(text or "")
    ranking = bool(
        (any(noun in value for noun in ("品牌", "牌子")) and any(word in value for word in ("最好", "最高", "最快", "Top", "TOP", "排名", "拉动", "涨幅", "增速")))
        or re.search(r"哪些(?:品牌|牌子)|第[一二三四五1-5]名|Top\s*5", value, re.IGNORECASE)
    )
    resolved_intent = intent if intent in {"market_analysis", "market_brand_ranking"} else (
        "market_brand_ranking" if ranking else "market_analysis"
    )
    resolved_metric = ranking_metric if ranking_metric in {"gmv_growth", "evol"} else (
        "evol" if any(word in value for word in ("涨幅", "增速", "同比最高", "增长率")) else "gmv_growth"
    )
    resolved_view = view if view in {"summary", "monthly_trend", "top_brands"} else (
        "top_brands" if resolved_intent == "market_brand_ranking" else
        "monthly_trend" if any(word in value for word in ("按月", "by month", "趋势")) else "summary"
    )
    written_platform = explicit_platform(value)
    # 大盘趋势默认看三平台；Top品牌默认看天猫Pure Mass。
    # 只有本轮明确写了平台，才覆盖这两个默认口径。
    resolved_platform = written_platform or (
        "TM" if resolved_intent == "market_brand_ranking"
        else normalize_platform(platform, value)
    )
    return MarketPlan(
        intent=resolved_intent,
        period=period or normalize_media_period_hint(value),
        segment=normalize_segment(segment, value),
        platform=resolved_platform,
        view=resolved_view,
        ranking_metric=resolved_metric,
    )
