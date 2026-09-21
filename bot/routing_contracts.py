from __future__ import annotations

from dataclasses import asdict, dataclass, field
from uuid import uuid4


RESPONSE_STRATEGIES = {
    "FULL_REPORT",
    "TARGETED_ANSWER",
    "MULTI_STEP_ANALYSIS",
    "META_ANSWER",
    "CLARIFY",
}


def infer_response_strategy(
    *, action: str | None, question_mode: str | None = None,
    reason_codes: list[str] | tuple[str, ...] = (), user_text: str = "",
) -> str:
    """Map legacy routes to the user-agnostic execution strategy contract."""
    route = str(action or "")
    if route in {"meta", "guide", "caliber_reject", "data_availability"}:
        return "META_ANSWER"
    if route.startswith("clarify_") or route in {
        "confirm_current_year", "provide_campaign_window", "confirm_brand_candidate",
        "market_parameter_error", "unsupported_scope",
    }:
        return "CLARIFY"
    if route in {
        "market_brand_deep_dive", "brand_platform_deep_dive",
        "brand_business_investment_analysis", "opportunity_analysis",
    } or "COMPLEX_MARKET_PLAN" in set(reason_codes):
        return "MULTI_STEP_ANALYSIS"
    if route in {"skill_dispatch", "filter_update"}:
        return "TARGETED_ANSWER"
    if question_mode in {"ranking", "comparison"} and any(
        token in str(user_text or "") for token in ("再", "然后", "下钻", "分别", "先")
    ):
        return "MULTI_STEP_ANALYSIS"
    return "FULL_REPORT"


@dataclass(frozen=True)
class CommerceScope:
    platforms: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MediaScope:
    mode: str | None = None
    channels: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MarketScope:
    segment: str | None = None
    category: str | None = None


@dataclass
class RouteDecision:
    intents: list[str]
    question_mode: str
    action: str
    brand_surface: str | None = None
    period: str | None = None
    commerce_scope: CommerceScope = field(default_factory=CommerceScope)
    media_scope: MediaScope = field(default_factory=MediaScope)
    market_scope: MarketScope = field(default_factory=MarketScope)
    metrics: list[str] = field(default_factory=list)
    dimensions: list[str] = field(default_factory=list)
    ranking_metric: str | None = None
    ranking_limit: int | None = None
    comparison: str | None = None
    missing_slots: list[str] = field(default_factory=list)
    unsupported_requests: list[str] = field(default_factory=list)
    requires_confirmation: bool = False
    confidence_level: str = "exact"
    decision_source: str = "deterministic"
    reason_codes: list[str] = field(default_factory=list)
    inherited_parameters: list[str] = field(default_factory=list)
    entity_resolution: dict | None = None
    task_bindings: list[dict] = field(default_factory=list)
    route_id: str = field(default_factory=lambda: uuid4().hex)
    response_strategy: str | None = None

    def __post_init__(self) -> None:
        if not self.response_strategy:
            self.response_strategy = infer_response_strategy(
                action=self.action,
                question_mode=self.question_mode,
                reason_codes=self.reason_codes,
            )
        if self.response_strategy not in RESPONSE_STRATEGIES:
            raise ValueError(f"unknown response strategy: {self.response_strategy}")

    def to_dict(self) -> dict:
        data = asdict(self)
        explicit = []
        if self.brand_surface:
            explicit.append("brand_surface")
        if self.period:
            explicit.append("period")
        if self.commerce_scope.platforms:
            explicit.append("commerce_scope.platforms")
        if self.media_scope.mode:
            explicit.append("media_scope.mode")
        if self.media_scope.channels:
            explicit.append("media_scope.channels")
        if self.market_scope.segment:
            explicit.append("market_scope.segment")
        if self.market_scope.category:
            explicit.append("market_scope.category")
        explicit = [slot for slot in explicit if slot not in self.inherited_parameters]
        data["trace"] = {
            "router_version": "v2",
            "candidate_intents": list(self.intents),
            "explicit_parameters": explicit,
            "inherited_parameters": list(self.inherited_parameters),
            "missing_parameters": list(self.missing_slots),
            "matched_rules": list(self.reason_codes),
            "final_action": self.action,
            "response_strategy": self.response_strategy,
            "entity_resolution": self.entity_resolution,
            "task_bindings": list(self.task_bindings),
        }
        return data


@dataclass(frozen=True)
class CapabilitySpec:
    name: str
    intents: tuple[str, ...]
    question_modes: tuple[str, ...]
    required_slots: tuple[str, ...]
    optional_slots: tuple[str, ...]
    commerce_platforms: tuple[str, ...]
    media_modes: tuple[str, ...]
    media_channels: tuple[str, ...]
    segments: tuple[str, ...]
    categories: tuple[str, ...]
    chain: str
    requires_confirmation: bool = False
    unsupported: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    output_schema: str | None = None
    question_patterns: tuple[str, ...] = ()
    supports_full_report: bool = False
    supports_targeted_answer: bool = False
    requires_data_query: bool = True
    enabled_flag: str | None = None
    limitations: tuple[str, ...] = ()
    example_questions: tuple[str, ...] = ()

    @property
    def platforms(self) -> tuple[str, ...]:
        return self.commerce_platforms or self.media_channels

    @property
    def output_contract(self) -> str | None:
        return self.output_schema


ALL_MODES = ("lookup", "report", "ranking", "explanation", "strategy", "comparison")
ALL_SEGMENTS = ("BEAUTY MARKET", "PURE MASS", "SELECTIVE", "PROFESSIONAL")
ALL_CATEGORIES = (
    "TOTAL BEAUTY", "FEMALE SKINCARE", "MAKEUP", "HAIR", "MALE SKINCARE",
)


CAPABILITY_REGISTRY: dict[str, CapabilitySpec] = {
    "tm_brand_business": CapabilitySpec(
        "tm_brand_business", ("EC_BUSINESS",), ALL_MODES,
        ("brand_surface", "period", "commerce_scope.platforms"), (),
        ("TM",), (), (), (), (), "default_chain",
        dimensions=("overall", "category", "key_driver", "series", "sku", "product_title"),
        output_schema="ec-report-evidence.v2",
        question_patterns=("天猫生意", "品牌整体表现", "品类/系列/商品下钻"),
        supports_full_report=True, supports_targeted_answer=True,
        limitations=("只基于内部可观测数据，不推断公司动作或因果",),
        example_questions=("珀莱雅2026年6月天猫生意怎么样？", "防晒品类有哪些Top系列？"),
    ),
    "dy_brand_business": CapabilitySpec(
        "dy_brand_business", ("EC_BUSINESS",), ALL_MODES,
        ("brand_surface", "period", "commerce_scope.platforms"), (),
        ("DY",), (), (), (), (), "douyin_business_analysis",
        dimensions=("overall", "category", "key_driver", "series", "sku", "product_title", "product_link"),
        output_schema="ec-report-evidence.v2",
        question_patterns=("抖音生意", "品类", "Key Driver", "商品标题和链接"),
        supports_full_report=True, supports_targeted_answer=True,
        limitations=("商品日表模块受数据质量闸门和功能开关控制",),
        example_questions=("韩束2026年6月抖音生意怎么样？", "防晒品类的主要Driver和Top商品是什么？"),
    ),
    "jd_brand_business": CapabilitySpec(
        "jd_brand_business", ("EC_BUSINESS",), ALL_MODES,
        ("brand_surface", "period", "commerce_scope.platforms"), (),
        ("JD",), (), (), (), (), "jd_business_analysis",
        unsupported=("key_driver", "series", "sku", "product_title", "product_link"),
        dimensions=("overall", "category"), output_schema="ec-report-evidence.v2",
        question_patterns=("京东生意", "京东品类"),
        supports_full_report=True, supports_targeted_answer=True,
        limitations=("暂不支持京东Key Driver、系列、SKU和商品链接下钻",),
        example_questions=("珀莱雅2026年6月京东生意怎么样？", "京东护肤品类表现如何？"),
    ),
    "ttl_brand_business": CapabilitySpec(
        "ttl_brand_business", ("EC_BUSINESS",), ALL_MODES,
        ("brand_surface", "period", "commerce_scope.platforms"), (),
        ("TTL",), (), (), (), (), "three_platform_competitor_analysis",
        unsupported=("key_driver", "series", "sku", "product_title", "product_link"),
        dimensions=("overall", "platform"), output_schema="ec-report-evidence.v2",
        question_patterns=("三平台生意", "平台比较"),
        supports_full_report=True, supports_targeted_answer=True,
        limitations=("三平台汇总不能直接替代单平台商品下钻",),
        example_questions=("珀莱雅2026年6月三平台生意怎么样？", "哪个平台增长额最大？"),
    ),
    "overall_bet": CapabilitySpec(
        "overall_bet", ("BET",), ALL_MODES,
        ("brand_surface", "period", "media_scope.mode"), ("media_scope.channels",),
        (), ("OVERALL_BET",), (), (), (), "media_analysis",
        unsupported=("causal_attribution", "company_action_inference"),
        question_patterns=("BET", "媒体投资", "费比", "BKFS"),
        supports_full_report=True, supports_targeted_answer=True,
        limitations=("只能描述投资与生意变化关系，不能做因果归因",),
        example_questions=("珀莱雅2026年1—6月BET怎么样？", "按月看媒体费比变化。"),
    ),
    "channel_bet": CapabilitySpec(
        "channel_bet", ("BET",), ALL_MODES,
        ("brand_surface", "period", "media_scope.mode", "media_scope.channels"), (),
        (), ("CHANNEL_ONLY",), ("DOUYIN", "RED"), (), (), "media_analysis",
        unsupported=("causal_attribution", "company_action_inference"),
        question_patterns=("抖音媒体投放", "小红书媒体投放"),
        supports_full_report=True, supports_targeted_answer=True,
        limitations=("媒体渠道与同名电商平台严格分开",),
        example_questions=("只看抖音相关BET。", "小红书媒体花费结构怎么样？"),
    ),
    "market": CapabilitySpec(
        "market", ("MARKET",), ALL_MODES,
        ("period", "commerce_scope.platforms", "market_scope.segment", "market_scope.category"), (),
        ("TM", "DY", "JD", "TTL"), (), (), ALL_SEGMENTS, ALL_CATEGORIES,
        "market_analysis",
        question_patterns=("市场大盘", "Top品牌", "市场趋势"),
        supports_full_report=True, supports_targeted_answer=True,
        limitations=("需要明确平台、Segment、Category和时间",),
        example_questions=("2026年Q2三平台Pure Mass TTL Beauty Top3品牌是谁？",),
    ),
    "market_ec_composite": CapabilitySpec(
        "market_ec_composite", ("MARKET", "EC_BUSINESS"), ALL_MODES,
        ("period", "commerce_scope.platforms", "market_scope.segment", "market_scope.category"),
        (), ("TM", "DY", "JD", "TTL"), (), (),
        ALL_SEGMENTS, ALL_CATEGORIES, "market_brand_deep_dive",
        unsupported=("causal_attribution", "company_action_inference"),
        question_patterns=("Top品牌再做三平台生意分析",),
        supports_full_report=True, supports_targeted_answer=False,
        limitations=("最多自动处理10个品牌",),
        example_questions=("Pure Mass Top3品牌是谁，再看他们三平台生意。",),
    ),
    "market_ec_bet_composite": CapabilitySpec(
        "market_ec_bet_composite", ("MARKET", "EC_BUSINESS", "BET"), ALL_MODES,
        ("period", "commerce_scope.platforms", "market_scope.segment", "market_scope.category"),
        ("media_scope.mode",), ("TM", "DY", "JD", "TTL"), ("OVERALL_BET",), (),
        ALL_SEGMENTS, ALL_CATEGORIES, "market_brand_deep_dive",
        unsupported=("causal_attribution", "company_action_inference"),
        question_patterns=("Top品牌三平台生意分析后再看BET",),
        supports_full_report=True, supports_targeted_answer=False,
        limitations=("最多自动处理10个品牌",),
        example_questions=("Pure Mass Top3三平台下钻，再看最新BET。",),
    ),
    "market_bet_composite": CapabilitySpec(
        "market_bet_composite", ("MARKET", "BET"), ALL_MODES,
        ("period", "commerce_scope.platforms", "market_scope.segment", "market_scope.category"),
        ("media_scope.mode",), ("TTL",), ("OVERALL_BET",), (),
        ALL_SEGMENTS, ALL_CATEGORIES, "market_brand_deep_dive",
        requires_confirmation=True,
        unsupported=("causal_attribution", "company_action_inference"),
        question_patterns=("Top品牌再看BET", "大盘比较后下钻"),
        supports_full_report=False, supports_targeted_answer=False,
        limitations=("最多自动处理5个品牌",),
        example_questions=("找Pure Mass Top3，再比较平台并看BET。",),
    ),
    "ec_bet_composite": CapabilitySpec(
        "ec_bet_composite", ("EC_BUSINESS", "BET"), ALL_MODES,
        ("brand_surface", "period", "commerce_scope.platforms", "media_scope.mode"),
        ("media_scope.channels",), ("TM", "DY", "JD"),
        ("OVERALL_BET", "CHANNEL_ONLY"), ("DOUYIN", "RED"), (), (),
        "brand_business_investment_analysis", requires_confirmation=True,
        unsupported=("causal_attribution", "company_action_inference"),
        question_patterns=("品牌生意和BET联合分析",),
        supports_full_report=True, supports_targeted_answer=False,
        limitations=("整体BET以天猫EC为主要业务参考，但不做因果归因",),
        example_questions=("PROYA天猫生意和整体BET怎么样？",),
    ),
    "followup": CapabilitySpec(
        "followup", ("FOLLOWUP",), ALL_MODES, (),
        ("brand_surface", "period", "commerce_scope.platforms"),
        ("TM", "DY", "JD", "TTL"), (), (), (), (), "skill_dispatch",
        dimensions=("category", "key_driver", "series", "sku", "month", "platform"),
        output_schema="ec-followup-answer.v2",
        question_patterns=("这个品类", "继续下钻", "按月整理", "Top链接"),
        supports_full_report=False, supports_targeted_answer=True,
        enabled_flag="FOLLOWUP_SKILL_V2_ENABLED",
        limitations=("实际维度仍受当前平台Capability约束",),
        example_questions=("防晒霜增长不错，再分析这个品类。", "按月整理媒体费比。"),
    ),
    "opportunity_insight": CapabilitySpec(
        "opportunity_insight", ("EC_BUSINESS",), ("strategy", "explanation", "comparison"),
        ("brand_surface", "period", "commerce_scope.platforms"),
        ("category", "dimensions"), ("TM", "DY", "JD", "TTL"), (), (), (), (),
        "opportunity_analysis",
        dimensions=(
            "overall", "category", "key_driver", "series", "sku",
            "growth_opportunity", "recovery_opportunity", "risk",
            "action_hypothesis", "validation_plan",
        ),
        output_schema="opportunity-insight.v3",
        question_patterns=("增长机会点", "行动方案", "怎么提升", "下一步怎么做"),
        supports_full_report=True, supports_targeted_answer=True,
        enabled_flag="AGENT_REASONING_V3_ENABLED",
        limitations=(
            "行动是基于内部证据和已审核业务方法论的待验证假设",
            "不生成SQL、预算承诺或因果结论",
        ),
        example_questions=("分析欧莱雅6月天猫生意，并找出增长机会点和行动方案。",),
    ),
    "meta": CapabilitySpec(
        "meta", ("META",), ("lookup",), (), (),
        (), (), (), (), (), "meta",
        question_patterns=("你能做什么", "怎么用", "支持哪些", "有什么限制"),
        supports_full_report=False, supports_targeted_answer=False,
        requires_data_query=False,
        example_questions=("抖音可以分析什么？", "京东支持Key Driver吗？"),
    ),
}


def capability_for(decision: RouteDecision) -> CapabilitySpec | None:
    intents = tuple(decision.intents)
    if intents == ("EC_BUSINESS", "BET"):
        return CAPABILITY_REGISTRY["ec_bet_composite"]
    if intents == ("BET",):
        key = "channel_bet" if decision.media_scope.mode == "CHANNEL_ONLY" else "overall_bet"
        return CAPABILITY_REGISTRY[key]
    if intents == ("MARKET",):
        return CAPABILITY_REGISTRY["market"]
    if intents == ("MARKET", "BET"):
        return CAPABILITY_REGISTRY["market_bet_composite"]
    if intents == ("EC_BUSINESS",):
        platform = (decision.commerce_scope.platforms or [None])[0]
        return CAPABILITY_REGISTRY.get({
            "TM": "tm_brand_business", "DY": "dy_brand_business",
            "JD": "jd_brand_business", "TTL": "ttl_brand_business",
        }.get(platform, ""))
    if intents == ("FOLLOWUP",):
        return CAPABILITY_REGISTRY["followup"]
    if intents == ("META",):
        return CAPABILITY_REGISTRY["meta"]
    return None


def validate_route_decision(decision: RouteDecision) -> RouteDecision:
    """Deterministically reject scope combinations the selected chain cannot honor."""
    spec = capability_for(decision)
    if spec is None:
        return decision
    unsupported = list(decision.unsupported_requests)
    platforms = decision.commerce_scope.platforms
    if platforms and spec.commerce_platforms and any(p not in spec.commerce_platforms for p in platforms):
        unsupported.append("commerce_scope.platforms")
    if decision.media_scope.mode and spec.media_modes and decision.media_scope.mode not in spec.media_modes:
        unsupported.append("media_scope.mode")
    if decision.media_scope.channels and spec.media_channels and any(
        channel not in spec.media_channels for channel in decision.media_scope.channels
    ):
        unsupported.append("media_scope.channels")
    if decision.market_scope.segment and spec.segments and decision.market_scope.segment not in spec.segments:
        unsupported.append("market_scope.segment")
    if decision.market_scope.category and spec.categories and decision.market_scope.category not in spec.categories:
        unsupported.append("market_scope.category")
    decision.unsupported_requests = list(dict.fromkeys(unsupported))
    if any(item.endswith("scope.platforms") or item.endswith("scope.mode") or item.endswith("scope.channels")
           or item.endswith("scope.segment") or item.endswith("scope.category")
           for item in decision.unsupported_requests):
        decision.action = "unsupported_scope"
        decision.reason_codes.append("CAPABILITY_SCOPE_UNSUPPORTED")
    return decision
