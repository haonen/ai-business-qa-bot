from __future__ import annotations

import calendar
from datetime import date

from bot.platforms import canonical_platform


MARKET_PLATFORMS = ("TM", "DY", "JD")
MARKET_SEGMENTS = ("BEAUTY MARKET", "PURE MASS", "SELECTIVE", "PROFESSIONAL")
MARKET_CATEGORIES = (
    "TOTAL BEAUTY", "FEMALE SKINCARE", "MAKEUP", "HAIR", "MALE SKINCARE",
)

# Confirmed cross-platform business taxonomy.  Skincare at level 1 contains
# both female and male skincare; female skincare is therefore Skincare less
# the level-2 male-skincare rows.  Fragrance alone is outside TTL Beauty.
_MAKEUP_LEVEL_1 = (
    "makeup", "makeup (exclude fragrance)", "makeup + fragrance",
    "makeup+fragrance",
)


def date_cast_sql(column: str = "bus_date") -> str:
    """Normalize a DATE or YYYY-MM-DD string column before date operations."""
    return f"CAST({column} AS DATE)"


def monthly_business_date_sql(column: str = "bus_date") -> str:
    """Restore monthly-table YYYY-01-MM storage to business month YYYY-MM-01."""
    return (
        f"STR_TO_DATE(CONCAT(YEAR({column}), '-', "
        f"LPAD(DAY({column}), 2, '0'), '-01'), '%Y-%m-%d')"
    )


def store_rank_monthly_date_sql(column: str = "bus_date") -> str:
    """Date expression for three_platform_store_rank_monthly's normal dates."""
    return f"CAST({column} AS DATE)"


def normalize_market_category(category: str | None) -> str:
    value = str(category or "TOTAL BEAUTY").strip().upper()
    aliases = {
        "TTL BEAUTY": "TOTAL BEAUTY",
        "TOTAL BEAUTY": "TOTAL BEAUTY",
        "SKINCARE": "FEMALE SKINCARE",
        "FEMALE SKINCARE": "FEMALE SKINCARE",
        "MAKEUP": "MAKEUP",
        "HAIR": "HAIR",
        "HAIRCARE": "HAIR",
        "MALE SKINCARE": "MALE SKINCARE",
        "MEX": "MALE SKINCARE",
    }
    normalized = aliases.get(value)
    if normalized not in MARKET_CATEGORIES:
        raise ValueError("不支持的Category参数。")
    return normalized


def store_rank_business_category_sql(
    category: str | None,
    level_1_column: str = "category_EN_level_1",
    level_2_column: str = "category_EN_level_2",
) -> str:
    """Return the shared TM/DY/JD/TTL business-category row filter."""
    category = normalize_market_category(category)
    level_1 = f"LOWER(TRIM(COALESCE({level_1_column}, '')))"
    level_2 = f"LOWER(TRIM(COALESCE({level_2_column}, '')))"
    makeup = ", ".join(f"'{value}'" for value in _MAKEUP_LEVEL_1)
    if category == "TOTAL BEAUTY":
        return f"({level_1} = 'skincare' OR {level_1} = 'hair' OR {level_1} IN ({makeup}))"
    if category == "FEMALE SKINCARE":
        return f"({level_1} = 'skincare' AND {level_2} <> 'male skincare')"
    if category == "MALE SKINCARE":
        return f"({level_1} = 'skincare' AND {level_2} = 'male skincare')"
    if category == "MAKEUP":
        return f"{level_1} IN ({makeup})"
    return f"{level_1} = 'hair'"


def store_rank_core_category_sql(column: str = "clear_category_status") -> str:
    """Compatibility alias for the old TTL-only callers."""
    del column
    return store_rank_business_category_sql("TOTAL BEAUTY")


def segmented_market_category_sql(
    category: str | None, column: str = "category_EN",
) -> str:
    category = normalize_market_category(category)
    source_value = {
        "TOTAL BEAUTY": "TOTAL BEAUTY",
        "FEMALE SKINCARE": "FEMALE SKINCARE",
        "MAKEUP": "MAKEUP",
        "HAIR": "HAIR",
        "MALE SKINCARE": "MALE SKINCARE",
    }[category]
    return f"UPPER(TRIM({column})) = '{source_value}'"


def month_slices(start: str, end: str) -> list[dict]:
    start_date = date.fromisoformat(str(start)[:10])
    end_date = date.fromisoformat(str(end)[:10])
    rows: list[dict] = []
    year, month = start_date.year, start_date.month
    while (year, month) <= (end_date.year, end_date.month):
        month_start = date(year, month, 1)
        month_end = date(year, month, calendar.monthrange(year, month)[1])
        rows.append({
            "month": f"{year:04d}-{month:02d}",
            "start": max(start_date, month_start).isoformat(),
            "end": min(end_date, month_end).isoformat(),
            "full_month": start_date <= month_start and end_date >= month_end,
        })
        month += 1
        if month == 13:
            year += 1
            month = 1
    return rows


def expected_platforms(platform: str) -> tuple[str, ...]:
    if platform not in {"TTL", *MARKET_PLATFORMS}:
        raise ValueError("不支持的平台参数。")
    return MARKET_PLATFORMS if platform == "TTL" else (platform,)


def validate_scope(segment: str, platform: str) -> tuple[str, str]:
    normalized_segment = str(segment or "").strip().upper()
    normalized_platform = canonical_platform(platform)
    if normalized_segment not in MARKET_SEGMENTS:
        raise ValueError("不支持的Segment参数。")
    expected_platforms(normalized_platform)
    return normalized_segment, normalized_platform


def comparison_status(current_rows: int, prior_rows: int, prior_value: float) -> str:
    if current_rows <= 0:
        return "missing_current"
    if prior_rows <= 0:
        return "missing_prior"
    if prior_value == 0:
        return "base_zero"
    return "ok"


def evol(current: float, prior: float) -> float | None:
    return current / prior - 1 if prior else None
