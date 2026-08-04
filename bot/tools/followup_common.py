from __future__ import annotations

from datetime import date
from typing import Any


def month_keys(start: str, end: str) -> list[str]:
    sy, sm = map(int, str(start)[:7].split("-"))
    ey, em = map(int, str(end)[:7].split("-"))
    out = []
    year, month = sy, sm
    while (year, month) <= (ey, em):
        out.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year += 1
            month = 1
    return out


def prior_month_key(month: str) -> str:
    return f"{int(month[:4]) - 1:04d}{month[4:]}"


def comparison_status(current_rows: int, prior_rows: int, prior_value: float) -> str:
    if current_rows <= 0:
        return "missing_current"
    if prior_rows <= 0:
        return "missing_prior"
    if not prior_value:
        return "base_zero"
    return "ok"


def evidence_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    evidence = []
    for index, row in enumerate(rows, 1):
        evidence.append({"evidence_id": f"row_{index}", **row})
    return evidence


def standard_result(
    *,
    query_meta: dict,
    filters: dict,
    rows: list[dict],
    totals: dict | None = None,
    coverage: dict | None = None,
    missing: list | None = None,
) -> dict:
    return {
        "query_meta": query_meta,
        "filters": filters,
        "totals": totals or {},
        "rows": rows,
        "coverage": coverage or {},
        "missing": missing or [],
        "evidence": evidence_rows(rows),
    }
