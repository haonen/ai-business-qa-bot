from __future__ import annotations

import os
import re

from bot.market_plan import explicit_category, explicit_platform, explicit_segment
from bot.routing_contracts import CommerceScope, MarketScope, MediaScope, RouteDecision
from bot.key_driver_ontology import detect_key_drivers, has_explicit_media_intent, inferred_key_driver_platform


BUSINESS_WORDS = ("生意", "经营", "商品", "产品表现", "gmv")


def question_mode(text: str) -> str:
    value = text.casefold()
    if any(word in value for word in ("策略", "怎么做", "如何成为", "公司做了什么")):
        return "strategy"
    if any(word in value for word in ("为什么", "原因", "归因", "驱动")):
        return "explanation"
    if any(word in value for word in ("top", "排名", "最好", "最大", "最高")):
        return "ranking"
    if any(word in value for word in ("对比", "比较", "vs", "同比")):
        return "comparison"
    if any(word in value for word in ("报告", "分析")):
        return "report"
    return "lookup"


def ranking_semantics(text: str) -> tuple[str | None, int | None]:
    value = text.casefold()
    metric = None
    if any(word in value for word in ("涨幅", "增速", "同比最高", "增长率")):
        metric = "evol"
    elif any(word in value for word in ("涨得最好", "拉动最大", "增长额", "贡献最大", "变化贡献")):
        metric = "gmv_growth"
    elif "top" in value or "排名" in value or "生意最好" in value:
        metric = "gmv_actual"
    match = re.search(r"top\s*(\d+)", value, re.IGNORECASE)
    limit = max(1, min(int(match.group(1)), 20)) if match else (5 if metric else None)
    return metric, limit


def extract_brand_surface(text: str, period: str | None = None) -> str | None:
    """Keep only the user's written brand surface; source mapping happens later."""
    # The politeness/subject clause preceding the action verb is often more
    # than one token ("你现在能帮我算一下"), so the leading group must repeat
    # (`+`) rather than match once; a single optional match left phrases like
    # "帮我算一下花知晓..." with "帮我算一下" still glued to the brand.
    value = re.sub(
        r"^\s*(?:(?:你好|你现在能|你现在|你可以|你能|你|请|麻烦|帮我|帮忙|再|先)\s*)*"
        r"(?:重新分析|分析一下|分析|算一下|计算一下|再算一下|做一下|做|看一下|看看|看下|看|生成)?\s*",
        "", text, flags=re.I,
    )
    if period:
        value = value.replace(period, " ")
    # Store-type suffixes ("欧莱雅官方旗舰店") are common phrasing but not part
    # of the brand name, and can sit mid-string between the brand and a
    # trailing date/business clause rather than at an edge a cut-marker
    # would catch.
    value = re.sub(r"官方旗舰店|海外旗舰店|全球旗舰店|旗舰店|专卖店|自营店|官方店", " ", value)
    # Dates can precede the brand (e.g. “分析2026年3月谷雨的媒体投资”). Alternatives
    # are ordered longest/most-specific first: a bare "\d{1,2}月" alternative
    # placed before the day-range or single-date alternatives would win the
    # match first and strip only "8月" out of "8月20日" or "8月20日至25日",
    # leaving "20日"/"20日至25日" attached to the extracted brand surface.
    value = re.sub(
        r"(?:20\d{2}\s*年)?\s*(?:"
        r"Q[1-4]|q[1-4]|"
        r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]?\s*[-—至到]+\s*\d{1,2}\s*月\s*\d{1,2}\s*[日号]?|"
        r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]?\s*[-—至到]+\s*\d{1,2}\s*[日号]?|"
        r"\d{1,2}\s*月\s*至\s*\d{1,2}\s*月|"
        r"\d{1,2}\s*[-—至到]\s*\d{1,2}\s*月|"
        r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]?|"
        r"\d{1,2}\s*月"
        r")",
        " ", value,
    )
    markers = [
        r"20\d{2}", r"\d{4}\s*年", r"\d{1,2}\s*月", r"618", r"双\s*11",
        r"天猫", r"抖音", r"京东", r"三平台", r"全平台", r"TMALL", r"DOUYIN", r"JINGDONG",
        r"BET", r"媒体", r"投放", r"生意", r"经营", r"GMV", r"商品", r"大盘", r"市场",
        r"表现", r"重点看", r"情况",
        # Quantity-style asks ("生意是多少"/"卖了多少钱") close with a different
        # verb than the descriptive markers above; without these, texts that
        # have no earlier marker (e.g. no platform word) keep the whole
        # quantity clause attached to the brand candidate.
        r"是多少", r"多少钱", r"多少", r"销售额", r"销量",
        # A demonstrative back-reference ("在这个期间的gmv") points at context
        # the caller already has, not at a new brand token; cutting here
        # keeps it from being read as part of the brand surface.
        r"这个", r"该", r"期间", r"里面", r"时候",
    ]
    hit = re.search("|".join(f"(?:{marker})" for marker in markers), value, re.I)
    candidate = value[:hit.start()] if hit else value
    candidate = re.sub(r"(?:的|在|于|里面|平台|\s|,|，|:|：)+$", "", candidate).strip().strip("'\"“”‘’()（）")
    if candidate.startswith("按") and "分析" in value:
        after_analysis = value.rsplit("分析", 1)[-1].strip()
        next_hit = re.search("|".join(f"(?:{marker})" for marker in markers), after_analysis, re.I)
        candidate = (after_analysis[:next_hit.start()] if next_hit else after_analysis).strip()
    candidate = re.split(r"[,，。]|同比|环比", candidate, maxsplit=1)[0].strip()
    if not candidate or len(candidate) > 40:
        return None
    if candidate.casefold() in {
        "分析", "报告", "品牌", "这个品牌", "哪些品牌", "整体", "总体", "只看", "仅看",
    }:
        return None
    return candidate


def _commerce_platform(text: str, has_business: bool) -> str | None:
    if not has_business:
        return None
    mentioned = set()
    if re.search(r"天猫|tmall|\btm\b", text, re.I):
        mentioned.add("TM")
    if re.search(r"抖音|douyin|\bdy\b", text, re.I):
        mentioned.add("DY")
    if re.search(r"京东|jingdong|\bjd\b", text, re.I):
        mentioned.add("JD")
    if "三平台" in text or "全平台" in text or len(mentioned) >= 2:
        return "TTL"
    return explicit_platform(text) or inferred_key_driver_platform(text)


def _explicit_channel_media(text: str) -> tuple[str | None, list[str]]:
    value = text.casefold()
    douyin_mentions = len(re.findall(r"抖音|douyin|\bdy\b", value))
    douyin_media = bool(
        re.search(r"(?:只看|仅看|只分析).{0,8}(?:抖音|douyin|\bdy\b)", value)
        or re.search(r"(?:抖音|douyin|\bdy\b)相关(?:的)?(?:bet|媒体|投放)", value)
        or (
            douyin_mentions >= 2
            and re.search(r"(?:抖音|douyin|\bdy\b).{0,5}(?:投放|媒体|花费|bet|kol|ksi)", value)
        )
    )
    red_media = bool(re.search(r"(?:小红书|red).{0,5}(?:投放|媒体|花费|bet|kol|ksi)", value))
    channels = (["DOUYIN"] if douyin_media else []) + (["RED"] if red_media else [])
    if channels:
        return "CHANNEL_ONLY", channels
    if any(word in value for word in ("整体bet", "总体bet", "全盘bet", "overall bet")):
        return "OVERALL_BET", []
    return None, []


def build_route_decision(
    text: str,
    period: str | None,
    *,
    brand_surface: str | None = None,
    time_detected: bool = False,
) -> RouteDecision | None:
    value = text.strip()
    lowered = value.casefold()
    possible_brand = brand_surface if brand_surface is not None else extract_brand_surface(value, period)
    driver_match = detect_key_drivers(value)
    has_business = any(word in lowered for word in BUSINESS_WORDS) or bool(driver_match.drivers)
    has_media = has_explicit_media_intent(value)
    has_market = "大盘" in value or "市场整体" in value or "整体市场" in value
    ranking_metric, ranking_limit = ranking_semantics(value)
    if ranking_metric and any(word in value for word in ("品牌", "牌子")):
        has_market = True
    if not (has_business or has_media or has_market) and possible_brand and (period or time_detected) and any(
        word in value for word in ("怎么样", "表现", "分析", "报告", "看看")
    ):
        has_business = True
    if not (has_business or has_media or has_market):
        return None

    brand = None if has_market else possible_brand
    platform = _commerce_platform(value, has_business)
    mode, channels = _explicit_channel_media(value)
    mode_ambiguous = has_media and ("抖音" in value or "douyin" in lowered) and mode is None

    if has_market:
        segment = explicit_segment(value)
        category = explicit_category(value)
        if re.search(r"\bmass\s+top\s*(?:\d+)?\s*(?:品牌)?", lowered):
            segment = segment or "PURE MASS"
        market_platform = explicit_platform(value)
        missing = [
            name for name, present in (
                ("period", period), ("commerce_scope.platforms", market_platform),
                ("market_scope.segment", segment), ("market_scope.category", category),
            ) if not present
        ]
        mode_name = question_mode(value)
        deep_dive = bool(
            ranking_metric
            and (
                mode_name in {"strategy", "explanation"}
                or any(word in lowered for word in (
                    "选品", "生意节奏", "值得学习", "如何成为",
                    "再往下分析", "再往下看", "增长最多的平台",
                    "他们三平台", "分别在天猫", "下钻", "比较三平台",
                ))
            )
        )
        include_bet = has_media
        reasons = ["MARKET_SCOPE_EXPLICIT_REQUIRED"] if missing else ["MARKET_SCOPE_COMPLETE"]
        if deep_dive or include_bet:
            reasons.append("COMPLEX_MARKET_PLAN")
        if include_bet and re.search(
            r"今年.*(?:最新|bet)|(?:最新|ytd).*(?:bet|媒体)", lowered, re.I,
        ):
            reasons.append("BET_LATEST_YTD_REQUESTED")
        executable_market_plan = (
            os.environ.get("EXECUTABLE_PLAN_V2_ENABLED", "0").strip().lower()
            in {"1", "true", "yes", "on"}
            and not missing and (deep_dive or include_bet)
        )
        return RouteDecision(
            intents=["MARKET", "BET"] if include_bet else ["MARKET"], question_mode=mode_name,
            action=(
                "clarify_market_scope" if missing
                else "market_brand_deep_dive" if executable_market_plan
                else "clarify_analysis_scope" if deep_dive or include_bet
                else "market_analysis"
            ),
            period=period, commerce_scope=CommerceScope([market_platform] if market_platform else []),
            media_scope=MediaScope("OVERALL_BET", []) if include_bet else MediaScope(),
            market_scope=MarketScope(segment, category), ranking_metric=ranking_metric,
            ranking_limit=ranking_limit, missing_slots=missing,
            requires_confirmation=bool(missing or ((deep_dive or include_bet) and not executable_market_plan)),
            confidence_level="ambiguous" if missing else "exact",
            reason_codes=reasons,
        )

    intents = (["EC_BUSINESS"] if has_business else []) + (["BET"] if has_media else [])
    missing = []
    if not brand:
        missing.append("brand_surface")
    if not period:
        missing.append("period")
    if has_business and not platform:
        missing.append("commerce_scope.platforms")

    if has_media:
        if mode_ambiguous or ("抖音" in value and not mode):
            missing.append("media_scope.mode")
        elif not mode:
            mode = "OVERALL_BET"

    action = "guide"
    reasons = []
    confirmation = False
    if intents == ["EC_BUSINESS", "BET"]:
        confirmation = True
        action = (
            "clarify_v2_brand" if "brand_surface" in missing else
            "clarify_media_scope" if "media_scope.mode" in missing else
            "clarify_v2_period" if "period" in missing else
            "clarify_analysis_scope"
        )
        reasons.append("COMPOSITE_REQUIRES_CONFIRMATION")
    elif intents == ["BET"]:
        action = (
            "clarify_v2_brand" if "brand_surface" in missing else
            "clarify_media_scope" if "media_scope.mode" in missing else
            "clarify_v2_period" if "period" in missing else
            "media_analysis"
        )
        confirmation = "media_scope.mode" in missing
    elif intents == ["EC_BUSINESS"]:
        if "brand_surface" in missing and platform == "TTL":
            action = "clarify_three_platform_brand"
        elif "brand_surface" in missing:
            action = "clarify_v2_brand"
        elif "commerce_scope.platforms" in missing:
            action = "clarify_ec_platform"
        elif "period" in missing:
            action = {
                "DY": "clarify_douyin_period", "JD": "clarify_jd_period",
                "TTL": "clarify_three_platform_period",
            }.get(platform, "clarify_period")
        else:
            action = {"TM": "default_chain", "DY": "douyin_business_analysis", "JD": "jd_business_analysis", "TTL": "three_platform_competitor_analysis"}.get(platform, "guide")

    unsupported = []
    mode_name = question_mode(value)
    if mode_name in {"strategy", "explanation"}:
        unsupported = ["causal_attribution", "company_action_inference"]
        reasons.append("OBSERVATIONAL_ANALYSIS_ONLY")
    return RouteDecision(
        intents=intents, question_mode=mode_name, action=action,
        brand_surface=brand, period=period,
        commerce_scope=CommerceScope([platform] if platform else []),
        media_scope=MediaScope(mode, channels), missing_slots=missing,
        unsupported_requests=unsupported, requires_confirmation=confirmation,
        confidence_level="ambiguous" if missing else "exact", reason_codes=reasons,
    )
