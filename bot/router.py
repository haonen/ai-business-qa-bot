from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import logging
import os
import re

from bot.session import SessionState
from bot.media_period import normalize_media_period_hint
from bot.utils import detect_brand_hint, extract_json_object, llm_client, normalize_period_hint
from bot.followup_plan import is_narrow_followup
from bot.market_plan import build_market_plan, is_market_question


log = logging.getLogger(__name__)


META_HINTS = [
    "你还会", "你会什么", "你会干什么", "你能干什么", "你能做什么",
    "你可以做什么", "有什么功能", "能分析什么", "怎么用", "你是谁",
    "支持哪些", "有什么限制",
]
CALIBER_HINTS = ["刷单", "口径", "对不上", "数据准", "为什么不一样", "情报通和"]
DRIVER_ALIASES = {"李佳琦": "李佳琦", "佳琦": "李佳琦", "T2": "T2", "Non-KOL": "Non-KOL"}
FUNCTION_HINTS = ["美白", "抗老", "保湿", "修护", "控油祛痘", "礼赠", "防晒"]
VIEW_HINTS = {
    "playbook": ["打法", "推什么", "人群", "场景"],
    "driver": ["key driver", "keydriver", "渠道", "渠道贡献", "渠道拆分", "驱动", "李佳琦", "T2", "达人", "自播", "店播", "Non-KOL", "non-kol"],
    "sku": ["sku", "SKU", "链接", "top链接", "Top链接", "单品", "产品"],
    "category": ["品类", "类目"],
}
MEDIA_HINTS = [
    "媒体投资", "媒体花费", "媒体费用", "媒体费比", "站外投放", "站外投资",
    "bet", "bkfs", "bkfst", "social search", "socialsearch", "search report",
    "社交搜索", "搜索指数", "媒体表现", "小红书投放", "抖音投放",
    "小红书花费", "抖音花费", "ksi", "kol performance", "kol表现", "kol花费",
    "达人投放", "达人花费", "engage", "cpe",
    "take rate", "take-rate", "take_rate", "bet%",
]


@dataclass
class FilterUpdate:
    matched: bool = False
    view: str | None = None
    period: str | None = None
    category: str | None = None
    key_driver: str | None = None
    function_tag: str | None = None
    series: str | None = None


@dataclass
class RouteResult:
    type: str
    brand: str | None = None
    period: str | None = None
    brand_aliases: list[str] | None = None
    media_scope: str | None = None
    followup_text: str | None = None
    segment: str | None = None
    platform: str | None = None
    market_view: str | None = None
    ranking_metric: str | None = None
    ranking_limit: int | None = None
    preflight_target: str | None = None
    message: str | None = None
    update: FilterUpdate | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        if self.update:
            data["update"] = asdict(self.update)
        return data


@dataclass
class IntentResult:
    intent: str
    brand: str | None = None
    brand_cn: str | None = None
    brand_en: str | None = None
    brand_aliases: list[str] | None = None
    period: str | None = None
    media_scope: str | None = None
    followup_text: str | None = None
    segment: str | None = None
    platform: str | None = None
    view: str | None = None
    ranking_metric: str | None = None
    confidence: str = "low"


def is_meta_question(text: str) -> bool:
    return any(h in text for h in META_HINTS)


def is_data_caliber_question(text: str) -> bool:
    return any(h in text for h in CALIBER_HINTS)


def is_media_question(text: str) -> bool:
    lowered = (text or "").lower()
    return any(h.lower() in lowered for h in MEDIA_HINTS) or bool(
        re.search(r"(?<![a-z])tr(?![a-z])", lowered)
    )


def _explicit_business_platform(text: str) -> str | None:
    lowered = str(text or "").lower()
    if "天猫" in lowered or bool(re.search(r"(?<![a-z])tmall(?![a-z])|(?<![a-z])tm(?![a-z])", lowered)):
        return "TM"
    if "抖音" in lowered or bool(re.search(r"(?<![a-z])dy(?![a-z])", lowered)):
        return "DY"
    if "京东" in lowered or bool(re.search(r"(?<![a-z])jd(?![a-z])", lowered)):
        return "JD"
    return None


def is_business_investment_question(text: str) -> bool:
    """Brand business/product performance plus BET investment in one request."""
    lowered = str(text or "").lower()
    has_business = any(token in lowered for token in (
        "生意", "主推商品", "商品表现", "产品表现", "gmv", "经营",
    ))
    has_investment = is_media_question(text) or any(token in lowered for token in (
        "投资情况", "投资表现", "投放情况", "投放表现",
    ))
    return has_business and has_investment


def _is_confirm_reply(text: str) -> bool:
    value = re.sub(r"[，,。！？!?\s]+", "", str(text or "")).lower()
    return any(token in value for token in ("确认", "可以", "开始", "继续", "按这个", "按此", "好的", "好", "是的"))


def _is_cancel_reply(text: str) -> bool:
    value = re.sub(r"[，,。！？!?\s]+", "", str(text or "")).lower()
    return any(token in value for token in ("取消", "不用了", "不分析", "算了"))


def _preflight_message(
    *, target: str, brand: str | None, period: str | None,
    platform: str | None = None, limit: int | None = None,
    ranking_metric: str | None = None,
) -> str:
    if target == "brand_business_investment_analysis":
        platform_label = {"TM": "天猫", "DY": "抖音", "JD": "京东"}.get(platform)
        scope = platform_label or "待确认平台"
        return (
            f"这是一个复合分析问题。我理解为：分析{brand or '该品牌'}在{period or '待确认期间'}的"
            f"{scope}生意与主推商品，同时查看BET媒体投资。\n\n"
            "**可以回答**\n"
            "- 所选平台的品牌GMV、同比、品类/渠道及现有商品级表现。\n"
            "- 同期BET媒体投资、平台/类型结构，以及可用的Search与KOL指标。\n"
            "- 分别指出生意增长项和投资变化。\n\n"
            "**不能直接回答**\n"
            "- 现有数据不能把某笔媒体投资与某个商品GMV逐笔归因，因此不会写“投资导致商品增长”或虚构ROI。\n"
            "- 数据源缺少的价格、优惠券或促销字段不会推断。\n\n"
            + (
                f"如口径正确，请回复“确认”；当前生意平台按{platform_label}执行。"
                if platform_label else
                "请回复“天猫，确认”“抖音，确认”或“京东，确认”。"
            )
        )
    metric_label = {
        "gmv_actual": "本期GMV规模",
        "gmv_growth": "GMV增长额",
        "evol": "同比增速",
    }.get(ranking_metric, "本期GMV规模")
    return (
        f"这是一个需要先确认口径的市场深度分析。我理解为：在{period or '待确认期间'}的Pure Mass Beauty中，"
        f"按TM＋DY＋JD三平台TTL的{metric_label}选Top {limit or 3}，再解释它们如何形成当前排名。\n\n"
        "**可以回答**\n"
        "- Top品牌排名、GMV、同比、增长额及三平台结构。\n"
        "- by month生意节奏，以及现有天猫/抖音商品数据中可验证的主推商品。\n"
        "- 用平台增量、月份变化和商品增量解释可观察到的排名支撑。\n\n"
        "**不能直接回答**\n"
        "- “如何成为Top”只能做数据分解，不能把相关性写成因果。\n"
        "- 当前缺少统一的京东商品明细、全平台价格/优惠券/促销历史时，不输出相关结论。\n\n"
        "如口径正确，请回复“确认”；如需改成增长额或同比增速排名，请直接说明。"
    )


def _sophisticated_preflight(text: str, state: SessionState) -> RouteResult | None:
    if is_business_investment_question(text):
        brand = detect_brand_hint(text) or state.drilldown_ctx.brand
        period = extract_period(text) or state.drilldown_ctx.period
        if not brand:
            return None
        platform = _explicit_business_platform(text)
        return RouteResult(
            type="clarify_analysis_scope", brand=brand, period=period,
            brand_aliases=[brand], platform=platform, media_scope="full_bet",
            preflight_target="brand_business_investment_analysis",
            message=_preflight_message(
                target="brand_business_investment_analysis", brand=brand,
                period=period, platform=platform,
            ),
        )
    if is_market_question(text) and re.search(
        r"Top\s*\d+.*(?:如何|怎么|为什么|为何)|(?:如何|怎么|为什么|为何).*Top\s*\d+",
        text, re.IGNORECASE,
    ):
        plan = build_market_plan(text)
        return RouteResult(
            type="clarify_analysis_scope", period=plan.period,
            segment=plan.segment, platform=plan.platform,
            market_view=plan.view, ranking_metric=plan.ranking_metric,
            ranking_limit=plan.ranking_limit,
            preflight_target="market_brand_deep_dive",
            message=_preflight_message(
                target="market_brand_deep_dive", brand=None, period=plan.period,
                limit=plan.ranking_limit, ranking_metric=plan.ranking_metric,
            ),
        )
    return None


def _resume_analysis_preflight(text: str, pending: dict) -> RouteResult | None:
    if _is_cancel_reply(text):
        return RouteResult(type="guide", message="已取消本次分析。")
    target = str(pending.get("target") or "")
    platform = _explicit_business_platform(text) or pending.get("platform")
    metric = str(pending.get("ranking_metric") or "gmv_actual")
    if any(token in text for token in ("增长额", "增量", "增长Top", "增长top")):
        metric = "gmv_growth"
    elif any(token in text for token in ("增速", "涨幅", "同比Top", "同比top")):
        metric = "evol"
    elif any(token in text for token in ("规模", "GMV", "gmv")):
        metric = "gmv_actual"
    if target == "brand_business_investment_analysis" and not platform:
        return RouteResult(
            type="clarify_analysis_scope", brand=pending.get("brand"),
            period=pending.get("period"), brand_aliases=list(pending.get("brand_aliases") or []),
            preflight_target=target,
            message=_preflight_message(
                target=target, brand=pending.get("brand"), period=pending.get("period"),
            ),
        )
    if _is_confirm_reply(text) or (target == "brand_business_investment_analysis" and platform):
        return RouteResult(
            type=target, brand=pending.get("brand"), period=pending.get("period"),
            brand_aliases=list(pending.get("brand_aliases") or []),
            platform=platform, segment=pending.get("segment"),
            market_view=pending.get("market_view"), ranking_metric=metric,
            ranking_limit=int(pending.get("ranking_limit") or 3), media_scope="full_bet",
        )
    return RouteResult(
        type="clarify_analysis_scope", brand=pending.get("brand"), period=pending.get("period"),
        brand_aliases=list(pending.get("brand_aliases") or []), platform=platform,
        segment=pending.get("segment"), market_view=pending.get("market_view"),
        ranking_metric=metric, ranking_limit=int(pending.get("ranking_limit") or 3),
        preflight_target=target,
        message=_preflight_message(
            target=target, brand=pending.get("brand"), period=pending.get("period"),
            platform=platform, limit=int(pending.get("ranking_limit") or 3),
            ranking_metric=metric,
        ),
    )


def _route_business_investment(text: str, state: SessionState) -> RouteResult:
    brand = detect_brand_hint(text) or state.drilldown_ctx.brand
    period = extract_period(text) or state.drilldown_ctx.period
    if not brand:
        return RouteResult(type="guide")
    platform = _explicit_business_platform(text)
    route_type = "brand_business_investment_analysis" if platform and period else (
        "clarify_business_platform" if not platform else "clarify_period"
    )
    return RouteResult(
        type=route_type,
        brand=brand,
        period=period,
        brand_aliases=[brand],
        platform=platform,
        media_scope="full_bet",
    )


def is_douyin_business_question(text: str) -> bool:
    lowered = str(text or "").lower()
    has_platform = "抖音" in lowered or bool(re.search(r"(?<![a-z])dy(?![a-z])", lowered))
    has_report_intent = any(hint in lowered for hint in (
        "生意分析", "经营分析", "生意复盘", "经营复盘", "gmv复盘",
        "品牌复盘", "生意报告", "生意月报", "品牌生意", "生意情况", "经营情况",
    ))
    return has_platform and has_report_intent and not is_media_question(text)


def is_jd_business_question(text: str) -> bool:
    lowered = str(text or "").lower()
    has_platform = "京东" in lowered or bool(re.search(r"(?<![a-z])jd(?![a-z])", lowered))
    has_report_intent = any(hint in lowered for hint in (
        "生意分析", "经营分析", "生意复盘", "经营复盘", "gmv复盘",
        "品牌复盘", "生意报告", "生意月报", "品牌生意", "自营生意",
        "生意情况", "经营情况",
        "gmv和品类", "gmv与品类", "品类表现",
    ))
    return has_platform and has_report_intent and not is_media_question(text)


def is_tmall_business_question(text: str) -> bool:
    lowered = str(text or "").lower()
    has_platform = "天猫" in lowered or bool(re.search(r"(?<![a-z])tmall(?![a-z])|(?<![a-z])tm(?![a-z])", lowered))
    has_business = any(hint in lowered for hint in (
        "生意", "经营", "gmv", "主推商品", "商品表现", "产品表现",
    ))
    return has_platform and has_business and not is_media_question(text) and not is_market_question(text)


def _strip_period_syntax(text: str) -> str:
    """Remove supported raw period spellings before extracting a brand."""
    value = str(text or "")
    patterns = (
        r"(?<!\d)20\d{6}\s*[~～—–\-至到]+\s*20\d{6}(?!\d)",
        r"(?<!\d)20\d{2}/\d{1,2}/\d{1,2}\s*[~～—–\-至到]+\s*20\d{2}/\d{1,2}/\d{1,2}(?!\d)",
        r"(?<!\d)20\d{2}-\d{1,2}-\d{1,2}\s*(?:~|～|—|–|至|到|\s-\s)\s*20\d{2}-\d{1,2}-\d{1,2}(?!\d)",
        r"(?<!\d)20\d{2}\s+\d{1,2}\s*[~～—–\-至到]+\s*\d{1,2}(?:\s*月)?(?!\d)",
        r"(?:(?:20\d{2})年)?\d{1,2}月\d{1,2}[日号]?\s*[~～—–\-至到]+\s*\d{1,2}[日号]?",
        r"(?:(?:20\d{2})年)?\d{1,2}月?\s*[~～—–\-至到]+\s*(?:(?:20\d{2})年)?\d{1,2}月",
        r"20\d{2}-\d{1,2}-\d{1,2}",
        r"(?:(?:20\d{2})年)?\d{1,2}月\d{1,2}[日号]?",
        r"(?:(?:20\d{2})年)?\d{1,2}月",
        r"(?:(?:20\d{2})年?)?[Qq][1-4]",
    )
    for pattern in patterns:
        value = re.sub(pattern, " ", value)
    return value


def _detect_douyin_business_brand(text: str) -> str | None:
    candidate = _strip_period_syntax(text)
    candidate = re.sub(r"(?i)(?<![a-z])dy(?![a-z])|抖音", " ", candidate)
    candidate = re.sub(
        r"品牌生意分析报告|品牌生意分析|生意分析报告|生意分析|经营分析|"
        r"生意复盘|经营复盘|GMV复盘|gmv复盘|品牌复盘|生意报告|生意月报|"
        r"生意情况|经营情况|生成|输出|报告|品牌|平台",
        " ",
        candidate,
    )
    candidate = re.sub(r"^\s*(帮我|请|麻烦|看一下|分析一下)\s*", "", candidate)
    candidate = re.sub(r"(?:(?:在|于|的)\s*)+$", "", candidate)
    candidate = re.sub(r"[，,。！？!?：:=\s]+", "", candidate)
    candidate = re.sub(r"(?:在|于|的)$", "", candidate)
    return candidate if 1 <= len(candidate) <= 30 else None


def _detect_jd_business_brand(text: str) -> str | None:
    candidate = _strip_period_syntax(text)
    candidate = re.split(r"[，,;；]​?\s*(?:输出|给我|并且|并)", candidate, maxsplit=1)[0]
    candidate = re.sub(r"(?i)(?<![a-z])jd(?![a-z])|京东", " ", candidate)
    candidate = re.sub(
        r"品牌生意分析报告|品牌生意分析|竞品生意分析报告|"
        r"竞品生意分析|竞品生意报告|自营生意分析|生意分析报告|"
        r"生意分析|经营分析|"
        r"生意复盘|经营复盘|GMV复盘|gmv复盘|品牌复盘|"
        r"生意报告|生意月报|品牌生意|自营生意|"
        r"生意情况|经营情况|"
        r"gmv和品类表现|GMV和品类表现|GMV和品类|gmv和品类|GMV与品类|"
        r"gmv与品类|品类表现|"
        r"京东自营旗舰店|自营旗舰店|生成|输出|报告|品牌|平台",
        " ",
        candidate,
    )
    candidate = re.sub(r"^\s*(请做|帮我|请|麻烦|看一下|分析一下|分析|做|看)\s*", "", candidate)
    candidate = re.sub(r"(?:(?:在|于|的)\s*)+$", "", candidate)
    candidate = re.sub(r"[，,。！？!?::=\s]+", "", candidate)
    return candidate if 1 <= len(candidate) <= 30 else None


def _route_douyin_business(text: str, state: SessionState) -> RouteResult:
    brand = _detect_douyin_business_brand(text) or state.drilldown_ctx.brand
    period = extract_period(text) or state.drilldown_ctx.period
    if not brand:
        return RouteResult(type="guide")
    return RouteResult(
        type="douyin_business_analysis" if period else "clarify_douyin_period",
        brand=brand,
        period=period,
        brand_aliases=[brand],
    )


def _route_jd_business(text: str, state: SessionState) -> RouteResult:
    brand = _detect_jd_business_brand(text) or state.drilldown_ctx.brand
    period = extract_period(text) or state.drilldown_ctx.period
    if not brand:
        return RouteResult(type="guide")
    return RouteResult(
        type="jd_business_analysis" if period else "clarify_jd_period",
        brand=brand,
        period=period,
        brand_aliases=[brand],
    )


def _route_tmall_business(text: str, state: SessionState) -> RouteResult:
    candidate = _strip_period_syntax(text)
    period = extract_period(candidate) or state.drilldown_ctx.period
    period = extract_period(text) or state.drilldown_ctx.period
    candidate = re.sub(r"(?i)(?<![a-z])tmall(?![a-z])|(?<![a-z])tm(?![a-z])|天猫", " ", candidate)
    candidate = re.sub(
        r"品牌生意分析报告|品牌生意分析|生意分析报告|生意分析|经营分析|"
        r"生意复盘|经营复盘|GMV复盘|gmv复盘|生意报告|生意月报|"
        r"生意情况|经营情况|主推商品|商品表现|产品表现|生成|输出|报告|品牌|平台",
        " ", candidate,
    )
    candidate = re.sub(r"^\s*(请做|帮我|请|麻烦|看一下|分析一下|分析|做|看)\s*", "", candidate)
    candidate = re.sub(r"(?:(?:在|于|的)\s*)+$", "", candidate)
    brand = re.sub(r"[，,。！？!?：:=\s]+", "", candidate) or state.drilldown_ctx.brand
    if not brand:
        return RouteResult(type="guide")
    return RouteResult(
        type="default_chain" if period else "clarify_period",
        brand=brand, period=period, brand_aliases=[brand], platform="TM",
    )


def extract_period(text: str) -> str | None:
    return normalize_period_hint(text)


def extract_media_period(text: str) -> str | None:
    return normalize_media_period_hint(text)


def _has_followup_context(state: SessionState) -> bool:
    ctx = state.drilldown_ctx
    return bool(ctx.brand and ctx.period)


def _clean_brand_candidate(brand: str | None, user_text: str) -> str | None:
    if not brand:
        return None
    candidate = str(brand).strip()
    period = normalize_period_hint(candidate)
    if period:
        candidate = candidate.replace(period, "")
    candidate = re.sub(r"\d{1,2}月\d{1,2}[日号]?[~\-至到]+\d{1,2}月\d{1,2}[日号]?", "", candidate)
    candidate = re.sub(r"\d{4}-\d{2}-\d{2}[~\-至到]+\d{4}-\d{2}-\d{2}", "", candidate)
    candidate = re.sub(r"(的)?(生意|表现|怎么样|如何|分析|情况|期间|帮我|请|麻烦|看一下|看看|分析一下)", "", candidate)
    candidate = re.sub(r"[，,。！？!?\s]+", "", candidate)
    if 1 <= len(candidate) <= 30:
        return candidate
    fallback = detect_brand_hint(user_text)
    if fallback and 1 <= len(fallback) <= 30:
        return fallback
    return None


def _intent_brand_aliases(intent: IntentResult, user_text: str, brand: str) -> list[str]:
    aliases: list[str] = []
    for value in (
        brand,
        intent.brand_cn,
        intent.brand_en,
        *(intent.brand_aliases or []),
    ):
        cleaned = _clean_brand_candidate(value, user_text)
        if cleaned and cleaned not in aliases:
            aliases.append(cleaned)
    return aliases


def _detect_media_brand(text: str) -> str | None:
    candidate = str(text or "")
    period = extract_media_period(candidate)
    if period:
        candidate = candidate.replace(period, " ")
    candidate = re.sub(r"^\s*追问\s*[:：]\s*", "", candidate)
    intent_pattern = re.compile(
        r"(?i)(social\s*search|search\s*report|kol\s*performance|bkfst?|ksi|bet|"
        r"cpe|engage|媒体投资|媒体花费|媒体费用|媒体费比|站外投放|站外投资|"
        r"搜索指数|KOL表现|KOL花费|达人投放|达人花费)"
    )
    match = intent_pattern.search(candidate)
    # “韩束4月的媒体投资是什么样的”应在媒体关键词前截出“韩束”，
    # 不能通过全句删词得到“韩束是什么样”。
    if match:
        before = candidate[:match.start()]
        after = candidate[match.end():]
        candidate = before if re.search(r"[\u4e00-\u9fa5A-Za-z0-9]", before) else after
    candidate = re.sub(
        r"^\s*(那|那么|请|麻烦|帮我|帮忙|想看|看一下|看看|分析一下|分析|看)\s*",
        "",
        candidate,
        flags=re.IGNORECASE,
    )
    candidate = re.sub(
        r"(是什么样的?|是什么情况|情况怎么样|表现怎么样|怎么样|如何|情况|表现|"
        r"看一下|看看|呢|吗|嘛|呀|啊|的|一下)+\s*$",
        "",
        candidate,
        flags=re.IGNORECASE,
    )
    candidate = re.sub(r"[，,。！？!?：:\s]+", "", candidate)
    if candidate in {"它", "该品牌", "这个品牌", "这个", "其", "该品牌的"}:
        return None
    return candidate if 1 <= len(candidate) <= 30 else None


def _route_media(text: str, state: SessionState) -> RouteResult:
    ctx = state.drilldown_ctx
    brand = _detect_media_brand(text) or ctx.brand
    period = extract_media_period(text) or ctx.period
    if not brand:
        return RouteResult(type="guide")
    return RouteResult(type="media_analysis", brand=brand, period=period)


def _route_market(text: str, state: SessionState, intent: IntentResult | None = None) -> RouteResult:
    if os.environ.get("MARKET_ANALYSIS_ENABLED", "1") != "1":
        return RouteResult(type="guide")
    if intent and intent.segment and str(intent.segment).upper() not in {"BEAUTY MARKET", "PURE MASS", "SELECTIVE", "PROFESSIONAL"}:
        return RouteResult(type="market_parameter_error", message="目前Segment仅支持Beauty Market、Pure Mass、Selective和Professional。")
    if intent and intent.platform and str(intent.platform).upper() not in {"TTL", "TM", "DY", "JD"}:
        return RouteResult(type="market_parameter_error", message="目前平台仅支持三平台TTL、天猫、抖音和京东。")
    ctx = state.market_context
    explicit_period = (intent.period if intent else None) or extract_media_period(text)
    inherits_context = bool(re.search(r"^(那|那么|其中|里面|刚才|上面)|第[一二三四五1-5]名|Top\s*5|哪些品牌|品牌.*(最好|最高|最快|排名)", text, re.IGNORECASE))
    plan = build_market_plan(
        text,
        period=explicit_period or (ctx.period if inherits_context else None),
        segment=(intent.segment if intent else None) or ctx.segment,
        platform=(intent.platform if intent else None) or ctx.platform,
        intent=intent.intent if intent else None,
        view=intent.view if intent else None,
        ranking_metric=intent.ranking_metric if intent else None,
    )
    return RouteResult(
        type=plan.intent if plan.period else "clarify_market_period",
        period=plan.period, segment=plan.segment, platform=plan.platform,
        market_view=plan.view, ranking_metric=plan.ranking_metric,
        ranking_limit=plan.ranking_limit,
    )


def _ordinal_brand_route(text: str, state: SessionState) -> RouteResult | None:
    match = re.search(r"第([一二三四五1-5])名", text)
    if not match or not state.market_context.top_brands:
        return None
    index_map = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}
    rank = index_map.get(match.group(1), int(match.group(1)) if match.group(1).isdigit() else 0)
    if not 1 <= rank <= len(state.market_context.top_brands):
        return None
    brand = state.market_context.top_brands[rank - 1]
    period = state.market_context.period
    if is_media_question(text):
        return RouteResult(type="media_analysis", brand=brand, period=period, media_scope="full_bet")
    if any(word in text for word in ("生意", "表现", "天猫", "怎么样", "如何")):
        return RouteResult(type="default_chain", brand=brand, period=period)
    return None


def classify_user_intent(user_text: str, state: SessionState) -> IntentResult | None:
    ctx = state.drilldown_ctx
    context = {
        "has_context": _has_followup_context(state),
        "brand": ctx.brand,
        "period": ctx.period,
        "category": ctx.category,
        "series": ctx.series,
        "key_driver": ctx.key_driver,
    }
    prompt = f"""
你是AI生意问答Bot的入口路由器。只判断用户意图和抽取参数，不做分析，不查数据。

意图出口严格只有六类：
1. meta：用户问你是谁、能做什么、支持哪些分析、怎么用。
2. default_analysis：用户想让你分析某品牌在某时间段的整体生意/表现/怎么样。
3. media_analysis：用户询问媒体投资、BET、BKFS、Social Search、KSI、KOL表现、Engage、CPE或媒体费比（Take Rate/TR/BET%）。
4. followup：用户基于上一轮天猫报告继续追问某品类/渠道/系列/打法/原因。
5. market_analysis：用户问大盘、市场整体或市场趋势。
6. market_brand_ranking：用户问大盘中增长最好、涨幅最高或Top品牌。

当前会话上下文：
{json.dumps(context, ensure_ascii=False)}

抽取规则：
- default_analysis和media_analysis都必须尽量抽取brand、brand_cn、brand_en、brand_aliases和period。
- media_analysis还必须抽取media_scope。
- brand保留用户提到的干净品牌名，不要包含“是什么样、怎么样、表现如何”等问句。
- brand_cn是该品牌正式中文名，brand_en是正式英文/罗马字品牌名；不确定则为null，禁止编造。
- brand_aliases列出该品牌常用的中英文写法、空格写法和官方大小写，不得包含其他品牌。
- media_scope严格取full_bet、media_investment、social_search、kol_performance之一。
- 省略品牌或时间时可从上下文继承。
- 如果用户没写时间，period 必须为 null；系统会追问，禁止补成618或其他时间。
- followup 输出 followup_text，去掉开头的“追问：”。
- market_analysis和market_brand_ranking抽取segment、platform、view、ranking_metric；不得输出表名或SQL。
- segment只允许BEAUTY MARKET、PURE MASS、SELECTIVE、PROFESSIONAL；用户说Total Beauty Market时输出BEAUTY MARKET；platform只允许TTL、TM、DY、JD。
- “涨得最好/拉动最大”ranking_metric=gmv_growth；“涨幅/增速最高”ranking_metric=evol。
- 如果用户没写“追问：”，但说“这个品类/上面/刚才/其中/李佳琦表现呢/T2打法”等，且当前上下文has_context=true，可以判为followup。
- 没有上下文时，不要把省略品牌和时间的问题判为followup。

例子：
用户：你能做什么
输出：{{"intent":"meta","brand":null,"period":null,"followup_text":null,"confidence":"high"}}
用户：谷雨618怎么样
输出：{{"intent":"default_analysis","brand":"谷雨","brand_cn":"谷雨","brand_en":null,"brand_aliases":["谷雨"],"period":"618","followup_text":null,"confidence":"high"}}
用户：谷雨5月13日到6月3日生意怎么样
输出：{{"intent":"default_analysis","brand":"谷雨","brand_cn":"谷雨","brand_en":null,"brand_aliases":["谷雨"],"period":"5月13日到6月3日","followup_text":null,"confidence":"high"}}
用户：分析2026年3月韩束的媒体投资
输出：{{"intent":"media_analysis","brand":"韩束","brand_cn":"韩束","brand_en":"KANS","brand_aliases":["韩束","KANS"],"period":"2026年3月","media_scope":"media_investment","followup_text":null,"confidence":"high"}}
用户：那它媒体投资如何
输出：{{"intent":"media_analysis","brand":null,"brand_cn":null,"brand_en":null,"brand_aliases":[],"period":null,"media_scope":"media_investment","followup_text":null,"confidence":"high"}}
用户：帮我分析一下5月13日到6月3日Olay的生意
输出：{{"intent":"default_analysis","brand":"Olay","brand_cn":"玉兰油","brand_en":"OLAY","brand_aliases":["Olay","OLAY","玉兰油"],"period":"5月13日到6月3日","followup_text":null,"confidence":"high"}}
用户：追问：乳液面霜卖得如何
输出：{{"intent":"followup","brand":null,"period":null,"followup_text":"乳液面霜卖得如何","confidence":"high"}}
用户：这个品类里李佳琦表现呢
输出：{{"intent":"followup","brand":null,"period":null,"followup_text":"这个品类里李佳琦表现呢","confidence":"high"}}
用户：2026年1-6月大盘怎么样
输出：{{"intent":"market_analysis","period":"2026年1-6月","segment":"PURE MASS","platform":"TTL","view":"summary","ranking_metric":"gmv_growth","confidence":"high"}}
用户：大盘里涨幅最高的品牌
输出：{{"intent":"market_brand_ranking","period":null,"segment":"PURE MASS","platform":"TM","view":"top_brands","ranking_metric":"evol","confidence":"high"}}

只返回JSON，不要解释。
用户问题：{user_text}
"""
    try:
        resp = llm_client().chat.completions.create(
            model=os.environ.get("DASHSCOPE_ROUTER_MODEL", "qwen3.7-plus"),
            messages=[
                {
                    "role": "system",
                    "content": "你是严格的业务问句参数解析器，只返回合法JSON对象。",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            max_tokens=260,
            timeout=float(os.environ.get("ROUTER_LLM_TIMEOUT", "12")),
            response_format={"type": "json_object"},
            extra_body={"enable_thinking": False},
        )
        parsed = extract_json_object(resp.choices[0].message.content or "")
        if not parsed:
            return None
        intent = parsed.get("intent")
        if intent not in {"meta", "default_analysis", "media_analysis", "followup", "market_analysis", "market_brand_ranking"}:
            return None
        return IntentResult(
            intent=intent,
            brand=parsed.get("brand"),
            brand_cn=parsed.get("brand_cn"),
            brand_en=parsed.get("brand_en"),
            brand_aliases=[
                str(value).strip()
                for value in (parsed.get("brand_aliases") or [])
                if str(value).strip()
            ][:12],
            period=parsed.get("period"),
            media_scope=parsed.get("media_scope"),
            followup_text=parsed.get("followup_text"),
            segment=parsed.get("segment"),
            platform=parsed.get("platform"),
            view=parsed.get("view"),
            ranking_metric=parsed.get("ranking_metric"),
            confidence=parsed.get("confidence") or "low",
        )
    except Exception as exc:
        log.warning("[router] llm intent failed, falling back to rules: %s", exc)
        return None


def _short_category(name: str) -> str:
    return name.split("-")[-1] if "-" in name else name


def _normalize_text(value: str) -> str:
    return re.sub(r"[\s/／\\\-_\(\)（）]+", "", value or "").lower()


def _resolve_category_from_state(text: str, state: SessionState) -> str | None:
    cache = state.last_result_cache or {}
    category_result = cache.get("category_result") or {}
    categories = category_result.get("categories") or []
    if not categories:
        return None
    norm_text = _normalize_text(text)
    best = None
    best_score = 0
    for row in categories:
        full = row.get("category_cn") or ""
        short = _short_category(full)
        candidates = {full, short}
        if "/" in short or "／" in short:
            candidates.add(short.replace("/", ""))
            candidates.add(short.replace("／", ""))
        for candidate in candidates:
            norm_candidate = _normalize_text(candidate)
            if not norm_candidate:
                continue
            score = 0
            if norm_candidate in norm_text:
                score = 100 + len(norm_candidate)
            else:
                # Let "乳液面霜" match "乳液/面霜".
                pieces = [p for p in re.split(r"[/／\\\-_\s]+", candidate) if p]
                if len(pieces) >= 2 and all(_normalize_text(p) in norm_text for p in pieces):
                    score = 80 + sum(len(_normalize_text(p)) for p in pieces)
                elif any(len(_normalize_text(p)) >= 2 and _normalize_text(p) in norm_text for p in pieces):
                    # Let "面霜品类" match "乳液/面霜", while avoiding one-char noise.
                    score = 55 + max((len(_normalize_text(p)) for p in pieces if _normalize_text(p) in norm_text), default=0)
            if score > best_score:
                best = full
                best_score = score
    return best


def try_filter_update(text: str, state: SessionState | None = None) -> FilterUpdate:
    update = FilterUpdate()
    lowered = text.lower()
    for view, hints in VIEW_HINTS.items():
        if any(h.lower() in lowered for h in hints):
            update.matched = True
            update.view = view
            break
    period = extract_period(text)
    if period:
        update.matched = True
        update.period = period
    if state:
        category = _resolve_category_from_state(text, state)
        if category:
            update.matched = True
            update.category = category
    for k, v in DRIVER_ALIASES.items():
        if k in text:
            update.matched = True
            update.key_driver = v
            break
    for tag in FUNCTION_HINTS:
        if tag in text:
            update.matched = True
            update.function_tag = tag
            break
    m = re.search(r"([\u4e00-\u9fa5A-Za-z0-9]{1,12})(?:系列|线)", text)
    if m:
        update.matched = True
        update.series = m.group(1)
    return update


def _route_by_rules(user_text: str, state: SessionState) -> RouteResult:
    text = user_text.strip()
    if is_meta_question(text):
        return RouteResult(type="meta")
    ordinal = _ordinal_brand_route(text, state)
    if ordinal:
        return ordinal
    if is_business_investment_question(text):
        return _route_business_investment(text, state)
    if is_jd_business_question(text):
        return _route_jd_business(text, state)
    if is_douyin_business_question(text):
        return _route_douyin_business(text, state)
    if is_tmall_business_question(text):
        return _route_tmall_business(text, state)
    if is_market_question(text):
        return _route_market(text, state)
    if text.startswith("追问：") or text.startswith("追问:"):
        followup = re.sub(r"^\s*追问\s*[:：]\s*", "", text, count=1).strip()
        if is_data_caliber_question(followup):
            return RouteResult(type="caliber_reject")
        media = is_media_question(followup)
        ctx = state.bet_context if media else state.ec_context
        return RouteResult(
            type="skill_dispatch",
            brand=ctx.brand or state.drilldown_ctx.brand,
            period=ctx.period or state.drilldown_ctx.period,
            brand_aliases=list(ctx.brand_aliases or state.drilldown_ctx.brand_aliases),
            followup_text=followup,
        )
    if os.environ.get("FOLLOWUP_SKILL_V2_ENABLED", "1") == "1" and is_narrow_followup(text):
        media = is_media_question(text)
        ctx = state.bet_context if media else state.ec_context
        brand = (_detect_media_brand(text) if media else detect_brand_hint(text)) or ctx.brand or state.drilldown_ctx.brand
        period = (extract_media_period(text) if media else extract_period(text)) or ctx.period or state.drilldown_ctx.period
        return RouteResult(type="skill_dispatch", brand=brand, period=period, followup_text=text)
    if is_media_question(text):
        return _route_media(text, state)
    brand = detect_brand_hint(text)
    if brand:
        period = extract_period(text)
        return RouteResult(type="default_chain" if period else "clarify_period", brand=brand, period=period)
    return RouteResult(type="guide")


def route(user_text: str, state: SessionState) -> RouteResult:
    text = user_text.strip()
    # BET仍是独立报告入口，但品牌、时间和中英文名优先由统一语义路由抽取。
    is_explicit_media = is_media_question(text)
    pending_period = extract_period(text)
    if state.pending_request and state.pending_request.get("intent") == "analysis_preflight":
        resumed = _resume_analysis_preflight(text, state.pending_request)
        if resumed:
            return resumed
    if state.pending_request and state.pending_request.get("intent") == "brand_business_investment_analysis":
        pending = state.pending_request
        selected_platform = _explicit_business_platform(text) or pending.get("platform")
        selected_period = pending_period or pending.get("period")
        if selected_platform and selected_period:
            return RouteResult(
                type="brand_business_investment_analysis",
                brand=pending.get("brand"),
                period=selected_period,
                brand_aliases=list(pending.get("brand_aliases") or []),
                platform=selected_platform,
                media_scope="full_bet",
            )
    if state.pending_request and state.pending_request.get("intent") == "jd_business_analysis" and pending_period:
        return RouteResult(
            type="jd_business_analysis",
            brand=state.pending_request.get("brand"),
            period=pending_period,
            brand_aliases=list(state.pending_request.get("brand_aliases") or []),
        )
    if state.pending_request and state.pending_request.get("intent") == "douyin_business_analysis" and pending_period:
        return RouteResult(
            type="douyin_business_analysis",
            brand=state.pending_request.get("brand"),
            period=pending_period,
            brand_aliases=list(state.pending_request.get("brand_aliases") or []),
        )
    if state.pending_request and state.pending_request.get("intent") in {"market_analysis", "market_brand_ranking", "market_brand_deep_dive"} and pending_period:
        pending = state.pending_request
        return RouteResult(
            type=pending["intent"], period=pending_period,
            segment=pending.get("segment") or "PURE MASS", platform=pending.get("platform") or "TTL",
            market_view=pending.get("market_view") or "summary",
            ranking_metric=pending.get("ranking_metric") or "gmv_actual",
        )
    if state.pending_request and state.pending_request.get("intent") == "followup_v2":
        awaiting = state.pending_request.get("awaiting")
        pending_brand = state.pending_request.get("brand")
        pending_followup = state.pending_request.get("followup_text") or ""
        if awaiting == "period" and pending_period:
            return RouteResult(
                type="skill_dispatch", brand=pending_brand, period=pending_period,
                brand_aliases=list(state.pending_request.get("brand_aliases") or []),
                followup_text=pending_followup,
            )
        if awaiting == "brand":
            supplied_brand = detect_brand_hint(text)
            pending_time = state.pending_request.get("period") or pending_period
            if supplied_brand:
                return RouteResult(
                    type="skill_dispatch", brand=supplied_brand, period=pending_time,
                    followup_text=pending_followup,
                )
    if state.pending_request and pending_period and not is_explicit_media:
        pending_brand = str(state.pending_request.get("brand") or "").strip()
        if pending_brand:
            return RouteResult(
                type="default_chain",
                brand=pending_brand,
                period=pending_period,
                brand_aliases=list(state.pending_request.get("brand_aliases") or []),
            )
    if text.startswith("追问：") or text.startswith("追问:"):
        followup = re.sub(r"^\s*追问\s*[:：]\s*", "", text, count=1).strip()
        ordinal = _ordinal_brand_route(followup, state)
        if ordinal:
            return ordinal
        if is_market_question(followup):
            return _route_market(followup, state)
        if is_data_caliber_question(followup):
            return RouteResult(type="caliber_reject")
        media = is_media_question(followup)
        ctx = state.bet_context if media else state.ec_context
        if os.environ.get("FOLLOWUP_SKILL_V2_ENABLED", "1") != "1":
            update = try_filter_update(followup, state)
            if update.matched:
                return RouteResult(type="filter_update", followup_text=followup, update=update)
        return RouteResult(
            type="skill_dispatch",
            brand=ctx.brand or state.drilldown_ctx.brand,
            period=ctx.period or state.drilldown_ctx.period,
            brand_aliases=list(ctx.brand_aliases or state.drilldown_ctx.brand_aliases),
            followup_text=followup,
        )

    if is_jd_business_question(text):
        return _route_jd_business(text, state)

    if is_douyin_business_question(text):
        return _route_douyin_business(text, state)

    if is_tmall_business_question(text):
        return _route_tmall_business(text, state)

    preflight = _sophisticated_preflight(text, state)
    if preflight:
        return preflight

    if is_business_investment_question(text):
        return _route_business_investment(text, state)

    # 明确的大盘问句走确定性快速路径，避免为已能完整解析的日期、Segment和平台
    # 等待路由模型；含糊的非大盘问句仍由统一语义路由处理。
    if is_market_question(text):
        return _route_market(text, state)

    intent = classify_user_intent(text, state)
    if not intent or intent.confidence != "high":
        return _route_by_rules(user_text, state)

    log.info(
        "[router] llm intent=%s brand=%s brand_cn=%s brand_en=%s aliases=%s "
        "period=%s scope=%s confidence=%s",
        intent.intent,
        intent.brand,
        intent.brand_cn,
        intent.brand_en,
        intent.brand_aliases,
        intent.period,
        intent.media_scope,
        intent.confidence,
    )

    if intent.intent == "meta":
        return RouteResult(type="meta")

    ordinal = _ordinal_brand_route(text, state)
    if ordinal:
        return ordinal
    if intent.intent in {"market_analysis", "market_brand_ranking"} or is_market_question(text):
        return _route_market(text, state, intent)

    if os.environ.get("FOLLOWUP_SKILL_V2_ENABLED", "1") == "1" and is_narrow_followup(text):
        media = is_explicit_media
        ctx = state.bet_context if media else state.ec_context
        brand = _clean_brand_candidate(intent.brand, text) or ctx.brand or state.drilldown_ctx.brand
        period = intent.period or (extract_media_period(text) if media else extract_period(text)) or ctx.period or state.drilldown_ctx.period
        aliases = _intent_brand_aliases(intent, text, brand) if brand else []
        return RouteResult(
            type="skill_dispatch", brand=brand, period=period,
            brand_aliases=aliases, followup_text=intent.followup_text or text,
        )

    # 明确包含BET关键词时，确定性路由优先于LLM的意图标签。
    # LLM仍负责抽取中英文品牌、别名和时间，但不能把KOL/BET问题降级为天猫追问。
    force_media = is_explicit_media and not is_meta_question(text)
    if intent.intent == "media_analysis" or force_media:
        brand = _clean_brand_candidate(intent.brand, text) or state.drilldown_ctx.brand
        period = intent.period or extract_media_period(text) or state.drilldown_ctx.period
        if not brand:
            return RouteResult(type="guide")
        aliases = _intent_brand_aliases(intent, text, brand)
        return RouteResult(
            type="media_analysis",
            brand=brand,
            period=period,
            brand_aliases=aliases,
            media_scope=intent.media_scope or "full_bet",
        )

    if intent.intent == "followup":
        if not _has_followup_context(state):
            return RouteResult(type="guide")
        followup = (intent.followup_text or text).strip()
        if is_data_caliber_question(followup):
            return RouteResult(type="caliber_reject")
        if os.environ.get("FOLLOWUP_SKILL_V2_ENABLED", "1") != "1":
            update = try_filter_update(followup, state)
            if update.matched:
                return RouteResult(type="filter_update", followup_text=followup, update=update)
        return RouteResult(type="skill_dispatch", followup_text=followup)

    if intent.intent == "default_analysis":
        brand = _clean_brand_candidate(intent.brand, text)
        if not brand:
            return RouteResult(type="guide")
        period = intent.period or extract_period(text)
        return RouteResult(
            type="default_chain" if period else "clarify_period",
            brand=brand,
            period=period,
            brand_aliases=_intent_brand_aliases(intent, text, brand),
        )

    return _route_by_rules(user_text, state)
