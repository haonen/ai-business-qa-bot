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
    if intent and intent.segment and str(intent.segment).upper() not in {"PURE MASS", "SELECTIVE", "PROFESSIONAL"}:
        return RouteResult(type="market_parameter_error", message="目前Segment仅支持Pure Mass、Selective和Professional。")
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
- segment只允许PURE MASS、SELECTIVE、PROFESSIONAL；platform只允许TTL、TM、DY、JD。
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
    if state.pending_request and state.pending_request.get("intent") in {"market_analysis", "market_brand_ranking"} and pending_period:
        pending = state.pending_request
        return RouteResult(
            type=pending["intent"], period=pending_period,
            segment=pending.get("segment") or "PURE MASS", platform=pending.get("platform") or "TTL",
            market_view=pending.get("market_view") or "summary",
            ranking_metric=pending.get("ranking_metric") or "gmv_growth",
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
