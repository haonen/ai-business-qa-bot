from __future__ import annotations

import calendar
from datetime import date


MARKET_PLATFORMS = ("TM", "DY", "JD")
MARKET_SEGMENTS = ("PURE MASS", "SELECTIVE", "PROFESSIONAL")
TTL_CATEGORIES = ("Skincare", "Hair", "Makeup + Fragrance", "Makeup+Fragrance")


def monthly_business_date_sql(column: str = "bus_date") -> str:
    """Restore monthly-table YYYY-01-MM storage to business month YYYY-MM-01."""
    return (
        f"STR_TO_DATE(CONCAT(YEAR({column}), '-', "
        f"LPAD(DAY({column}), 2, '0'), '-01'), '%Y-%m-%d')"
    )


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
    normalized_platform = str(platform or "").strip().upper()
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
