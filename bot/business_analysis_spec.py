from __future__ import annotations

from dataclasses import asdict, dataclass, field


COMMERCE_PLATFORMS = ("TM", "DY", "JD")
BUSINESS_CATEGORIES = (
    "TOTAL BEAUTY", "FEMALE SKINCARE", "MAKEUP", "HAIR", "MALE SKINCARE",
)


def normalize_business_category(category: str | None) -> str:
    value = str(category or "TOTAL BEAUTY").strip().upper()
    normalized = {
        "TTL BEAUTY": "TOTAL BEAUTY",
        "TOTAL BEAUTY": "TOTAL BEAUTY",
        "SKINCARE": "FEMALE SKINCARE",
        "FEMALE SKINCARE": "FEMALE SKINCARE",
        "MAKEUP": "MAKEUP",
        "HAIR": "HAIR",
        "HAIRCARE": "HAIR",
        "MALE SKINCARE": "MALE SKINCARE",
        "MEX": "MALE SKINCARE",
    }.get(value)
    if normalized not in BUSINESS_CATEGORIES:
        raise ValueError("不支持的Category参数。")
    return normalized


@dataclass(frozen=True)
class BusinessAnalysisSpec:
    """Platform-independent semantic contract for a business analysis request."""

    subject_scope: str
    brand: str | None
    platforms: list[str] = field(default_factory=list)
    platform_mode: str = "single"
    segment: str | None = None
    category: str = "TOTAL BEAUTY"
    time_scope: dict = field(default_factory=dict)
    comparison_spec: dict = field(default_factory=dict)
    analysis_mode: str = "report"

    def to_dict(self) -> dict:
        return asdict(self)


def compile_business_analysis_spec(decision) -> BusinessAnalysisSpec | None:
    intents = list(getattr(decision, "intents", None) or [])
    if not any(intent in intents for intent in ("EC_BUSINESS", "MARKET")):
        return None
    platform = ((getattr(decision, "commerce_scope", None).platforms or [None])[0])
    if platform not in {*COMMERCE_PLATFORMS, "TTL"}:
        return None
    subject_scope = "market" if "MARKET" in intents else "brand"
    market_scope = getattr(decision, "market_scope", None)
    return build_business_analysis_spec(
        subject_scope=subject_scope,
        brand=None if subject_scope == "market" else getattr(decision, "brand_surface", None),
        platform=platform,
        segment=(getattr(market_scope, "segment", None) or "PURE MASS") if subject_scope == "market" else None,
        category=getattr(market_scope, "category", None) or "TOTAL BEAUTY",
        time_scope=dict((getattr(decision, "entity_resolution", None) or {}).get("time_scope") or {}),
        comparison_spec=dict(getattr(decision, "comparison_spec", None) or {}),
        analysis_mode=getattr(decision, "question_mode", None) or "report",
    )


def build_business_analysis_spec(
    *, subject_scope: str, brand: str | None, platform: str,
    segment: str | None = None, category: str | None = None,
    time_scope: dict | None = None, comparison_spec: dict | None = None,
    analysis_mode: str = "report",
) -> BusinessAnalysisSpec:
    """Build the same contract after an incremental task-slot merge."""
    platforms = list(COMMERCE_PLATFORMS) if platform == "TTL" else [platform]
    return BusinessAnalysisSpec(
        subject_scope=subject_scope,
        brand=None if subject_scope == "market" else brand,
        platforms=platforms,
        platform_mode="combined" if platform == "TTL" else "single",
        segment=(segment or "PURE MASS") if subject_scope == "market" else None,
        category=category or "TOTAL BEAUTY",
        time_scope=dict(time_scope or {}),
        comparison_spec=dict(comparison_spec or {}),
        analysis_mode=analysis_mode or "report",
    )
