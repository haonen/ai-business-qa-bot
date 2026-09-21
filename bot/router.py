from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
import json
import logging
import os
import re

from bot.session import SessionState
from bot.media_period import normalize_media_period_hint
from bot.utils import detect_brand_hint, extract_json_object, llm_client, llm_model, normalize_period_hint
from bot.followup_plan import is_narrow_followup
from bot.opportunity_insight import asks_for_opportunity
from bot.market_plan import (
    build_market_plan, explicit_category, explicit_platform, explicit_segment, is_market_question,
)
from bot.platforms import canonical_platform
from bot.router_v2 import build_route_decision, extract_brand_surface
from bot.routing_contracts import (
    CommerceScope, MarketScope, MediaScope, RouteDecision,
    infer_response_strategy, validate_route_decision,
)
from bot.entity_resolution import EntityResolution, ResolvedPeriod, resolve_entities, resolve_time_scope
from bot.key_driver_ontology import (
    detect_key_drivers, has_explicit_media_intent, inferred_key_driver_platform,
)
from bot.task_context import resolve_task_turn
from bot.brand_reference import english_brand_for_chinese


log = logging.getLogger(__name__)


META_HINTS = [
    "你还会", "你会什么", "你会干什么", "你能干什么", "你能做什么",
    "你可以做什么", "有什么功能", "能分析什么", "怎么用", "你是谁",
    "支持哪些", "有什么限制", "接下来还能", "还能看什么",
]
CALIBER_HINTS = ["刷单", "口径", "对不上", "数据准", "为什么不一样", "情报通和"]
# Shared by is_tmall/douyin/jd/unspecified_platform_business_question: with an
# explicit platform word already required alongside these, a plain "怎么样"
# or "是多少" is unambiguously a business ask in this bot's domain. Before
# these were added, a platform-qualified question with none of the narrower
# "生意/表现" words (e.g. "谷雨天猫618怎么样", "珀莱雅天猫卖了多少钱") failed every
# platform-specific gate and fell through to the generic catch-all route,
# which does not strip the platform word out of the extracted brand.
BUSINESS_HINT_WORDS = (
    "生意", "经营", "gmv", "主推商品", "商品表现", "产品表现", "品类表现", "表现",
    "怎么样", "咋样", "如何", "情况", "好不好",
    "是多少", "多少钱", "卖了多少", "花了多少", "赚了多少", "销售额", "销量",
)
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
    "费用增长", "费用同比", "费用增加", "费用上涨", "费用下降", "费用减少",
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
    # Preserves the complete task wording across an explicit preflight
    # confirmation (for example when the next message is only "确认").
    original_text: str | None = None
    brand: str | None = None
    period: str | None = None
    brand_aliases: list[str] | None = None
    media_scope: str | None = None
    followup_text: str | None = None
    segment: str | None = None
    category: str | None = None
    platform: str | None = None
    media_mode: str | None = None
    media_channels: list[str] | None = None
    question_mode: str | None = None
    market_view: str | None = None
    ranking_metric: str | None = None
    ranking_limit: int | None = None
    preflight_target: str | None = None
    message: str | None = None
    route_decision: dict | None = None
    time_scope: dict | None = None
    brand_resolution: dict | None = None
    task_bindings: list[dict] | None = None
    business_period: ResolvedPeriod | None = None
    bet_period: ResolvedPeriod | None = None
    include_bet: bool = False
    bet_latest_ytd: bool = False
    update: FilterUpdate | None = None
    response_strategy: str | None = None

    def __post_init__(self) -> None:
        if not self.response_strategy:
            self.response_strategy = infer_response_strategy(
                action=self.type,
                question_mode=self.question_mode,
                reason_codes=(self.route_decision or {}).get("reason_codes") or [],
                user_text=self.original_text or self.followup_text or "",
            )

    def to_dict(self) -> dict:
        data = asdict(self)
        if self.update:
            data["update"] = asdict(self.update)
        return data


def _adapt_v2_decision(decision: RouteDecision) -> RouteResult:
    decision = validate_route_decision(decision)
    platform = (decision.commerce_scope.platforms or [None])[0]
    action = decision.action
    if action == "market_analysis" and decision.ranking_metric:
        action = "market_brand_ranking"
    media_scope = None
    if decision.media_scope.mode == "OVERALL_BET":
        media_scope = "full_bet"
    elif decision.media_scope.mode == "CHANNEL_ONLY":
        media_scope = "channel_only:" + ",".join(channel.lower() for channel in decision.media_scope.channels)
    message = None
    if action == "clarify_media_scope":
        message = (
            "你提到的“抖音BET/投放”是想只看抖音相关的BET，还是看整体BET？"
            "请一次回复：只看抖音，或整体BET。"
        )
    elif action == "confirm_current_year":
        scope = (decision.entity_resolution or {}).get("time_scope") or {}
        mention = next(iter(scope.get("mentions") or []), {})
        proposed = mention.get("canonical") or f"{date.today().year}年"
        message = f"你没有写年份。是否按今年理解，也就是{proposed}？请回复“是”或补充正确年份。"
    elif action == "provide_campaign_window":
        scope = (decision.entity_resolution or {}).get("time_scope") or {}
        campaign = scope.get("campaign_name") or "这次大促"
        message = (
            f"请确认{campaign}的分析窗口，并一次回复年份、开始日期和结束日期，"
            "例如：2026年5月13日至6月7日。"
        )
    elif action == "confirm_brand_candidate":
        brand_data = (decision.entity_resolution or {}).get("brand") or {}
        candidates = brand_data.get("candidates") or []
        choices = "；".join(
            f"{index}. {item.get('canonical_display_name')}"
            for index, item in enumerate(candidates[:3], 1)
        )
        if brand_data.get("match_method") == "multiple_exact_brands":
            message = f"我识别到多个不同品牌，但本轮只支持单品牌分析。请选择主品牌：{choices}。"
        else:
            message = f"品牌“{brand_data.get('surface') or ''}”存在多个可能匹配，请回复序号或完整品牌名：{choices}。"
    elif action == "clarify_time_roles":
        message = "你写了多个时间，但主分析期和对比期不明确。请明确回复：分析哪个时间，对比哪个时间。"
    elif action == "clarify_market_scope":
        labels = {
            "period": "时间", "commerce_scope.platforms": "平台（天猫/抖音/京东/三平台）",
            "market_scope.segment": "Segment（Beauty Market/Pure Mass/Selective/Professional）",
            "market_scope.category": "Category（TTL Beauty/女士护肤/彩妆/Hair/男士护肤）",
        }
        missing_text = "、".join(labels[item] for item in decision.missing_slots if item in labels)
        if "COMPLEX_MARKET_PLAN" in decision.reason_codes:
            bet_step = " 最后对入选品牌逐一查询今年截至最新可用月的BET。" if "BET" in decision.intents else ""
            message = (
                "我已保留完整任务：先选Top品牌，再逐品牌拆三平台、"
                "识别增长最多的平台并继续下钻。"
                f"{bet_step}\n\n开始前还需要你补充：{missing_text}。"
            )
        else:
            message = f"请一次补充大盘口径：{missing_text}。"
    elif action == "clarify_analysis_scope":
        if "MARKET" in decision.intents:
            message = _preflight_message(
                target="market_brand_deep_dive", brand=None,
                period=decision.period, limit=decision.ranking_limit,
                ranking_metric=decision.ranking_metric,
                include_bet="BET" in decision.intents,
            )
        else:
            message = _preflight_message(
                target="brand_business_investment_analysis",
                brand=decision.brand_surface, period=decision.period,
                platform=platform,
            )
    entity = decision.entity_resolution or {}
    brand_resolution = entity.get("brand") or {}
    aliases = list(brand_resolution.get("aliases") or [])
    if decision.brand_surface and decision.brand_surface not in aliases:
        aliases.insert(0, decision.brand_surface)
    task_periods = {
        binding.get("intent"): _period_from_task_binding(binding)
        for binding in decision.task_bindings
        if binding.get("intent")
    }
    decision_payload = decision.to_dict()
    awaiting_slot = {
        "clarify_v2_brand": "brand", "clarify_v2_period": "period",
        "clarify_ec_platform": "platform", "clarify_media_scope": "media_scope",
        "clarify_market_scope": "market_scope", "clarify_analysis_scope": "confirmation",
        "provide_campaign_window": "campaign_window",
        "confirm_brand_candidate": "brand_candidate",
    }.get(action)
    task_relation = (
        "SUPPLY_MISSING_SLOT"
        if decision.decision_source == "context_merge"
        or any("PENDING" in code for code in decision.reason_codes)
        else "NEW_TASK"
    )
    raw_slots = {
        "original_question": entity.get("text"), "brand": decision.brand_surface,
        "brand_aliases": aliases or ([decision.brand_surface] if decision.brand_surface else []),
        "period": decision.period, "platform": platform,
        "dimensions": list(decision.dimensions), "media_mode": decision.media_scope.mode,
        "media_channels": list(decision.media_scope.channels),
        "segment": decision.market_scope.segment, "category": decision.market_scope.category,
        "comparison_metric": decision.ranking_metric,
    }
    slot_ops = {
        key: {"op": "SET", "value": value}
        for key, value in raw_slots.items()
        if value is not None and value != []
    }
    slot_ops["awaiting_slot"] = {
        "op": "SET" if awaiting_slot else "CLEAR", "value": awaiting_slot,
    }
    slot_ops["status"] = {"op": "SET", "value": "awaiting" if awaiting_slot else "active"}
    decision_payload["task_context_patch"] = {
        "relation": task_relation,
        "slot_ops": slot_ops,
        "intents": list(decision.intents),
        "goals": list(dict.fromkeys([*decision.intents, *(
            ["COMPARE_PLATFORMS", "DRILL_SELECTED_PLATFORM"]
            if "RANK_THEN_DRILL" in decision.reason_codes else []
        )])),
        "reason_codes": ["ROUTE_ESTABLISHED_TASK", *decision.reason_codes],
    }
    return RouteResult(
        type=action, original_text=entity.get("text"),
        brand=decision.brand_surface, period=decision.period,
        brand_aliases=aliases or ([decision.brand_surface] if decision.brand_surface else []),
        media_scope=media_scope, media_mode=decision.media_scope.mode,
        media_channels=list(decision.media_scope.channels), question_mode=decision.question_mode,
        segment=decision.market_scope.segment, category=decision.market_scope.category,
        platform=platform, ranking_metric=decision.ranking_metric,
        ranking_limit=decision.ranking_limit,
        market_view="top_brands" if decision.ranking_metric else "summary",
        preflight_target=(
            "brand_business_investment_analysis"
            if decision.intents == ["EC_BUSINESS", "BET"]
            else "market_brand_deep_dive"
            if "MARKET" in decision.intents and decision.action == "clarify_analysis_scope"
            else None
        ),
        message=message, route_decision=decision_payload,
        time_scope=entity.get("time_scope"), brand_resolution=brand_resolution or None,
        task_bindings=list(decision.task_bindings),
        business_period=task_periods.get("EC_BUSINESS"),
        bet_period=task_periods.get("BET"),
        include_bet="MARKET" in decision.intents and "BET" in decision.intents,
        bet_latest_ytd=(
            "MARKET" in decision.intents and "BET" in decision.intents
            and "BET_LATEST_YTD_REQUESTED" in decision.reason_codes
        ),
        response_strategy=decision.response_strategy,
    )


def _period_from_task_binding(binding: dict) -> ResolvedPeriod | None:
    period_range = binding.get("range") or {}
    if not period_range.get("start_date") or not period_range.get("end_date"):
        return None
    return ResolvedPeriod(
        binding.get("period") or f"{period_range['start_date']}~{period_range['end_date']}",
        start_date=period_range["start_date"], end_date=period_range["end_date"],
        comparison_start=period_range.get("comparison_start"),
        comparison_end=period_range.get("comparison_end"),
    )


def _bind_entities_to_tasks(decision: RouteDecision, entity: EntityResolution, text: str) -> None:
    mentions = entity.time_scope.mentions
    if not mentions:
        return
    patterns = {
        "EC_BUSINESS": r"生意|经营|gmv|商品|产品",
        "BET": r"bet|媒体投资|媒体花费|媒体费用|投放",
        "MARKET": r"大盘|市场|top|排名",
    }
    task_intents = list(dict.fromkeys(decision.intents))
    anchors = {}
    for intent in task_intents:
        matches = list(re.finditer(patterns.get(intent, r"$^"), text, re.I))
        anchors[intent] = [((match.start() + match.end()) / 2) for match in matches]

    def distance(intent: str, mention_index: int) -> float:
        center = (mentions[mention_index].start_offset + mentions[mention_index].end_offset) / 2
        return min((abs(center - anchor) for anchor in anchors.get(intent) or []), default=float("inf"))

    assignments: dict[str, int] = {}
    exact_indices = [
        index for index, mention in enumerate(mentions)
        if mention.resolution_status == "exact" and mention.start_date and mention.end_date
    ]
    has_comparison = bool(re.search(r"同比|对比|比较|\bvs\b|去年同期", text, re.I))
    if len(task_intents) > 1 and len(exact_indices) >= len(task_intents) and not has_comparison:
        available = set(exact_indices)
        for intent in task_intents:
            selected = min(available, key=lambda index: distance(intent, index))
            assignments[intent] = selected
            available.remove(selected)
        # Multiple periods are not ambiguous when each clause has its own
        # explicit period and the clause anchors uniquely bind them.
        entity.time_scope.missing_slots = [
            slot for slot in entity.time_scope.missing_slots
            if slot != "time_scope.period_roles"
        ]
        if not entity.time_scope.missing_slots:
            entity.time_scope.status = "exact"
    elif exact_indices:
        focus_index = next(
            (index for index in exact_indices if mentions[index].role == "FOCUS"),
            exact_indices[0],
        )
        assignments = {intent: focus_index for intent in task_intents}

    bindings = []
    brand_ref = entity.brand.canonical_brand_key or entity.brand.surface
    platform = (decision.commerce_scope.platforms or [None])[0]
    for intent in task_intents:
        index = assignments.get(intent)
        if index is None:
            continue
        mention = mentions[index]
        start = date.fromisoformat(mention.start_date)
        end = date.fromisoformat(mention.end_date)
        try:
            comparison_start = start.replace(year=start.year - 1)
        except ValueError:
            comparison_start = start.replace(year=start.year - 1, day=28)
        try:
            comparison_end = end.replace(year=end.year - 1)
        except ValueError:
            comparison_end = end.replace(year=end.year - 1, day=28)
        bindings.append({
            "task_id": intent.casefold(), "intent": intent,
            "brand_ref": brand_ref if intent != "MARKET" else None,
            "time_ref": index, "period": mention.canonical or mention.raw_text,
            "range": {
                "start_date": mention.start_date, "end_date": mention.end_date,
                "comparison_start": comparison_start.isoformat(),
                "comparison_end": comparison_end.isoformat(),
            },
            "platform": platform if intent in {"EC_BUSINESS", "MARKET"} else None,
            "media_mode": decision.media_scope.mode if intent == "BET" else None,
            "media_channels": list(decision.media_scope.channels) if intent == "BET" else [],
        })
    decision.task_bindings = bindings


def _decision_from_entities(text: str, entity: EntityResolution) -> RouteDecision | None:
    period = entity.time_scope.focus_period
    brand_surface = entity.brand.surface if entity.brand.status == "resolved" else entity.brand.surface
    decision = build_route_decision(
        text,
        period,
        brand_surface=brand_surface,
        time_detected=bool(entity.time_scope.mentions),
    )
    if not decision:
        return None
    return _apply_entity_resolution(decision, entity)


def _apply_entity_resolution(decision: RouteDecision, entity: EntityResolution) -> RouteDecision:
    _bind_entities_to_tasks(decision, entity, entity.text)
    decision.entity_resolution = entity.to_dict()
    is_market = "MARKET" in decision.intents
    if not is_market and entity.brand.status == "resolved":
        decision.brand_surface = entity.brand.surface
        decision.missing_slots = [slot for slot in decision.missing_slots if slot != "brand_surface"]
    if entity.time_scope.focus_period:
        resolved = entity.time_scope.focus_period
        # Keep the legacy/user-facing period spelling for compatibility while
        # carrying V2's validated dates on the string-compatible object.
        # Downstream tools therefore never need to parse the display text.
        focus_mention = next(
            (item for item in entity.time_scope.mentions if item.role == "FOCUS"),
            entity.time_scope.mentions[0] if entity.time_scope.mentions else None,
        )
        legacy_period = extract_period(entity.text) or extract_media_period(entity.text)
        display_period = (
            str(resolved)
            if focus_mention
            and focus_mention.year_source in {"default_current_year", "confirmed"}
            and focus_mention.granularity in {"MONTH", "QUARTER"}
            else legacy_period or str(resolved)
        )
        decision.period = ResolvedPeriod(
            display_period,
            start_date=resolved.start_date, end_date=resolved.end_date,
            comparison_start=resolved.comparison_start,
            comparison_end=resolved.comparison_end,
        )
        decision.missing_slots = [slot for slot in decision.missing_slots if slot != "period"]

    blocking_action = None
    if not is_market:
        if entity.brand.status == "ambiguous" and entity.brand.candidates:
            blocking_action = "confirm_brand_candidate"
        elif entity.brand.status == "not_found":
            blocking_action = "clarify_v2_brand"
    time_missing = set(entity.time_scope.missing_slots)
    if not blocking_action:
        if "time_scope.campaign_window" in time_missing:
            blocking_action = "provide_campaign_window"
        elif "time_scope.year_confirmation" in time_missing:
            blocking_action = "confirm_current_year"
        elif "time_scope.period_roles" in time_missing:
            blocking_action = "clarify_time_roles"
        elif "time_scope.valid_period" in time_missing:
            blocking_action = "clarify_v2_period"
    if blocking_action:
        decision.action = blocking_action
        decision.requires_confirmation = True
        decision.confidence_level = "ambiguous"
        decision.reason_codes.extend(entity.reason_codes)
    return decision


def _resume_entity_pending(text: str, state: SessionState) -> RouteResult | None:
    pending = state.pending_request or {}
    intent = pending.get("intent")
    if intent not in {"confirm_current_year", "provide_campaign_window", "confirm_brand_candidate"}:
        return None
    original = str(pending.get("original_text") or "").strip()
    if not original:
        return None
    if intent == "confirm_current_year":
        explicit_year = re.search(r"20\d{2}", text)
        accepted = bool(re.search(r"^(?:是|对|可以|确认|按今年|今年)$", text.strip()))
        if not accepted and not explicit_year:
            if re.search(r"^(?:不|不是|否)", text.strip()):
                return RouteResult(type="confirm_current_year", message="请补充正确年份，例如：2025年。")
            return None
        year = int(explicit_year.group(0)) if explicit_year else int(pending.get("proposed_year") or date.today().year)
        entity = resolve_entities(original, current_year=date.today().year, confirmed_year=year)
        decision = _decision_from_entities(original, entity)
        return _adapt_v2_decision(decision) if decision else None
    if intent == "provide_campaign_window":
        supplied = resolve_time_scope(text, current_year=date.today().year)
        if not supplied.focus_period or supplied.missing_slots:
            return RouteResult(
                type="provide_campaign_window",
                message="请提供包含年份的完整起止日期，例如：2026年5月13日至6月7日。",
            )
        entity = resolve_entities(original, current_year=date.today().year)
        entity.time_scope.focus_period = supplied.focus_period
        entity.time_scope.status = "exact"
        entity.time_scope.missing_slots = []
        for mention in entity.time_scope.mentions:
            if mention.granularity == "CAMPAIGN":
                mention.start_date = supplied.focus_period.start_date
                mention.end_date = supplied.focus_period.end_date
                mention.resolution_status = "exact"
                mention.canonical = str(supplied.focus_period)
        decision = _decision_from_entities(original, entity)
        return _adapt_v2_decision(decision) if decision else None
    candidates = list(pending.get("candidates") or [])
    selected = None
    ordinal = re.fullmatch(r"\s*([1-3])\s*", text)
    if ordinal and int(ordinal.group(1)) <= len(candidates):
        selected = candidates[int(ordinal.group(1)) - 1].get("canonical_brand_key")
    else:
        normalized_reply = re.sub(r"\s+", "", text).casefold()
        for item in candidates:
            names = [item.get("canonical_display_name"), *(item.get("aliases") or [])]
            if any(re.sub(r"\s+", "", str(name or "")).casefold() == normalized_reply for name in names):
                selected = item.get("canonical_brand_key")
                break
    if not selected:
        return None
    entity = resolve_entities(original, current_year=date.today().year, selected_brand_key=selected)
    decision = _decision_from_entities(original, entity)
    return _adapt_v2_decision(decision) if decision else None


def _resume_v2_scope(text: str, state: SessionState) -> RouteResult | None:
    pending = state.pending_request or {}
    if pending.get("intent") != "v2_media_scope":
        return None
    lowered = text.casefold()
    if any(token in lowered for token in ("整体bet", "整体", "overall", "全盘")):
        mode, channels = "OVERALL_BET", []
    elif any(token in lowered for token in ("只看抖音", "仅看抖音", "抖音相关", "douyin")):
        mode, channels = "CHANNEL_ONLY", ["DOUYIN"]
    else:
        return None
    intents = list(pending.get("intents") or ["BET"])
    if not pending.get("period"):
        action = "clarify_v2_period"
    elif intents == ["EC_BUSINESS", "BET"]:
        action = "clarify_analysis_scope"
    else:
        action = "media_analysis"
    decision = RouteDecision(
        intents=intents, question_mode=pending.get("question_mode") or "report",
        action=action,
        brand_surface=pending.get("brand"), period=pending.get("period"),
        commerce_scope=CommerceScope([pending.get("platform")] if pending.get("platform") else []),
        media_scope=MediaScope(mode, channels),
        requires_confirmation=intents == ["EC_BUSINESS", "BET"],
        decision_source="context_merge",
        reason_codes=["PENDING_MEDIA_SCOPE_RESOLVED"],
        inherited_parameters=[
            slot for slot, present in (
                ("brand_surface", pending.get("brand")), ("period", pending.get("period")),
                ("commerce_scope.platforms", pending.get("platform")),
            ) if present
        ],
    )
    return _adapt_v2_decision(decision)


def _resume_v2_slots(text: str, state: SessionState) -> RouteResult | None:
    pending = state.pending_request or {}
    if pending.get("intent") == "v2_period":
        period = extract_period(text) or extract_media_period(text)
        if not period:
            return None
        intents = list(pending.get("intents") or ["BET"])
        decision = RouteDecision(
            intents=intents, question_mode=pending.get("question_mode") or "report",
            action="clarify_analysis_scope" if intents == ["EC_BUSINESS", "BET"] else "media_analysis",
            brand_surface=pending.get("brand"), period=period,
            commerce_scope=CommerceScope([pending.get("platform")] if pending.get("platform") else []),
            media_scope=MediaScope(pending.get("media_mode"), list(pending.get("media_channels") or [])),
            requires_confirmation=intents == ["EC_BUSINESS", "BET"],
            decision_source="context_merge",
            reason_codes=["PENDING_PERIOD_RESOLVED"],
            inherited_parameters=["brand_surface", "commerce_scope.platforms", "media_scope.mode"],
        )
        return _adapt_v2_decision(decision)
    if pending.get("intent") == "v2_brand":
        reply_entity = None
        if os.environ.get("ENTITY_RESOLVER_V2_ENABLED", "0") == "1":
            reply_entity = resolve_entities(text, current_year=date.today().year)
            if reply_entity.brand.status == "ambiguous" and reply_entity.brand.candidates:
                entity_payload = reply_entity.to_dict()
                entity_payload["text"] = pending.get("original_text") or text
                decision = RouteDecision(
                    intents=list(pending.get("intents") or ["EC_BUSINESS"]),
                    question_mode=pending.get("question_mode") or "lookup",
                    action="confirm_brand_candidate",
                    brand_surface=reply_entity.brand.surface,
                    period=pending.get("period"),
                    commerce_scope=CommerceScope([pending.get("platform")] if pending.get("platform") else []),
                    media_scope=MediaScope(pending.get("media_mode"), list(pending.get("media_channels") or [])),
                    entity_resolution=entity_payload,
                    requires_confirmation=True, confidence_level="ambiguous",
                    reason_codes=["BRAND_NEEDS_CONFIRMATION"],
                )
                return _adapt_v2_decision(decision)
            if reply_entity.brand.status != "resolved":
                return RouteResult(
                    type="clarify_v2_brand",
                    message="品牌库中没有找到唯一匹配。请提供正式中文名或英文名。",
                )
        brand = (
            reply_entity.brand.surface if reply_entity and reply_entity.brand.status == "resolved"
            else extract_brand_surface(text) or text.strip()
        )
        if not brand or len(brand) > 40:
            return None
        intents = list(pending.get("intents") or ["EC_BUSINESS"])
        platform = pending.get("platform")
        media_mode = pending.get("media_mode")
        if intents == ["EC_BUSINESS", "BET"]:
            action = "clarify_analysis_scope" if pending.get("period") and platform and media_mode else "clarify_v2_period"
        elif intents == ["BET"]:
            action = "media_analysis" if pending.get("period") and media_mode else "clarify_v2_period"
        else:
            action = (
                {"TM": "default_chain", "DY": "douyin_business_analysis", "JD": "jd_business_analysis", "TTL": "three_platform_competitor_analysis"}.get(platform, "clarify_ec_platform")
                if pending.get("period") else "clarify_period"
            )
        decision = RouteDecision(
            intents=intents, question_mode=pending.get("question_mode") or "lookup", action=action,
            brand_surface=brand, period=pending.get("period"),
            commerce_scope=CommerceScope([platform] if platform else []),
            media_scope=MediaScope(media_mode, list(pending.get("media_channels") or [])),
            decision_source="context_merge", inherited_parameters=[
                slot for slot, present in (
                    ("period", pending.get("period")), ("commerce_scope.platforms", platform),
                    ("media_scope.mode", media_mode),
                ) if present
            ],
            reason_codes=["PENDING_BRAND_RESOLVED"],
            entity_resolution=(
                {
                    "text": pending.get("original_text") or text,
                    "time_scope": pending.get("time_scope") or {},
                    "brand": reply_entity.brand.to_dict(),
                    "reason_codes": [],
                }
                if reply_entity else None
            ),
        )
        return _adapt_v2_decision(decision)
    if pending.get("intent") == "v2_market_scope":
        period = extract_period(text) or pending.get("period")
        platform = explicit_platform(text) or pending.get("platform")
        segment = explicit_segment(text) or pending.get("segment")
        category = explicit_category(text) or pending.get("category")
        missing = [
            slot for slot, value in (
                ("period", period), ("commerce_scope.platforms", platform),
                ("market_scope.segment", segment), ("market_scope.category", category),
            ) if not value
        ]
        intents = list(pending.get("intents") or ["MARKET"])
        preflight_target = pending.get("preflight_target")
        reason_codes = ["PENDING_MARKET_SCOPE_MERGED"]
        if pending.get("bet_latest_ytd"):
            reason_codes.append("BET_LATEST_YTD_REQUESTED")
        decision = RouteDecision(
            intents=intents, question_mode=pending.get("question_mode") or "lookup",
            action=(
                "clarify_market_scope" if missing
                # Supplying the last missing scope is also an explicit
                # confirmation of the preserved multi-step request. Execute
                # the complete target instead of collapsing back to ranking.
                else preflight_target if preflight_target
                else "market_analysis"
            ),
            period=period, commerce_scope=CommerceScope([platform] if platform else []),
            media_scope=MediaScope("OVERALL_BET", []) if "BET" in intents else MediaScope(),
            market_scope=MarketScope(segment, category),
            ranking_metric=pending.get("ranking_metric"), ranking_limit=pending.get("ranking_limit"),
            missing_slots=missing, requires_confirmation=bool(missing),
            confidence_level="ambiguous" if missing else "exact",
            decision_source="context_merge",
            reason_codes=reason_codes,
            inherited_parameters=[
                slot for slot, inherited in (
                    ("period", pending.get("period")),
                    ("commerce_scope.platforms", pending.get("platform")),
                    ("market_scope.segment", pending.get("segment")),
                    ("market_scope.category", pending.get("category")),
                ) if inherited
            ],
        )
        result = _adapt_v2_decision(decision)
        result.original_text = pending.get("original_text") or result.original_text
        return result
    return None


def _inherit_v2_context(decision: RouteDecision, text: str, state: SessionState) -> RouteDecision:
    pronoun = bool(re.search(r"(?:那它|它|这个品牌|该品牌|这个牌子)", text))
    if not pronoun:
        return decision
    media_only = decision.intents == ["BET"]
    ctx = state.bet_context if media_only else state.ec_context
    inherited = []
    if not ctx.brand:
        ctx = state.drilldown_ctx
    if ctx.brand and (not decision.brand_surface or decision.brand_surface in {"那它", "它", "这个品牌", "该品牌"}):
        decision.brand_surface = ctx.brand
        inherited.append("brand_surface")
    if ctx.period and not decision.period:
        decision.period = ctx.period
        inherited.append("period")
    decision.missing_slots = [
        slot for slot in decision.missing_slots
        if not (slot == "brand_surface" and decision.brand_surface)
        and not (slot == "period" and decision.period)
    ]
    if media_only and decision.media_scope.mode and not decision.missing_slots:
        decision.action = "media_analysis"
    decision.decision_source = "context_merge"
    decision.inherited_parameters.extend(inherited)
    decision.reason_codes.append("EXPLICIT_PRONOUN_CONTEXT_INHERITANCE")
    return decision


def _opportunity_followup_with_context(text: str, state: SessionState) -> RouteResult | None:
    """A message that both asks for opportunity insight and matches an
    existing EC report context should stay a followup even if it also reads
    like a fresh report request. Without this, a compound message like
    "分析欧莱雅官方旗舰店昨天的生意表现，找出...机会点的行动方案" gets caught by
    is_unspecified_platform_business_question purely on its "生意表现"
    wording, asks the user to re-clarify a platform they already confirmed,
    and drops the opportunity request and the already-cached report entirely.
    """
    if not asks_for_opportunity(text):
        return None
    ctx = state.ec_context
    entities = resolve_entities(text, current_year=date.today().year)
    explicit_brand = (
        entities.brand.surface if entities.brand.status == "resolved" else None
    )
    explicit_period = entities.time_scope.focus_period
    explicit_platform = _explicit_business_platform(text)
    brand = explicit_brand or ctx.brand or state.task_context.brand
    period = explicit_period or ctx.period or state.task_context.period
    platform = explicit_platform or ctx.platform or state.task_context.platform
    if not brand or not period or not platform:
        return None
    explicit_scope = bool(explicit_brand or explicit_period or explicit_platform)
    prior_brand = ctx.brand or state.task_context.brand
    prior_period = ctx.period or state.task_context.period
    prior_platform = ctx.platform or state.task_context.platform
    explicit_brand_key = str(
        entities.brand.canonical_display_name
        or english_brand_for_chinese(explicit_brand)
        or explicit_brand
        or ""
    ).casefold()
    prior_brand_key = str(
        english_brand_for_chinese(prior_brand) or prior_brand or ""
    ).casefold()
    identity_changed = bool(
        (explicit_brand and prior_brand and explicit_brand_key != prior_brand_key)
        or (explicit_period and prior_period and explicit_period != prior_period)
        or (explicit_platform and prior_platform and explicit_platform != prior_platform)
    )
    reasoning_v3 = os.environ.get("AGENT_REASONING_V3_ENABLED", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }
    reasoning_v3_shadow = os.environ.get("AGENT_REASONING_V3_SHADOW", "1").strip().lower() in {
        "1", "true", "yes", "on",
    }
    # A complete opportunity request with an explicit scope is a new analysis
    # task when its identity differs from the latest report.  Never let the
    # cached report overwrite the current turn's validated entities.
    # V3 can also reason over a fully matched cached report for an omitted
    # follow-up such as "还有什么机会点".  The complete identity check
    # above prevents this from reusing a partial or cross-scope context.
    route_type = "opportunity_analysis" if reasoning_v3 else "skill_dispatch"
    return RouteResult(
        type=route_type,
        brand=brand,
        period=period,
        platform=platform,
        brand_aliases=[explicit_brand] if explicit_brand else list(ctx.brand_aliases or []),
        followup_text=text,
        original_text=text,
        response_strategy="MULTI_STEP_ANALYSIS" if route_type == "opportunity_analysis" else "TARGETED_ANSWER",
        route_decision={
            "shadow_route_type": (
                "opportunity_analysis" if reasoning_v3_shadow and not reasoning_v3 else None
            ),
            "reason_codes": [
                "OPPORTUNITY_REQUEST",
                "CURRENT_EXPLICIT_SCOPE_LOCKED" if explicit_scope else "MATCHED_REPORT_CONTEXT",
                "NEW_TASK_IDENTITY" if identity_changed else "CONTINUE_TASK_IDENTITY",
                *( ["AGENT_REASONING_V3_SHADOW"] if reasoning_v3_shadow and not reasoning_v3 else []),
            ],
            "task_context_patch": {
                "relation": "NEW_TASK" if identity_changed else "CONTINUE_TASK",
                "slot_ops": {
                    "original_question": {"op": "SET", "value": text, "source": "current_explicit"},
                    "brand": {
                        "op": "SET" if explicit_brand else "KEEP", "value": explicit_brand,
                        "source": "current_explicit" if explicit_brand else "task_context",
                    },
                    "period": {
                        "op": "SET" if explicit_period else "KEEP", "value": explicit_period,
                        "source": "current_explicit" if explicit_period else "task_context",
                    },
                    "platform": {
                        "op": "SET" if explicit_platform else "KEEP", "value": explicit_platform,
                        "source": "current_explicit" if explicit_platform else "task_context",
                    },
                },
                "intents": ["EC_BUSINESS"],
                "goals": ["DESCRIBE_BUSINESS", "FIND_OPPORTUNITIES", "RECOMMEND_ACTIONS"],
                "reason_codes": ["EXPLICIT_SLOTS_OVERRIDE_CONTEXT"],
            },
        },
    )


def _market_collection_followup(text: str, state: SessionState) -> RouteResult | None:
    """Resolve '这三位/这些品牌/他们' against the latest market ranking."""
    top_brands = list(state.market_context.top_brands or [])
    if not top_brands:
        return None
    collective_reference = bool(re.search(
        r"这s*三位|这s*几位|这些品牌|这几个品牌|他们|它们", text,
    ))
    listed_top_brands = False
    if not collective_reference:
        entity = resolve_entities(text, current_year=date.today().year)
        if entity.brand.match_method == "multiple_exact_brands":
            def normalize(value: object) -> str:
                return re.sub(r"[^a-z0-9一-鿿]", "", str(value).casefold())

            top_keys = {normalize(value) for value in top_brands}
            candidate_keys = {
                normalize(candidate.canonical_display_name)
                for candidate in entity.brand.candidates
            }
            listed_top_brands = len(top_keys & candidate_keys) >= 2
    if not (collective_reference or listed_top_brands):
        return None
    pending = state.pending_request or {}
    task_text = str(pending.get("original_text") or text) if listed_top_brands else text
    has_business = bool(re.search(r"生意|经营|gmv|平台|天猫|抖音|京东|商品|选品", task_text, re.I))
    has_bet = is_media_question(task_text)
    if not (has_business or has_bet):
        return None
    return RouteResult(
        type="market_brand_deep_dive",
        original_text=task_text,
        period=state.market_context.period,
        segment=state.market_context.segment,
        category=state.market_context.category,
        platform="TTL",
        market_view="top_brands",
        ranking_metric="gmv_actual",
        ranking_limit=min(len(top_brands), 3),
        include_bet=has_bet,
        bet_latest_ytd=has_bet and bool(re.search(r"今年|最新|ytd", task_text, re.I)),
        media_mode="OVERALL_BET" if has_bet else None,
        media_scope="full_bet" if has_bet else None,
    )


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
    if any(h in text for h in META_HINTS):
        return True
    if re.search(r"(?:支持|能否|能不能|可以不可以).{0,10}(?:Key\s*Driver|SKU|系列|品类|链接).{0,3}(?:吗|么|？|\?)?$", text, re.I):
        return True
    return bool(re.search(
        r"(?:天猫|抖音|京东|三平台|BET|媒体).{0,8}"
        r"(?:支持|能否|能不能|可以|能).{0,10}"
        r"(?:分析|看|查|Key\s*Driver|SKU|系列|品类|链接)",
        text, re.I,
    ))


def is_data_availability_question(text: str) -> bool:
    value = str(text or "")
    return any(token in value for token in (
        "数据最新到", "数据更新到", "更新到什么时候",
        "最新到几月", "数据到几月", "数据最新是什么时候",
    ))


def _unified_response_enabled() -> bool:
    return os.environ.get("UNIFIED_RESPONSE_STRATEGY_ENABLED", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _unified_targeted_route(text: str, state: SessionState) -> RouteResult | None:
    if not _unified_response_enabled() or is_meta_question(text) or is_data_availability_question(text):
        return None
    lowered = text.casefold()
    if is_market_question(text) or has_explicit_media_intent(text):
        return None
    if re.search(r"完整.{0,4}报告|生成.{0,12}报告|整体生意|生意怎么样|整体表现", text):
        return None
    if re.search(r"先.+(?:再|然后)|(?:再|然后).+下钻", text):
        return None
    targeted = is_narrow_followup(text) or bool(re.search(
        r"key\s*driver|sku|链接|商品标题|这个品类|该品类|这个driver|该driver|"
        r"[一-龥A-Za-z0-9_-]{1,16}(?:品类|系列).{0,8}(?:表现|增长|下滑|贡献|分析|排名)",
        lowered, re.I,
    ))
    if not targeted:
        return None
    ctx = state.ec_context
    has_explicit_scope = bool(
        extract_period(text)
        or re.search(r"天猫|抖音|京东|三平台|tmall|douyin|jingdong|\b(?:tm|dy|jd)\b", text, re.I)
    )
    # A dimension phrase such as “防晒品类” is not a brand. Only run the
    # legacy brand fallback when the same turn also carries an explicit task scope.
    explicit_brand = None
    if has_explicit_scope:
        resolved = resolve_entities(text, current_year=date.today().year).brand
        if resolved.status == "resolved":
            explicit_brand = resolved.surface
    brand = explicit_brand or ctx.brand or state.task_context.brand or state.drilldown_ctx.brand
    period = extract_period(text) or ctx.period or state.task_context.period or state.drilldown_ctx.period
    platform = (
        "DY" if "抖音" in text else "JD" if "京东" in text else "TM" if "天猫" in text else
        inferred_key_driver_platform(text) or ctx.platform or state.task_context.platform
    )
    aliases = [explicit_brand] if explicit_brand else list(ctx.brand_aliases or state.drilldown_ctx.brand_aliases)
    return RouteResult(
        type="skill_dispatch", brand=brand, period=period, platform=platform,
        brand_aliases=aliases, followup_text=_strip_followup_prefix(text),
        response_strategy="TARGETED_ANSWER",
    )


def is_data_caliber_question(text: str) -> bool:
    return any(h in text for h in CALIBER_HINTS)


def is_media_question(text: str) -> bool:
    lowered = (text or "").lower()
    media_text = re.sub(r"non[\s_-]*kol", "", lowered, flags=re.I)
    return has_explicit_media_intent(text) or any(h.lower() in media_text for h in MEDIA_HINTS) or bool(
        re.search(r"(?<![a-z])tr(?![a-z])", media_text)
    )


def _explicit_business_platform(text: str) -> str | None:
    lowered = str(text or "").lower()
    if "天猫" in lowered or bool(re.search(r"(?<![a-z])tmall(?![a-z])|(?<![a-z])tm(?![a-z])", lowered)):
        return "TM"
    if "抖音" in lowered or bool(re.search(r"(?<![a-z])(?:douyin|dy)(?![a-z])", lowered)):
        return "DY"
    if "京东" in lowered or bool(re.search(r"(?<![a-z])(?:jingdong|jd)(?![a-z])", lowered)):
        return "JD"
    return inferred_key_driver_platform(text)


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
    ranking_metric: str | None = None, include_bet: bool = False,
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
    bet_step = (
        "- 对入选品牌逐一查询今年1月至各品牌Topline最新可用月的BET；"
        "如果最新月份不同，会分别标注。\n"
        if include_bet else ""
    )
    return (
        f"这是一个需要先确认口径的市场深度分析。我理解为：在{period or '待确认期间'}的Pure Mass Beauty中，"
        f"按TM＋DY＋JD三平台TTL的{metric_label}选Top {limit or 3}，再解释它们如何形成当前排名。\n\n"
        "**执行计划**\n"
        "- 先确定Top品牌，再逐品牌拆解三平台结构、增长最多的平台、月度生意节奏和可验证商品。\n"
        f"{bet_step}\n"
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
        brand = _sanitize_routed_brand(detect_brand_hint(text)) or state.drilldown_ctx.brand
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
        if os.environ.get("EXECUTABLE_PLAN_V2_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}:
            return RouteResult(
                type="market_brand_deep_dive", original_text=text,
                period=plan.period, segment=plan.segment, category=plan.category,
                platform=plan.platform, market_view=plan.view,
                ranking_metric=plan.ranking_metric, ranking_limit=plan.ranking_limit,
                include_bet=bool(re.search(r"bet|媒体投资|媒体花费|投放", text, re.I)),
                bet_latest_ytd=bool(re.search(r"今年|最新|ytd", text, re.I)),
            )
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
            original_text=pending.get("original_text"),
            include_bet=bool(pending.get("include_bet")),
            bet_latest_ytd=bool(pending.get("bet_latest_ytd")),
            preflight_target=target,
            message=_preflight_message(
                target=target, brand=pending.get("brand"), period=pending.get("period"),
            ),
        )
    if _is_confirm_reply(text) or (target == "brand_business_investment_analysis" and platform):
        task_bindings = list(pending.get("task_bindings") or [])
        task_periods = {
            binding.get("intent"): _period_from_task_binding(binding)
            for binding in task_bindings
            if binding.get("intent")
        }
        return RouteResult(
            type=target, original_text=pending.get("original_text"),
            brand=pending.get("brand"), period=pending.get("period"),
            brand_aliases=list(pending.get("brand_aliases") or []),
            platform=platform, segment=pending.get("segment"),
            market_view=pending.get("market_view"), ranking_metric=metric,
            ranking_limit=int(pending.get("ranking_limit") or 3),
            media_scope=pending.get("media_scope") or "full_bet",
            media_mode=pending.get("media_mode") or "OVERALL_BET",
            media_channels=list(pending.get("media_channels") or []),
            task_bindings=task_bindings,
            business_period=task_periods.get("EC_BUSINESS"),
            bet_period=task_periods.get("BET"),
            include_bet=bool(pending.get("include_bet")),
            bet_latest_ytd=bool(pending.get("bet_latest_ytd")),
        )
    return RouteResult(
        type="clarify_analysis_scope", original_text=pending.get("original_text"),
        brand=pending.get("brand"), period=pending.get("period"),
        brand_aliases=list(pending.get("brand_aliases") or []), platform=platform,
        segment=pending.get("segment"), market_view=pending.get("market_view"),
        ranking_metric=metric, ranking_limit=int(pending.get("ranking_limit") or 3),
        include_bet=bool(pending.get("include_bet")),
        bet_latest_ytd=bool(pending.get("bet_latest_ytd")),
        preflight_target=target,
        message=_preflight_message(
            target=target, brand=pending.get("brand"), period=pending.get("period"),
            platform=platform, limit=int(pending.get("ranking_limit") or 3),
            ranking_metric=metric, include_bet=bool(pending.get("include_bet")),
        ),
    )


def _route_business_investment(text: str, state: SessionState) -> RouteResult:
    brand = _sanitize_routed_brand(detect_brand_hint(text)) or state.drilldown_ctx.brand
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
    has_platform = (
        "抖音" in lowered
        or bool(re.search(r"(?<![a-z])(?:douyin|dy)(?![a-z])", lowered))
        or inferred_key_driver_platform(text) == "DY"
    )
    has_business = any(hint in lowered for hint in BUSINESS_HINT_WORDS) or inferred_key_driver_platform(text) == "DY"
    return (
        has_platform and has_business
        and not is_media_question(text)
        and not is_market_question(text)
    )


def is_jd_business_question(text: str) -> bool:
    lowered = str(text or "").lower()
    has_platform = "京东" in lowered or bool(re.search(r"(?<![a-z])(?:jingdong|jd)(?![a-z])", lowered))
    has_business = any(hint in lowered for hint in BUSINESS_HINT_WORDS)
    return (
        has_platform and has_business
        and not is_media_question(text)
        and not is_market_question(text)
    )


def is_tmall_business_question(text: str) -> bool:
    lowered = str(text or "").lower()
    has_platform = (
        "天猫" in lowered
        or bool(re.search(r"(?<![a-z])tmall(?![a-z])|(?<![a-z])tm(?![a-z])", lowered))
        or inferred_key_driver_platform(text) == "TM"
    )
    has_business = any(hint in lowered for hint in BUSINESS_HINT_WORDS) or inferred_key_driver_platform(text) == "TM"
    return has_platform and has_business and not is_media_question(text) and not is_market_question(text)


def is_three_platform_competitor_question(text: str) -> bool:
    lowered = str(text or "").lower()
    strong_trigger = any(token in lowered for token in (
        "竞品三平台生意分析", "三平台竞品生意分析", "三平台生意分析",
        "三平台竞品分析", "三平台生意报告", "三平台生意",
    ))
    mentions_all_platforms = all(token in lowered for token in ("天猫", "抖音", "京东"))
    has_report_intent = any(token in lowered for token in (
        "生意", "经营", "gmv", "份额", "类目", "渠道", "报告", "分析", "复盘",
    ))
    return (
        (strong_trigger or (mentions_all_platforms and has_report_intent))
        and not is_media_question(text)
        and not is_market_question(text)
    )


def is_unspecified_platform_business_question(text: str) -> bool:
    """Brand business request that must not silently default to Tmall."""
    lowered = str(text or "").lower()
    has_business = any(hint in lowered for hint in BUSINESS_HINT_WORDS)
    return (
        has_business
        and _explicit_business_platform(text) is None
        and not is_media_question(text)
        and not is_market_question(text)
    )


def _route_unspecified_platform_business(text: str, state: SessionState) -> RouteResult:
    ordinal = _ordinal_brand_route(text, state)
    if ordinal and ordinal.type == "default_chain":
        return RouteResult(
            type="clarify_ec_platform",
            original_text=text,
            brand=ordinal.brand,
            period=ordinal.period,
            brand_aliases=[ordinal.brand] if ordinal.brand else [],
            include_bet=is_business_investment_question(text),
        )
    brand = _sanitize_routed_brand(detect_brand_hint(text)) or state.drilldown_ctx.brand
    period = extract_period(text) or state.drilldown_ctx.period
    if not brand:
        return RouteResult(type="guide")
    return RouteResult(
        type="clarify_ec_platform",
        original_text=text,
        brand=brand,
        period=period,
        brand_aliases=[brand],
        include_bet=is_business_investment_question(text),
    )


def _resume_active_task_context(text: str, state: SessionState) -> RouteResult | None:
    """Resolve a clarification by slot semantics, independent of phrasing."""
    merged = resolve_task_turn(text, state)
    if not merged:
        return None
    task_payload = {
        "original_question": merged.original_question,
        "brand": merged.brand, "brand_aliases": merged.brand_aliases,
        "period": merged.period, "platform": merged.platform,
        "intents": merged.intents, "goals": merged.goals,
        "media_mode": merged.media_mode, "media_channels": merged.media_channels,
        "comparison_metric": merged.comparison_metric,
        "awaiting_slot": None, "status": "active",
        "last_relation": merged.relation,
    }
    before = state.task_context
    task_trace = {
        "task_id": before.task_id,
        "before_version": before.state_version,
        "relation": merged.relation,
        "explicit_patch": {
            "brand": merged.brand if merged.brand != before.brand else None,
            "period": merged.period if merged.period != before.period else None,
            "platform": merged.platform if merged.platform != before.platform else None,
        },
        "missing_slots": list(merged.missing_slots),
    }
    common = {
        "original_text": merged.combined_question or merged.original_question,
        "brand": merged.brand, "brand_aliases": merged.brand_aliases,
        "period": merged.period, "platform": merged.platform,
        "media_mode": merged.media_mode, "media_channels": merged.media_channels,
        "include_bet": "BET" in merged.intents or "BET" in merged.goals,
        "ranking_metric": merged.comparison_metric,
        "route_decision": {
            "decision_source": "active_task_merge",
            "reason_codes": merged.reason_codes,
            "task_context": task_payload,
            "task_context_trace": task_trace,
        },
    }
    if merged.missing_slots:
        missing = merged.missing_slots[0]
        action = {
            "brand": "clarify_v2_brand",
            "period": "clarify_v2_period",
            "platform": "clarify_ec_platform",
        }.get(missing)
        if action:
            common["route_decision"]["missing_slots"] = list(merged.missing_slots)
            return RouteResult(type=action, **common)
    has_ec = "EC_BUSINESS" in merged.intents or "EC_BUSINESS" in merged.goals
    has_bet = "BET" in merged.intents or "BET" in merged.goals
    compare_then_drill = (
        "COMPARE_PLATFORMS" in merged.goals
        and "DRILL_SELECTED_PLATFORM" in merged.goals
    )
    if has_ec and merged.platform == "TTL" and compare_then_drill:
        return RouteResult(type="brand_platform_deep_dive", **common)
    if has_ec and has_bet and merged.platform in {"TM", "DY", "JD"}:
        return RouteResult(type="brand_business_investment_analysis", **common)
    if has_ec and merged.platform:
        route_type = {
            "TTL": "three_platform_competitor_analysis",
            "TM": "default_chain", "DY": "douyin_business_analysis", "JD": "jd_business_analysis",
        }.get(merged.platform)
        if route_type:
            return RouteResult(type=route_type, **common)
    if has_bet and merged.brand and merged.period:
        return RouteResult(type="media_analysis", **common)
    return None


def _strip_period_syntax(text: str) -> str:
    """Remove supported raw period spellings before extracting a brand."""
    value = str(text or "")
    patterns = (
        r"(?<!\d)20\d{6}\s*[~～—–\-至到]+\s*20\d{6}(?!\d)",
        r"(?<!\d)20\d{2}/\d{1,2}/\d{1,2}\s*[~～—–\-至到]+\s*20\d{2}/\d{1,2}/\d{1,2}(?!\d)",
        r"(?<!\d)20\d{2}-\d{1,2}-\d{1,2}\s*(?:~|～|—|–|至|到|\s-\s)\s*20\d{2}-\d{1,2}-\d{1,2}(?!\d)",
        r"(?<!\d)20\d{2}\s+\d{1,2}\s*[~～—–\-至到]+\s*\d{1,2}(?:\s*月)?(?!\d)",
        # Full day-range with a month on both sides ("5月1日至5月20日") must be
        # tried before the day-only-on-the-right pattern below, or that
        # pattern only consumes up to the first digit of the second month
        # (matching "...至5" out of "至5月20日") and leaves "月20日" attached
        # to whatever text follows.
        r"(?:(?:20\d{2})年)?\d{1,2}月\d{1,2}[日号]?\s*[~～—–\-至到]+\s*(?:(?:20\d{2})年)?\d{1,2}月\d{1,2}[日号]?",
        r"(?:(?:20\d{2})年)?\d{1,2}月\d{1,2}[日号]?\s*[~～—–\-至到]+\s*\d{1,2}[日号]?",
        r"(?:(?:20\d{2})年)?\d{1,2}月?\s*[~～—–\-至到]+\s*(?:(?:20\d{2})年)?\d{1,2}月",
        r"20\d{2}-\d{1,2}-\d{1,2}",
        r"(?:(?:20\d{2})年)?\d{1,2}月\d{1,2}[日号]?",
        r"(?:(?:20\d{2})年)?\d{1,2}月",
        r"(?:(?:20\d{2})年?)?[Qq][1-4]",
        # Campaign-window literals (extract_period/normalize_period_hint
        # already resolves these to a date range separately); without this,
        # "618"/"双11"/"520" survive into the brand candidate untouched, e.g.
        # "谷雨天猫618怎么样" would extract brand="谷雨618".
        r"(?:(?:20\d{2})年?)?(?:618|双11|双十一|520)",
    )
    for pattern in patterns:
        value = re.sub(pattern, " ", value)
    return value


def _sanitize_routed_brand(value: str | None) -> str | None:
    """Final guardrail shared by every route before a brand reaches a query."""
    candidate = str(value or "").strip()
    if not candidate:
        return None
    # Store-type suffixes ("欧莱雅官方旗舰店") are common phrasing but not part
    # of the brand name, and can sit mid-string rather than at an edge this
    # function's other patterns anchor to.
    candidate = re.sub(r"官方旗舰店|海外旗舰店|全球旗舰店|旗舰店|专卖店|自营店|官方店", "", candidate)
    # Stacked politeness/subject tokens ("你帮我看下") need to repeat over a
    # single pass, not just match once — a non-repeating group only strips
    # one of them and leaves the rest glued to the brand (mirrors the same
    # fix already applied to router_v2.extract_brand_surface).
    candidate = re.sub(
        r"^\s*(?:(?:你好|你现在能|你现在|你可以|你能|你|请|麻烦|帮我|帮忙|看一下|看看|看下|分析一下|分析|看)\s*)*",
        "",
        candidate,
        flags=re.IGNORECASE,
    )
    candidate = re.sub(r"[，,。！？!?：:=\s]+$", "", candidate)
    candidate = re.sub(
        r"(?:的)?(?:生意|经营|商品|产品|品类|品牌)?(?:表现|情况)?"
        r"(?:怎么样|咋样|如何|怎样|好不好|是什么样的?|是什么情况)(?:呢|吗|嘛)?$",
        "",
        candidate,
        flags=re.IGNORECASE,
    )
    # Quantity-style asks ("生意是多少"/"卖了多少钱") use a completely different
    # closing verb than the descriptive "怎么样/如何" phrasings above, so they
    # need their own suffix strip; otherwise the trailing clause survives and
    # pollutes the extracted brand name. The verb+quantity phrases ("卖了多少
    # 钱") must be stripped before the bare "多少/多少钱" pattern below, or the
    # bare pattern greedily eats just the tail ("多少钱") and leaves "卖了"
    # stuck to the brand.
    candidate = re.sub(
        r"(?:卖了多少钱|花了多少钱|赚了多少钱|卖了多少|花了多少|赚了多少|卖多少|花多少|赚多少)$",
        "",
        candidate,
        flags=re.IGNORECASE,
    )
    candidate = re.sub(
        r"(?:的)?(?:生意|经营|GMV|gmv|销售额|销量|花费|费用|媒体花费|媒体投资)?"
        r"(?:是多少钱|是多少|有多少|多少钱|多少)(?:呢|啊|呀|吗)?$",
        "",
        candidate,
        flags=re.IGNORECASE,
    )
    candidate = re.sub(
        r"(?:的)?(?:生意|经营|表现|情况|生意表现|经营表现|生意情况|经营情况)$",
        "",
        candidate,
        flags=re.IGNORECASE,
    )
    # Earlier phrase removals can leave a connector word ("在/于/的") trailed
    # by whitespace instead of directly at the string end (e.g. a platform
    # word in the middle of the sentence was replaced with a space). Strip
    # trailing whitespace first so the connector is actually at $ when this
    # pattern runs, otherwise it silently no-ops and the connector survives
    # into the final brand string.
    candidate = re.sub(r"(?:\s*(?:在|于|的))+$", "", candidate.rstrip()).strip()
    candidate = re.sub(r"[，,。！？!?：:=\s]+", "", candidate)
    if candidate in {"它", "该品牌", "这个品牌", "这个", "其", "公司", "品牌"}:
        return None
    return candidate if 1 <= len(candidate) <= 30 else None


def _detect_douyin_business_brand(text: str) -> str | None:
    candidate = _strip_period_syntax(text)
    candidate = re.sub(r"(?i)(?<![a-z])dy(?![a-z])|抖音", " ", candidate)
    candidate = re.sub(
        r"品牌生意分析报告|品牌生意分析|生意分析报告|生意分析|经营分析|"
        r"生意复盘|经营复盘|GMV复盘|gmv复盘|品牌复盘|生意报告|生意月报|"
        r"生意怎么样|经营怎么样|生意咋样|经营咋样|表现怎么样|表现咋样|"
        r"生意表现|经营表现|商品表现|产品表现|品类表现|生意情况|经营情况|"
        r"怎么样|咋样|如何|"
        r"生成|输出|报告|品牌|平台",
        " ",
        candidate,
    )
    candidate = re.sub(r"^\s*(请重新分析|重新分析|分析一下|帮我|请|麻烦|看一下|分析)\s*", "", candidate)
    candidate = re.sub(r"(?:(?:在|于|的)\s*)+$", "", candidate)
    candidate = re.sub(r"[，,。！？!?：:=\s]+", "", candidate)
    candidate = re.sub(r"(?:在|于|的)$", "", candidate)
    return _sanitize_routed_brand(candidate)


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
        r"生意怎么样|经营怎么样|生意咋样|经营咋样|表现怎么样|表现咋样|"
        r"生意表现|经营表现|商品表现|产品表现|生意情况|经营情况|"
        r"怎么样|咋样|如何|"
        r"gmv和品类表现|GMV和品类表现|GMV和品类|gmv和品类|GMV与品类|"
        r"gmv与品类|品类表现|"
        r"京东自营旗舰店|自营旗舰店|生成|输出|报告|品牌|平台",
        " ",
        candidate,
    )
    candidate = re.sub(r"^\s*(请做|帮我|请|麻烦|看一下|分析一下|分析|做|看)\s*", "", candidate)
    candidate = re.sub(r"(?:(?:在|于|的)\s*)+$", "", candidate)
    candidate = re.sub(r"[，,。！？!?::=\s]+", "", candidate)
    candidate = re.sub(r"^(?:在|于|的)+|(?:在|于|的)+$", "", candidate)
    return _sanitize_routed_brand(candidate)


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
        platform="DY",
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
        platform="JD",
    )


def _route_tmall_business(text: str, state: SessionState) -> RouteResult:
    candidate = _strip_period_syntax(text)
    period = extract_period(candidate) or state.drilldown_ctx.period
    period = extract_period(text) or state.drilldown_ctx.period
    candidate = re.sub(r"(?i)(?<![a-z])tmall(?![a-z])|(?<![a-z])tm(?![a-z])|天猫", " ", candidate)
    candidate = re.sub(
        r"品牌生意分析报告|品牌生意分析|生意分析报告|生意分析|经营分析|"
        r"生意复盘|经营复盘|GMV复盘|gmv复盘|生意报告|生意月报|"
        r"生意怎么样|经营怎么样|生意咋样|经营咋样|表现怎么样|表现咋样|"
        r"生意情况|经营情况|主推商品|商品表现|产品表现|生成|输出|报告|品牌|平台",
        " ", candidate,
    )
    candidate = re.sub(r"^\s*(请做|帮我|请|麻烦|看一下|分析一下|分析|做|看)\s*", "", candidate)
    candidate = re.sub(r"(?:(?:在|于|的)\s*)+$", "", candidate)
    brand = _sanitize_routed_brand(candidate) or state.drilldown_ctx.brand
    if not brand:
        return RouteResult(type="guide")
    return RouteResult(
        type="default_chain" if period else "clarify_period",
        brand=brand, period=period, brand_aliases=[brand], platform="TM",
    )


def _detect_three_platform_brand(text: str) -> str | None:
    candidate = _strip_period_syntax(text)
    candidate = re.sub(
        r"竞品三平台生意分析报告|三平台竞品生意分析报告|三平台生意分析报告|"
        r"竞品三平台生意分析|三平台竞品生意分析|三平台生意分析|"
        r"三平台竞品分析|三平台生意报告|三平台生意|三平台|全平台|天猫|抖音|京东|TMALL|DOUYIN|JINGDONG|"
        r"同比去年同期|同比同期|同比|品牌|生成|输出|报告|分析|复盘|请按|模板",
        " ",
        candidate,
        flags=re.IGNORECASE,
    )
    candidate = re.sub(r"^\s*(请做|帮我|请|麻烦|看一下|分析一下|分析|做|看)\s*", "", candidate)
    candidate = re.split(
        r"\s*(?:在|的)?\s*(?:GMV|gmv|份额|类目|品类|渠道|生意表现|经营表现)",
        candidate,
        maxsplit=1,
    )[0]
    candidate = re.split(r"[，,；;]\s*(?:输出|包含|需要|看)", candidate, maxsplit=1)[0]
    candidate = re.sub(r"[「」『』【】\[\]，,、。！？!?：:=\s]+", "", candidate)
    candidate = re.sub(r"^(?:在|于|的)+|(?:在|于|的)+$", "", candidate)
    return _sanitize_routed_brand(candidate)


def _strip_followup_prefix(text: str) -> str:
    return re.sub(r"^\s*追问\s*[:：,，]\s*", "", str(text or ""), count=1).strip()


def _detect_followup_brand(text: str) -> str | None:
    """Extract a brand explicitly named before a follow-up KPI phrase."""
    candidate = _strip_followup_prefix(text)
    candidate = re.sub(
        r"^\s*(?:为什么|请问|帮我看(?:一下)?|看一下|看看|分析一下)\s*",
        "",
        candidate,
    )
    period = extract_media_period(candidate) or extract_period(candidate)
    if period:
        candidate = candidate.replace(period, " ")
    match = re.search(
        r"(?P<brand>[A-Za-z][A-Za-z0-9&.'\- ]{0,30}?|"
        r"[\u4e00-\u9fa5][\u4e00-\u9fa5A-Za-z0-9]{1,20}?)"
        r"(?:的)?(?:媒体花费|媒体费用|媒体投资|费用|费比|BET|AIT|GMV|生意|表现)",
        candidate,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    brand = match.group("brand").strip()
    if brand in {"它", "这个", "该品牌", "公司", "品牌", "上面", "刚才"}:
        return None
    return _sanitize_routed_brand(brand)


def _followup_context(text: str, state: SessionState):
    last_view = str(state.drilldown_ctx.last_analysis_view or "")
    if is_media_question(text) or last_view in {"media_analysis", "bet_followup"}:
        return state.bet_context, True
    return state.ec_context, False


def _route_three_platform_competitor(text: str, state: SessionState) -> RouteResult:
    brand = _detect_three_platform_brand(text) or state.drilldown_ctx.brand
    period = extract_period(text) or state.drilldown_ctx.period
    if not brand:
        return RouteResult(type="clarify_three_platform_brand", period=period)
    return RouteResult(
        type="three_platform_competitor_analysis" if period else "clarify_three_platform_period",
        brand=brand,
        period=period,
        brand_aliases=[brand],
        platform="TTL",
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
    candidate = re.sub(r"(的)?(生意|表现|怎么样|咋样|如何|分析|情况|期间|帮我|请|麻烦|看一下|看看|分析一下)", "", candidate)
    candidate = re.sub(r"[，,。！？!?\s]+", "", candidate)
    cleaned = _sanitize_routed_brand(candidate)
    if cleaned:
        return cleaned
    fallback = _sanitize_routed_brand(detect_brand_hint(user_text))
    if fallback:
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
    candidate = re.sub(r"^\s*追问\s*[:：,，]\s*", "", candidate)
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
    return _sanitize_routed_brand(candidate)


def _route_media(text: str, state: SessionState) -> RouteResult:
    ctx = state.drilldown_ctx
    brand = _detect_media_brand(text) or ctx.brand
    period = extract_media_period(text) or ctx.period
    if not brand:
        return RouteResult(type="guide")
    return RouteResult(type="media_analysis", brand=brand, period=period)


def _resume_period_only_domain(text: str, state: SessionState) -> RouteResult | None:
    """Keep the active business domain when a reply only changes the period."""
    period = extract_media_period(text)
    if not period:
        return None
    lowered = text.casefold()
    if is_media_question(text) or any(
        token in lowered for token in ("gmv", "生意", "经营", "商品", "大盘", "市场", "天猫", "抖音", "京东")
    ):
        return None
    residue = text.replace(period, " ")
    residue = re.sub(
        r"(?:那|那么|就|改成|改为|调整到|调整为|到|看|分析|按|也|可以|行|好的|好|的|吧|呢|请|麻烦|[\s，,。！？!?])+",
        "",
        residue,
    )
    if residue:
        return None
    bet = state.bet_context
    last_view = str(state.drilldown_ctx.last_analysis_view or "")
    if not bet.brand or last_view not in {"media_analysis", "bet_followup"}:
        return None
    filters = bet.filters or {}
    return RouteResult(
        type="media_analysis",
        brand=bet.brand,
        period=period,
        brand_aliases=list(bet.brand_aliases or []),
        media_scope="full_bet",
        media_mode=filters.get("media_mode") or "OVERALL_BET",
        media_channels=list(filters.get("media_channels") or []),
        question_mode="report",
    )


def _route_market(text: str, state: SessionState, intent: IntentResult | None = None) -> RouteResult:
    if os.environ.get("MARKET_ANALYSIS_ENABLED", "1") != "1":
        return RouteResult(type="guide")
    if intent and intent.segment and str(intent.segment).upper() not in {"BEAUTY MARKET", "PURE MASS", "SELECTIVE", "PROFESSIONAL"}:
        return RouteResult(type="market_parameter_error", message="目前Segment仅支持Beauty Market、Pure Mass、Selective和Professional。")
    if intent and intent.platform:
        try:
            canonical_platform(intent.platform)
        except ValueError:
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
        original_text=text, period=plan.period, segment=plan.segment,
        category=plan.category, platform=plan.platform,
        market_view=plan.view, ranking_metric=plan.ranking_metric,
        ranking_limit=plan.ranking_limit,
        include_bet=(
            plan.intent == "market_brand_deep_dive"
            and bool(re.search(r"bet|媒体投资|媒体花费|投放", text, re.I))
        ),
        bet_latest_ytd=bool(re.search(r"今年|最新|ytd", text, re.I)),
    )


def _ordinal_brand_route(text: str, state: SessionState) -> RouteResult | None:
    match = re.search(r"第([一二三四五1-5])名", text)
    top_brands = list(state.active_plan.top_brands or state.market_context.top_brands or [])
    if not match or not top_brands:
        return None
    index_map = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}
    rank = index_map.get(match.group(1), int(match.group(1)) if match.group(1).isdigit() else 0)
    if not 1 <= rank <= len(top_brands):
        return None
    brand = top_brands[rank - 1]
    period = state.active_plan.entities.get("period") or state.market_context.period
    if is_media_question(text):
        return RouteResult(type="media_analysis", brand=brand, period=period, media_scope="full_bet")
    if "三平台" in text or "全平台" in text:
        return RouteResult(
            type="three_platform_competitor_analysis", brand=brand,
            period=period, brand_aliases=[brand], platform="TTL",
        )
    selected = next(
        (row for row in state.active_plan.selected_platforms if row.get("brand") == brand), None,
    )
    if selected and re.search(r"增长.*平台|最好.*平台|选中平台|该平台|那个平台", text):
        route_type = {
            "TM": "default_chain", "DY": "douyin_business_analysis", "JD": "jd_business_analysis",
        }.get(str(selected.get("platform") or "").upper())
        if route_type:
            return RouteResult(
                type=route_type, brand=brand, period=period,
                brand_aliases=[brand], platform=selected.get("platform"),
            )
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
            model=llm_model("router"),
            messages=[
                {
                    "role": "system",
                    "content": "你是严格的业务问句参数解析器，只返回合法JSON对象。",
                },
                {"role": "user", "content": prompt},
            ],
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
    if is_data_availability_question(text):
        return RouteResult(type="data_availability", response_strategy="META_ANSWER")
    if is_meta_question(text):
        return RouteResult(type="meta")
    ordinal = _ordinal_brand_route(text, state)
    if ordinal:
        return ordinal
    if is_business_investment_question(text):
        return _route_business_investment(text, state)
    if is_three_platform_competitor_question(text):
        return _route_three_platform_competitor(text, state)
    if is_jd_business_question(text):
        return _route_jd_business(text, state)
    if is_douyin_business_question(text):
        return _route_douyin_business(text, state)
    if is_tmall_business_question(text):
        return _route_tmall_business(text, state)
    if is_market_question(text):
        return _route_market(text, state)
    if re.match(r"^\s*追问\s*[:：,，]", text):
        followup = _strip_followup_prefix(text)
        if is_data_caliber_question(followup):
            return RouteResult(type="caliber_reject")
        ctx, media = _followup_context(followup, state)
        explicit_brand = _detect_followup_brand(followup)
        brand = explicit_brand or ctx.brand or state.drilldown_ctx.brand
        period = (
            (extract_media_period(followup) if media else extract_period(followup))
            or ctx.period
            or state.drilldown_ctx.period
        )
        aliases = [explicit_brand] if explicit_brand else list(
            ctx.brand_aliases or state.drilldown_ctx.brand_aliases
        )
        return RouteResult(
            type="skill_dispatch",
            brand=brand,
            period=period,
            brand_aliases=aliases,
            followup_text=followup,
        )
    if os.environ.get("FOLLOWUP_SKILL_V2_ENABLED", "1") == "1" and is_narrow_followup(text):
        ctx, media = _followup_context(text, state)
        brand = (
            _detect_followup_brand(text)
            or (_detect_media_brand(text) if media else _sanitize_routed_brand(detect_brand_hint(text)))
            or ctx.brand
            or state.drilldown_ctx.brand
        )
        period = (extract_media_period(text) if media else extract_period(text)) or ctx.period or state.drilldown_ctx.period
        return RouteResult(type="skill_dispatch", brand=brand, period=period, followup_text=text)
    if is_media_question(text):
        return _route_media(text, state)
    brand = _sanitize_routed_brand(detect_brand_hint(text))
    if brand:
        period = extract_period(text)
        return RouteResult(type="default_chain" if period else "clarify_period", brand=brand, period=period)
    return RouteResult(type="guide")


def _route_legacy(user_text: str, state: SessionState) -> RouteResult:
    text = user_text.strip()
    if is_data_availability_question(text):
        return RouteResult(type="data_availability", response_strategy="META_ANSWER")
    if is_meta_question(text):
        return RouteResult(type="meta", response_strategy="META_ANSWER")
    collection_followup = _market_collection_followup(text, state)
    if collection_followup:
        return collection_followup
    # Resolve a structured clarification before Router V2 can interpret the
    # short answer as a new request and erase already validated entities.
    task_followup = (
        _resume_active_task_context(text, state)
        if os.environ.get("TASK_CONTEXT_V2_ENABLED", "1") == "1" else None
    )
    if task_followup:
        return task_followup
    opportunity_followup = _opportunity_followup_with_context(text, state)
    if opportunity_followup:
        return opportunity_followup
    targeted = _unified_targeted_route(text, state)
    if targeted:
        return targeted
    # Rollout starts dark/shadow. Production enables V2 explicitly after the
    # comparison window; keeping the default off preserves the V1 rollback.
    v2_enabled = os.environ.get("ROUTER_V2_ENABLED", "0") == "1"
    v2_shadow = os.environ.get("ROUTER_V2_SHADOW", "0") == "1"
    entity_enabled = os.environ.get("ENTITY_RESOLVER_V2_ENABLED", "0") == "1"
    entity_shadow = os.environ.get("ENTITY_RESOLVER_V2_SHADOW", "0") == "1"
    ordinal_market_reply = bool(
        state.market_context.top_brands
        and re.search(r"第\s*[一二三四五六七八九十\d]+\s*名", text)
    )
    has_analysis_context = bool(
        state.ec_context.brand
        or state.bet_context.brand
        or state.drilldown_ctx.brand
    )
    contextual_narrow_followup = bool(
        has_analysis_context
        and is_narrow_followup(text)
        and not is_market_question(text)
    )
    v2_eligible = (
        not re.match(r"^\s*追问\s*[:：,，]", text)
        and not ordinal_market_reply
        and not contextual_narrow_followup
    )
    entity_result = None
    if (entity_enabled or entity_shadow) and v2_eligible:
        entity_result = resolve_entities(text, current_year=date.today().year)
        if entity_shadow:
            shadow_entity_decision = _decision_from_entities(text, entity_result)
            log.info(
                "[entity_resolver_v2_shadow] legacy_brand=%s legacy_period=%s "
                "entity_brand=%s entity_period=%s entity_action=%s entity=%s",
                detect_brand_hint(text), extract_period(text) or extract_media_period(text),
                entity_result.brand.surface,
                str(entity_result.time_scope.focus_period) if entity_result.time_scope.focus_period else None,
                shadow_entity_decision.action if shadow_entity_decision else None,
                entity_result.to_dict(),
            )
    if (v2_enabled or v2_shadow) and v2_eligible:
        if entity_enabled and entity_result:
            v2_decision = _decision_from_entities(text, entity_result)
        else:
            v2_decision = build_route_decision(text, extract_period(text) or extract_media_period(text))
        if v2_decision:
            v2_decision = _inherit_v2_context(v2_decision, text, state)
        if v2_shadow and not v2_enabled and v2_decision:
            log.info("[router_v2_shadow] decision=%s", v2_decision.to_dict())
        if not v2_enabled:
            v2_decision = None
    if v2_enabled and v2_eligible:
        # A full new request wins over pending context. Terse scope answers are
        # allowed to resume only the matching V2 clarification.
        pending_scope_reply = bool(
            state.pending_request
            and state.pending_request.get("intent") == "v2_media_scope"
            and len(text) <= 16
            and re.search(r"整体|全盘|只看抖音|仅看抖音|抖音相关|overall", text, re.I)
        )
        if (
            v2_decision
            and (v2_decision.brand_surface or "MARKET" in v2_decision.intents)
            and not pending_scope_reply
        ):
            return _adapt_v2_decision(v2_decision)
        resumed_entity = _resume_entity_pending(text, state) if entity_enabled else None
        if resumed_entity:
            return resumed_entity
        resumed_v2 = _resume_v2_scope(text, state)
        if resumed_v2:
            return resumed_v2
        resumed_slots = _resume_v2_slots(text, state)
        if resumed_slots:
            return resumed_slots
        if v2_decision:
            return _adapt_v2_decision(v2_decision)
    period_only_resume = _resume_period_only_domain(text, state)
    if period_only_resume:
        return period_only_resume
    # BET仍是独立报告入口，但品牌、时间和中英文名优先由统一语义路由抽取。
    is_explicit_media = is_media_question(text)
    pending_period = extract_period(text)
    if is_three_platform_competitor_question(text):
        return _route_three_platform_competitor(text, state)
    # A complete request with an explicit commerce platform must override stale
    # clarification state. Otherwise an earlier pending Tmall/default request
    # can silently redirect a new Douyin or JD question to the wrong chain.
    if is_jd_business_question(text):
        return _route_jd_business(text, state)
    if is_douyin_business_question(text):
        return _route_douyin_business(text, state)
    if is_tmall_business_question(text):
        return _route_tmall_business(text, state)
    if state.pending_request and state.pending_request.get("intent") == "three_platform_competitor_analysis":
        pending = state.pending_request
        supplied_brand = _detect_three_platform_brand(text) if not pending.get("brand") else pending.get("brand")
        supplied_period = pending_period or pending.get("period")
        if supplied_brand and supplied_period:
            return RouteResult(
                type="three_platform_competitor_analysis",
                brand=supplied_brand,
                period=supplied_period,
                brand_aliases=[supplied_brand],
                platform="TTL",
            )
        return RouteResult(
            type="clarify_three_platform_brand" if not supplied_brand else "clarify_three_platform_period",
            brand=supplied_brand,
            period=supplied_period,
            brand_aliases=[supplied_brand] if supplied_brand else [],
            platform="TTL",
        )
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
            supplied_brand = _sanitize_routed_brand(detect_brand_hint(text))
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
    if re.match(r"^\s*追问\s*[:：,，]", text):
        followup = _strip_followup_prefix(text)
        ordinal = _ordinal_brand_route(followup, state)
        if ordinal:
            return ordinal
        if is_market_question(followup):
            return _route_market(followup, state)
        if is_data_caliber_question(followup):
            return RouteResult(type="caliber_reject")
        ctx, media = _followup_context(followup, state)
        if os.environ.get("FOLLOWUP_SKILL_V2_ENABLED", "1") != "1":
            update = try_filter_update(followup, state)
            if update.matched:
                return RouteResult(type="filter_update", followup_text=followup, update=update)
        explicit_brand = _detect_followup_brand(followup)
        brand = explicit_brand or ctx.brand or state.drilldown_ctx.brand
        period = (
            (extract_media_period(followup) if media else extract_period(followup))
            or ctx.period
            or state.drilldown_ctx.period
        )
        aliases = [explicit_brand] if explicit_brand else list(
            ctx.brand_aliases or state.drilldown_ctx.brand_aliases
        )
        return RouteResult(
            type="skill_dispatch", brand=brand, period=period,
            brand_aliases=aliases, followup_text=followup,
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

    if is_unspecified_platform_business_question(text):
        return _route_unspecified_platform_business(text, state)

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
        ctx, media = _followup_context(followup, state)
        explicit_brand = _detect_followup_brand(followup)
        brand = explicit_brand or ctx.brand or state.drilldown_ctx.brand
        period = (
            (extract_media_period(followup) if media else extract_period(followup))
            or ctx.period
            or state.drilldown_ctx.period
        )
        aliases = [explicit_brand] if explicit_brand else list(
            ctx.brand_aliases or state.drilldown_ctx.brand_aliases
        )
        return RouteResult(
            type="skill_dispatch", brand=brand, period=period,
            brand_aliases=aliases, followup_text=followup,
        )

    if intent.intent == "default_analysis":
        brand = _clean_brand_candidate(intent.brand, text)
        if not brand:
            return RouteResult(type="guide")
        period = intent.period or extract_period(text)
        return RouteResult(
            type="clarify_ec_platform",
            brand=brand,
            period=period,
            brand_aliases=_intent_brand_aliases(intent, text, brand),
        )

    return _route_by_rules(user_text, state)


def _llm_route_error(message: str) -> RouteResult:
    return RouteResult(
        type="unsupported_scope",
        message="我暂时无法可靠理解这条请求，已保留当前会话范围。请稍后重试。",
        route_decision={
            "decision_source": "llm_router_error",
            "reason_codes": ["LLM_ROUTER_UNAVAILABLE"],
            "error": message,
            "intents": [],
        },
        response_strategy="CLARIFY",
    )


def _adapt_llm_routing(envelope, user_text: str) -> RouteResult:
    """Translate the new semantic contract to existing execution adapters."""
    from bot.entity_resolution import ResolvedPeriod

    payload = envelope.to_dict()
    if envelope.atomic_request is not None:
        return RouteResult(type="atomic_lookup", original_text=user_text,
            route_decision=payload, response_strategy="TARGETED_ANSWER")
    focus = envelope.focus_period
    comparison = envelope.comparison_periods[0] if envelope.comparison_periods else None
    period = None
    if focus:
        period = ResolvedPeriod(
            focus.raw,
            start_date=focus.start_date,
            end_date=focus.end_date,
            comparison_start=comparison.start_date if comparison else None,
            comparison_end=comparison.end_date if comparison else None,
        )
    platform_values = set(envelope.platforms)
    platform = (
        "TTL" if "MARKET" in set(envelope.intents) and (
            "TTL" in platform_values or {"TM", "DY", "JD"}.issubset(platform_values)
        )
        else envelope.platforms[0] if envelope.platforms else None
    )
    brand = envelope.brand_surface
    resolution = envelope.brand_resolution or {}
    if resolution.get("status") == "resolved":
        brand = resolution.get("surface") or brand

    slot_ops = dict(envelope.slot_ops)
    canonical_slots = {
        "original_question": user_text,
        "brand": brand,
        "brand_aliases": list(envelope.brand_aliases),
        "period": period,
        "platform": platform,
        "dimensions": list(envelope.dimensions),
        "media_mode": envelope.media_mode,
        "media_channels": list(envelope.media_channels),
        "segment": envelope.segment,
        "category": envelope.category,
        "comparison_metric": envelope.ranking_metric,
        "awaiting_slot": None,
        "status": "cancelled" if envelope.relation == "CANCEL" else "active",
    }
    for name, value in canonical_slots.items():
        requested_op = str((slot_ops.get(name) or {}).get("op") or "").upper()
        if envelope.relation == "NEW_TASK" and value is not None:
            requested_op = "SET"
        if requested_op not in {"SET", "KEEP", "CLEAR"}:
            requested_op = "SET" if value is not None else "KEEP"
        slot_ops[name] = {
            "op": requested_op,
            # Never allow a free-form slot_ops value to override the separately
            # validated canonical envelope.
            "value": None if requested_op == "CLEAR" else value,
            "source": "llm_router_validated",
        }
    decision = {
        **payload,
        "decision_source": "llm_router_v1",
        "reason_codes": ["LLM_FIRST_ROUTER", f"TURN_RELATION_{envelope.relation}"],
        "task_context_patch": {
            "relation": envelope.relation,
            "slot_ops": slot_ops,
            "intents": list(envelope.intents),
            "goals": list(envelope.goals),
            "reason_codes": ["LLM_FIRST_ROUTER"],
        },
        "entity_resolution": {
            "text": user_text,
            "brand": resolution,
            "time_scope": {
                "focus_period": focus.raw if focus else None,
                "focus_range": focus.to_dict() if focus else None,
                "comparison_periods": [item.to_dict() for item in envelope.comparison_periods],
                "missing_slots": list(envelope.missing_slots),
            },
        },
    }
    common = dict(
        original_text=user_text,
        brand=brand,
        period=period,
        brand_aliases=list(envelope.brand_aliases),
        platform=platform,
        media_mode=envelope.media_mode,
        media_channels=list(envelope.media_channels),
        question_mode=envelope.question_mode,
        segment=envelope.segment,
        category=envelope.category,
        ranking_metric=envelope.ranking_metric,
        ranking_limit=envelope.ranking_limit,
        route_decision=decision,
        time_scope=(decision["entity_resolution"]["time_scope"]),
        brand_resolution=resolution,
        response_strategy=envelope.response_strategy,
    )

    if envelope.relation == "CANCEL":
        return RouteResult(
            type="unsupported_scope", message="已取消当前分析请求。",
            route_decision=decision, response_strategy="META_ANSWER",
        )
    intents = set(envelope.intents)
    if intents == {"META"}:
        return RouteResult(type="meta", original_text=user_text, route_decision=decision)
    if intents == {"DATA_AVAILABILITY"}:
        return RouteResult(type="data_availability", original_text=user_text, route_decision=decision)

    if envelope.unsupported_requests:
        unsupported_text = "、".join(envelope.unsupported_requests)
        return RouteResult(
            type="unsupported_scope",
            message=envelope.clarification_question or (
                f"当前能力不支持这个分析组合（{unsupported_text}），"
                "请缩小到已支持的平台或维度。"
            ),
            **common,
        )

    missing = set(envelope.missing_slots)
    if "brand" in missing:
        candidates = resolution.get("candidates") or []
        if candidates:
            choices = "；".join(
                f"{index}. {item.get('canonical_display_name')}"
                for index, item in enumerate(candidates[:3], 1)
            )
            return RouteResult(
                type="confirm_brand_candidate",
                message=envelope.clarification_question or f"品牌存在多个可能匹配，请选择：{choices}。",
                **common,
            )
        return RouteResult(
            type="clarify_v2_brand",
            message=envelope.clarification_question or "请提供需要分析的品牌名称。",
            **common,
        )
    if {"period", "comparison_period"} & missing:
        return RouteResult(
            type="clarify_v2_period",
            message=envelope.clarification_question or "请提供主分析期；如需自定义对比，也请同时给出对比期。",
            **common,
        )
    if "MARKET" in intents and missing:
        return RouteResult(
            type="clarify_market_scope",
            message=envelope.clarification_question or "请补充完整的市场分析范围。",
            **common,
        )
    if "platform" in missing:
        return RouteResult(
            type="clarify_ec_platform",
            message=envelope.clarification_question or "请确认要分析天猫、抖音、京东还是三平台。",
            **common,
        )
    if "BET" in intents and not envelope.media_mode:
        return RouteResult(
            type="clarify_media_scope",
            message=envelope.clarification_question or "请确认看整体BET，还是指定媒体渠道。",
            **common,
        )

    if "FOLLOWUP" in intents:
        return RouteResult(
            type="skill_dispatch", followup_text=user_text, **common,
        )
    if "MARKET" in intents:
        route_type = (
            "market_brand_deep_dive"
            if envelope.response_strategy == "MULTI_STEP_ANALYSIS" else
            "market_brand_ranking"
            if envelope.question_mode == "ranking" or envelope.ranking_metric else
            "market_analysis"
        )
        return RouteResult(
            type=route_type, market_view="top_brands" if "ranking" in envelope.dimensions else "summary",
            include_bet="BET" in intents, media_scope="full_bet" if "BET" in intents else None,
            **common,
        )
    if intents >= {"EC_BUSINESS", "BET"}:
        return RouteResult(
            type="brand_business_investment_analysis",
            media_scope="full_bet" if envelope.media_mode == "OVERALL_BET" else "channel_only",
            **common,
        )
    if "BET" in intents:
        return RouteResult(
            type="media_analysis",
            media_scope="full_bet" if envelope.media_mode == "OVERALL_BET" else "channel_only",
            **common,
        )
    if "EC_BUSINESS" in intents:
        route_type = {
            "TM": "default_chain", "DY": "douyin_business_analysis",
            "JD": "jd_business_analysis", "TTL": "three_platform_competitor_analysis",
        }.get(str(platform or "").upper())
        if not route_type:
            return RouteResult(
                type="clarify_ec_platform",
                message=envelope.clarification_question or "请确认要分析天猫、抖音、京东还是三平台。",
                **common,
            )
        if envelope.question_mode in {"strategy", "explanation"} or any(
            item in {"FIND_OPPORTUNITIES", "RECOMMEND_ACTIONS"} for item in envelope.goals
        ):
            route_type = "opportunity_analysis"
        return RouteResult(type=route_type, **common)
    return _llm_route_error("no executable intent")


def route(user_text: str, state: SessionState, *, received_at=None) -> RouteResult:
    """Production entrypoint: LLM first; legacy only via manual emergency flag."""
    from bot.llm_router import RoutingEnvelopeError, llm_router_enabled, route_with_llm

    if not llm_router_enabled():
        return _route_legacy(user_text, state)
    try:
        return _adapt_llm_routing(route_with_llm(user_text, state, received_at=received_at), user_text)
    except RoutingEnvelopeError as exc:
        log.error("[llm_router] routing unavailable: %s", exc)
        return _llm_route_error(str(exc))
