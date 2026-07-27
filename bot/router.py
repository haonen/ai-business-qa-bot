from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import logging
import os
import re

from bot.session import SessionState
from bot.utils import detect_brand_hint, extract_json_object, llm_client, normalize_period_hint


log = logging.getLogger(__name__)


META_HINTS = ["你还会", "你能做什么", "有什么功能", "能分析什么", "怎么用", "你是谁", "支持哪些", "有什么限制"]
CALIBER_HINTS = ["刷单", "口径", "对不上", "数据准", "为什么不一样", "情报通和"]
DRIVER_ALIASES = {"李佳琦": "李佳琦", "佳琦": "李佳琦", "T2": "T2", "达人": "T2", "自播": "Non-KOL", "店播": "Non-KOL", "Non-KOL": "Non-KOL"}
LINK_TYPE_ALIASES = {"平台活动": "平台活动链接", "预售": "预售链接", "付定": "预售链接", "尾款": "预售链接", "自播": "自播链接", "直播间": "自播链接"}
FUNCTION_HINTS = ["美白", "抗老", "保湿", "修护", "控油祛痘", "礼赠", "防晒"]
VIEW_HINTS = {
    "playbook": ["打法", "推什么", "人群", "场景"],
    "driver": ["key driver", "keydriver", "渠道", "渠道贡献", "渠道拆分", "驱动", "李佳琦", "T2", "达人", "自播", "店播", "Non-KOL", "non-kol"],
    "sku": ["sku", "SKU", "链接", "top链接", "Top链接", "单品", "产品"],
    "category": ["品类", "类目"],
}


@dataclass
class FilterUpdate:
    matched: bool = False
    view: str | None = None
    period: str | None = None
    category: str | None = None
    kol_driver: str | None = None
    link_type: str | None = None
    function_tag: str | None = None
    series: str | None = None


@dataclass
class RouteResult:
    type: str
    brand: str | None = None
    period: str | None = None
    followup_text: str | None = None
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
    period: str | None = None
    followup_text: str | None = None
    confidence: str = "low"


def is_meta_question(text: str) -> bool:
    return any(h in text for h in META_HINTS)


def is_data_caliber_question(text: str) -> bool:
    return any(h in text for h in CALIBER_HINTS)


def extract_period(text: str) -> str | None:
    return normalize_period_hint(text)


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


def classify_user_intent(user_text: str, state: SessionState) -> IntentResult | None:
    ctx = state.drilldown_ctx
    context = {
        "has_context": _has_followup_context(state),
        "brand": ctx.brand,
        "period": ctx.period,
        "category": ctx.category,
        "series": ctx.series,
        "kol_driver": ctx.kol_driver,
    }
    prompt = f"""
你是AI生意问答Bot的入口路由器。只判断用户意图和抽取参数，不做分析，不查数据。

意图出口严格只有三类：
1. meta：用户问你是谁、能做什么、支持哪些分析、怎么用。
2. default_analysis：用户想让你分析某品牌在某时间段的整体生意/表现/怎么样。
3. followup：用户基于上一轮报告继续追问某品类/渠道/系列/打法/原因。若用户明确以“追问：”开头，必须是followup。

当前会话上下文：
{json.dumps(context, ensure_ascii=False)}

抽取规则：
- default_analysis 必须尽量抽取 brand 和 period。
- 如果用户没写时间，period 可以为 null，系统会默认618。
- followup 输出 followup_text，去掉开头的“追问：”。
- 如果用户没写“追问：”，但说“这个品类/上面/刚才/其中/李佳琦表现呢/T2打法”等，且当前上下文has_context=true，可以判为followup。
- 没有上下文时，不要把省略品牌和时间的问题判为followup。

例子：
用户：你能做什么
输出：{{"intent":"meta","brand":null,"period":null,"followup_text":null,"confidence":"high"}}
用户：谷雨618怎么样
输出：{{"intent":"default_analysis","brand":"谷雨","period":"618","followup_text":null,"confidence":"high"}}
用户：谷雨5月13日到6月3日生意怎么样
输出：{{"intent":"default_analysis","brand":"谷雨","period":"5月13日到6月3日","followup_text":null,"confidence":"high"}}
用户：帮我分析一下5月13日到6月3日Olay的生意
输出：{{"intent":"default_analysis","brand":"Olay","period":"5月13日到6月3日","followup_text":null,"confidence":"high"}}
用户：追问：乳液面霜卖得如何
输出：{{"intent":"followup","brand":null,"period":null,"followup_text":"乳液面霜卖得如何","confidence":"high"}}
用户：这个品类里李佳琦表现呢
输出：{{"intent":"followup","brand":null,"period":null,"followup_text":"这个品类里李佳琦表现呢","confidence":"high"}}

只返回JSON，不要解释。
用户问题：{user_text}
"""
    try:
        resp = llm_client().chat.completions.create(
            model=os.environ.get("DASHSCOPE_ROUTER_MODEL", "qwen-turbo"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=180,
        )
        parsed = extract_json_object(resp.choices[0].message.content or "")
        if not parsed:
            return None
        intent = parsed.get("intent")
        if intent not in {"meta", "default_analysis", "followup"}:
            return None
        return IntentResult(
            intent=intent,
            brand=parsed.get("brand"),
            period=parsed.get("period"),
            followup_text=parsed.get("followup_text"),
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
            update.kol_driver = v
            break
    for k, v in LINK_TYPE_ALIASES.items():
        if k in text:
            update.matched = True
            update.link_type = v
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
    if text.startswith("追问：") or text.startswith("追问:"):
        followup = re.sub(r"^\s*追问\s*[:：]\s*", "", text, count=1).strip()
        if is_data_caliber_question(followup):
            return RouteResult(type="caliber_reject")
        update = try_filter_update(followup, state)
        if update.matched:
            return RouteResult(type="filter_update", followup_text=followup, update=update)
        return RouteResult(type="skill_dispatch", followup_text=followup)
    brand = detect_brand_hint(text)
    if brand:
        return RouteResult(type="default_chain", brand=brand, period=extract_period(text) or "618")
    return RouteResult(type="guide")


def route(user_text: str, state: SessionState) -> RouteResult:
    text = user_text.strip()
    if text.startswith("追问：") or text.startswith("追问:"):
        followup = re.sub(r"^\s*追问\s*[:：]\s*", "", text, count=1).strip()
        if is_data_caliber_question(followup):
            return RouteResult(type="caliber_reject")
        update = try_filter_update(followup, state)
        if update.matched:
            return RouteResult(type="filter_update", followup_text=followup, update=update)
        return RouteResult(type="skill_dispatch", followup_text=followup)

    intent = classify_user_intent(text, state)
    if not intent or intent.confidence != "high":
        return _route_by_rules(user_text, state)

    log.info(
        "[router] llm intent=%s brand=%s period=%s confidence=%s",
        intent.intent,
        intent.brand,
        intent.period,
        intent.confidence,
    )

    if intent.intent == "meta":
        return RouteResult(type="meta")

    if intent.intent == "followup":
        if not _has_followup_context(state):
            return RouteResult(type="guide")
        followup = (intent.followup_text or text).strip()
        if is_data_caliber_question(followup):
            return RouteResult(type="caliber_reject")
        update = try_filter_update(followup, state)
        if update.matched:
            return RouteResult(type="filter_update", followup_text=followup, update=update)
        return RouteResult(type="skill_dispatch", followup_text=followup)

    if intent.intent == "default_analysis":
        brand = _clean_brand_candidate(intent.brand, text)
        if not brand:
            return RouteResult(type="guide")
        return RouteResult(type="default_chain", brand=brand, period=intent.period or extract_period(text) or "618")

    return _route_by_rules(user_text, state)
