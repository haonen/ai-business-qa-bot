from __future__ import annotations

from dataclasses import asdict, dataclass
import re

from bot.media_period import normalize_media_period_hint
from bot.platforms import canonical_platform


SEGMENT_ALIASES = {
    "BEAUTY MARKET": "BEAUTY MARKET", "BEAUTYMARKET": "BEAUTY MARKET",
    "TOTAL BEAUTY MARKET": "BEAUTY MARKET", "TOTAL BEAUTY": "BEAUTY MARKET",
    "整体美妆市场": "BEAUTY MARKET", "美妆全市场": "BEAUTY MARKET",
    "PURE MASS": "PURE MASS", "PUREMASS": "PURE MASS", "PURE-MASS": "PURE MASS",
    "MASS": "PURE MASS",
    "大众": "PURE MASS", "大众美妆": "PURE MASS", "纯大众": "PURE MASS",
    "SELECTIVE": "SELECTIVE", "高端": "SELECTIVE", "高端美妆": "SELECTIVE",
    "PROFESSIONAL": "PROFESSIONAL", "专业": "PROFESSIONAL", "专业美妆": "PROFESSIONAL",
}
CATEGORY_ALIASES = {
    "TTL BEAUTY": "TOTAL BEAUTY", "TOTAL BEAUTY": "TOTAL BEAUTY",
    "MASS BEAUTY": "TOTAL BEAUTY",
    "全美妆": "TOTAL BEAUTY", "美妆全品类": "TOTAL BEAUTY",
    "FEMALE SKINCARE": "FEMALE SKINCARE", "女士护肤": "FEMALE SKINCARE",
    "女护肤": "FEMALE SKINCARE", "护肤": "FEMALE SKINCARE",
    "MAKEUP": "MAKEUP", "彩妆": "MAKEUP",
    "HAIR": "HAIR", "HAIRCARE": "HAIR", "美发": "HAIR", "护发": "HAIR",
    "MALE SKINCARE": "MALE SKINCARE", "MEX": "MALE SKINCARE",
    "男士护肤": "MALE SKINCARE", "男护肤": "MALE SKINCARE",
}
PLATFORM_ALIASES = {
    "TTL": "TTL", "三平台": "TTL", "全平台": "TTL", "整体": "TTL",
    "TM": "TM", "TMALL": "TM", "天猫": "TM",
    "DY": "DY", "DOUYIN": "DY", "抖音": "DY",
    "JD": "JD", "JINGDONG": "JD", "京东": "JD",
}
MARKET_HINTS = (
    "大盘", "市场整体", "整体市场", "市场涨跌", "市场趋势",
    "Total Beauty", "Beauty Market",
)


@dataclass(frozen=True)
class MarketPlan:
    intent: str
    period: str | None
    segment: str = "PURE MASS"
    platform: str = "TTL"
    category: str = "TOTAL BEAUTY"
    view: str = "summary"
    ranking_metric: str = "gmv_actual"
    ranking_limit: int = 5
    include_bet: bool = False
    bet_latest_ytd: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def is_market_question(text: str) -> bool:
    value = str(text or "")
    lowered = value.casefold()
    return any(hint.casefold() in lowered for hint in MARKET_HINTS) or bool(
        re.search(
            r"哪些(?:品牌|牌子)|(?:品牌|牌子).*(?:涨得最好|涨幅最高|增速最快|"
            r"拉动最大|增长贡献最大|生意最好|Top\s*5)|"
            r"(?:生意|增长|涨幅|增速).*(?:最好|最高|最快).*(?:品牌|牌子)|"
            r"(?:天猫|抖音|京东|三平台|mass\s*)?\s*Top\s*(?:\d+|品牌)",
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


def explicit_segment(text: str) -> str | None:
    combined = str(text or "").upper().replace("_", " ")
    for alias, canonical in SEGMENT_ALIASES.items():
        if alias.upper() == "TOTAL BEAUTY":
            continue
        if alias.upper() in combined:
            return canonical
    return None


def explicit_category(text: str) -> str | None:
    combined = str(text or "").upper().replace("_", " ").replace("TOTAL BEAUTY MARKET", "")
    for alias, canonical in CATEGORY_ALIASES.items():
        if alias.upper() in combined:
            return canonical
    return None


def normalize_category(value: str | None, text: str = "") -> str:
    return explicit_category(text) or explicit_category(value or "") or "TOTAL BEAUTY"


def normalize_platform(value: str | None, text: str = "") -> str:
    combined = f"{value or ''} {text}".upper().strip()
    # Explicit platform names beat generic words such as “整体”.
    for alias in ("天猫", "TMALL", "TM", "抖音", "DOUYIN", "DY", "京东", "JINGDONG", "JD"):
        if alias.upper() in combined:
            return PLATFORM_ALIASES[alias]
    for alias, canonical in PLATFORM_ALIASES.items():
        if alias.upper() in combined:
            return canonical
    try:
        return canonical_platform(value)
    except ValueError:
        pass
    return "TTL"


def explicit_platform(text: str) -> str | None:
    """Return only a platform explicitly written in the current user turn."""
    combined = str(text or "").upper()
    for alias in ("天猫", "TMALL", "TM", "抖音", "DOUYIN", "DY", "京东", "JINGDONG", "JD"):
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
    category: str | None = None,
    intent: str | None = None,
    view: str | None = None,
    ranking_metric: str | None = None,
) -> MarketPlan:
    value = str(text or "")
    ranking = bool(
        (any(noun in value for noun in ("品牌", "牌子")) and any(word in value for word in ("最好", "最高", "最快", "Top", "TOP", "排名", "拉动", "涨幅", "增速")))
        or re.search(r"哪些(?:品牌|牌子)|第[一二三四五1-5]名|Top\s*(?:\d+|品牌)", value, re.IGNORECASE)
    )
    deep_dive = ranking and any(
        token.casefold() in value.casefold()
        for token in (
            "跨三平台", "三平台分析", "平台、选品", "平台、 选品",
            "选品", "生意节奏", "价格和促销", "价格与促销", "促销",
            "值得学习", "学习的点", "增长来自哪里",
            "如何成为", "怎么成为", "为什么是top", "为何是top",
        )
    )
    resolved_intent = (
        "market_brand_deep_dive" if deep_dive else
        intent if intent in {"market_analysis", "market_brand_ranking", "market_brand_deep_dive"} else
        "market_brand_ranking" if ranking else "market_analysis"
    )
    resolved_metric = ranking_metric if ranking_metric in {"gmv_actual", "gmv_growth", "evol"} else (
        "evol" if any(word in value for word in ("涨幅", "增速", "同比最高", "增长率")) else
        "gmv_growth" if any(word in value for word in ("增长", "增量", "拉动", "涨得")) else
        "gmv_actual"
    )
    top_match = re.search(r"Top\s*(\d+)", value, re.IGNORECASE)
    resolved_limit = max(1, min(int(top_match.group(1)), 20)) if top_match else 5
    resolved_view = view if view in {"summary", "monthly_trend", "top_brands"} else (
        "top_brands" if resolved_intent in {"market_brand_ranking", "market_brand_deep_dive"} else
        "monthly_trend" if any(word in value for word in ("按月", "by month", "趋势")) else "summary"
    )
    written_platform = explicit_platform(value)
    # 大盘趋势默认看三平台；Top品牌默认看天猫Pure Mass。
    # 只有本轮明确写了平台，才覆盖这两个默认口径。
    resolved_platform = written_platform or (
        "TTL" if resolved_intent == "market_brand_deep_dive" and (
            resolved_metric == "gmv_actual" or "三平台" in value or "跨三平台" in value
        ) else
        "TTL" if resolved_intent == "market_brand_ranking" and resolved_metric == "gmv_actual" and (
            bool(re.search(r"Top\s*\d+", value, re.IGNORECASE))
            or "mass beauty" in value.casefold() or "pure mass" in value.casefold()
        ) else
        "TM" if resolved_intent in {"market_brand_ranking", "market_brand_deep_dive"}
        else normalize_platform(platform, value)
    )
    return MarketPlan(
        intent=resolved_intent,
        period=period or normalize_media_period_hint(value),
        segment=normalize_segment(segment, value),
        platform=resolved_platform,
        category=normalize_category(category, value),
        view=resolved_view,
        ranking_metric=resolved_metric,
        ranking_limit=resolved_limit,
    )
