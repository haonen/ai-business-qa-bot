from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


CANONICAL_PLATFORMS = ("TM", "DY", "JD")


def _key(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return re.sub(r"[\s_\-]+", "", text)


INPUT_ALIASES = {
    "TM": ("TM", "TMALL", "天猫"),
    "JD": ("JD", "JINGDONG", "京东"),
    "DY": ("DY", "DOUYIN", "抖音"),
    "TTL": ("TTL", "三平台", "全平台"),
    "RED": ("RED", "XHS", "XIAOHONGSHU", "小红书"),
}
_INPUT_LOOKUP = {
    _key(alias): canonical
    for canonical, aliases in INPUT_ALIASES.items()
    for alias in aliases
}


def canonical_platform(value: object, *, allow_ttl: bool = True, allow_red: bool = False) -> str:
    canonical = _INPUT_LOOKUP.get(_key(value), "")
    allowed = set(CANONICAL_PLATFORMS)
    if allow_ttl:
        allowed.add("TTL")
    if allow_red:
        allowed.add("RED")
    if canonical not in allowed:
        raise ValueError(f"不支持的平台：{value}")
    return canonical


@dataclass(frozen=True)
class PlatformFieldSpec:
    field: str | None
    values: dict[str, tuple[str, ...]]
    implicit: str | None = None


_EC_VALUES = {
    "TM": ("TM", "TMALL", "天猫"),
    "JD": ("JD", "JINGDONG", "京东"),
    "DY": ("DY", "DOUYIN", "抖音"),
}

# Per-table physical platform contract. Dedicated platform tables are implicit
# and deliberately receive no predicate for a non-existent platform column.
TABLE_PLATFORM_SPECS: dict[str, PlatformFieldSpec] = {
    "three_platform_store_rank_monthly": PlatformFieldSpec("platform", _EC_VALUES),
    "three_platforms_segmented_markets_monthly": PlatformFieldSpec("platform", _EC_VALUES),
    "three_platforms_segmented_markets_daily": PlatformFieldSpec("platform", _EC_VALUES),
    "top_brands_total_ec": PlatformFieldSpec("platform", {"TTL": ("TTL",)}),
    "ai_bot_media_ksi_performance": PlatformFieldSpec(
        "platform",
        {
            "DY": ("douyin", "dy", "抖音"),
            "RED": ("red", "xhs", "xiaohongshu", "小红书"),
        },
    ),
    "tmall_store_ranking_day_jiashicang": PlatformFieldSpec(None, {}, implicit="TM"),
    "ai_bot_tmall_product_link": PlatformFieldSpec(None, {}, implicit="TM"),
    "dy_store_ranking_BFSS_day_jiashicang": PlatformFieldSpec(None, {}, implicit="DY"),
    "ai_bot_dy_product_link": PlatformFieldSpec(None, {}, implicit="DY"),
    "jd_store_ranking_selfrun_day_jiashicang": PlatformFieldSpec(None, {}, implicit="JD"),
}


def platform_values(table: str, platform: object) -> tuple[str, ...]:
    spec = TABLE_PLATFORM_SPECS[table]
    canonical = canonical_platform(platform, allow_red=True)
    if spec.implicit:
        if canonical != spec.implicit:
            raise ValueError(f"{table}是{spec.implicit}专属表，不支持{canonical}")
        return ()
    values = spec.values.get(canonical)
    if not values:
        raise ValueError(f"{table}没有{canonical}平台口径")
    return values


def platform_filter_sql(table: str, platform: object, *, column: str | None = None) -> str:
    spec = TABLE_PLATFORM_SPECS[table]
    values = platform_values(table, platform)
    if spec.implicit:
        return ""
    physical = column or spec.field
    literals = ", ".join("'" + value.upper().replace("'", "''") + "'" for value in values)
    return f"UPPER(TRIM({physical})) IN ({literals})"


def normalized_platform_sql(table: str, *, column: str | None = None) -> str:
    spec = TABLE_PLATFORM_SPECS[table]
    if spec.implicit:
        return f"'{spec.implicit}'"
    physical = column or spec.field
    clauses = []
    for canonical in CANONICAL_PLATFORMS:
        values = spec.values.get(canonical)
        if not values:
            continue
        literals = ", ".join("'" + value.upper().replace("'", "''") + "'" for value in values)
        clauses.append(f"WHEN UPPER(TRIM({physical})) IN ({literals}) THEN '{canonical}'")
    return "CASE " + " ".join(clauses) + " ELSE UPPER(TRIM(" + str(physical) + ")) END"
