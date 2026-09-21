from __future__ import annotations

"""Deterministic, allow-listed transformations for executable agent plans."""

from collections import defaultdict
from typing import Any, Callable


class DeriveError(ValueError):
    pass


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rankable(rows: list[dict], metric: str) -> list[dict]:
    return [row for row in rows if _number(row.get(metric)) is not None]


def top_n(*, rows: list[dict], rank_by: str, limit: int = 5, **_: Any) -> dict:
    ranked = sorted(
        _rankable(rows, rank_by),
        key=lambda row: (_number(row.get(rank_by)) or 0, _number(row.get("gmv_actual")) or 0),
        reverse=True,
    )
    return {"rows": [{**row, "derived_rank": index} for index, row in enumerate(ranked[:limit], 1)]}


def bottom_n(*, rows: list[dict], rank_by: str, limit: int = 5, **_: Any) -> dict:
    ranked = sorted(
        _rankable(rows, rank_by),
        key=lambda row: (_number(row.get(rank_by)) or 0, -(_number(row.get("gmv_actual")) or 0)),
    )
    return {"rows": [{**row, "derived_rank": index} for index, row in enumerate(ranked[:limit], 1)]}


def sort_and_rank(
    *, rows: list[dict], rank_by: str, descending: bool = True, limit: int | None = None, **_: Any,
) -> dict:
    ranked = sorted(
        _rankable(rows, rank_by),
        key=lambda row: (_number(row.get(rank_by)) or 0, _number(row.get("gmv_actual")) or 0),
        reverse=descending,
    )
    if limit is not None:
        ranked = ranked[:limit]
    return {"rows": [{**row, "derived_rank": index} for index, row in enumerate(ranked, 1)]}


def _per_group_extreme(
    *, rows: list[dict], group_by: str, rank_by: str, largest: bool,
    supported_values: list[str] | None = None,
    expected_groups: list[str] | None = None, **_: Any,
) -> dict:
    groups: dict[str, list[dict]] = defaultdict(list)
    supported = {str(value).upper() for value in supported_values or []}
    for row in rows:
        group = str(row.get(group_by) or "").strip()
        metric = _number(row.get(rank_by))
        candidate = str(row.get("platform") or row.get("value") or "").upper()
        if not group or metric is None or (supported and candidate not in supported):
            continue
        groups[group].append(row)

    selected, missing_groups = [], []
    all_groups = list(dict.fromkeys(
        [str(value or "").strip() for value in (expected_groups or [])]
        + [str(row.get(group_by) or "").strip() for row in rows]
    ))
    for group in [value for value in all_groups if value]:
        candidates = groups.get(group) or []
        if not candidates:
            missing_groups.append(group)
            continue
        ordered = sorted(
            candidates,
            key=lambda row: (
                _number(row.get(rank_by)) or 0,
                _number(row.get("gmv_actual")) or 0,
                -{"TM": 0, "DY": 1, "JD": 2}.get(str(row.get("platform") or "").upper(), 99),
            ),
            reverse=largest,
        )
        winner = ordered[0]
        primary = _number(winner.get(rank_by))
        tied = [row for row in candidates if _number(row.get(rank_by)) == primary]
        selected.append({
            **winner,
            "group": group,
            "selection_metric": rank_by,
            "selection_value": primary,
            "all_negative": bool(largest and all((_number(row.get(rank_by)) or 0) < 0 for row in candidates)),
            "tie": len(tied) > 1,
            "tied_candidates": [
                str(row.get("platform") or row.get("value") or "") for row in tied
            ],
        })
    return {
        "rows": selected,
        "brand_platform_pairs": [
            {"brand": row.get(group_by), "platform": row.get("platform"), **row}
            for row in selected
            if row.get("platform")
        ],
        "missing_groups": missing_groups,
    }


def per_group_argmax(**kwargs: Any) -> dict:
    return _per_group_extreme(largest=True, **kwargs)


def per_group_argmin(**kwargs: Any) -> dict:
    return _per_group_extreme(largest=False, **kwargs)


def filter_supported(
    *, rows: list[dict], field: str, supported_values: list[str], **_: Any,
) -> dict:
    supported = {str(value).upper() for value in supported_values}
    kept = [row for row in rows if str(row.get(field) or "").upper() in supported]
    return {"rows": kept, "excluded_count": len(rows) - len(kept)}


def join_by_key(
    *, left: list[dict], right: list[dict], key: str, **_: Any,
) -> dict:
    right_index = {str(row.get(key)): row for row in right if row.get(key) is not None}
    return {"rows": [{**row, **right_index.get(str(row.get(key)), {})} for row in left]}


def build_pairs(
    *, rows: list[dict], left_field: str = "brand", right_field: str = "platform", **_: Any,
) -> dict:
    return {"rows": [
        {left_field: row.get(left_field), right_field: row.get(right_field)}
        for row in rows if row.get(left_field) and row.get(right_field)
    ]}


def resolve_followup_subject(*, rows: list[dict], **_: Any) -> dict:
    """Executor-owned resolver marker; inputs are already validated session evidence."""
    return {"rows": list(rows)}


def check_evidence_coverage(*, rows: list[dict], **_: Any) -> dict:
    """Executor-owned coverage marker used by the controlled follow-up recipe."""
    return {"rows": list(rows), "requires_query": True}


DERIVE_OPERATORS: dict[str, Callable[..., dict]] = {
    "top_n": top_n,
    "bottom_n": bottom_n,
    "per_group_argmax": per_group_argmax,
    "per_group_argmin": per_group_argmin,
    "sort_and_rank": sort_and_rank,
    "filter_supported": filter_supported,
    "join_by_key": join_by_key,
    "build_pairs": build_pairs,
    "resolve_followup_subject": resolve_followup_subject,
    "check_evidence_coverage": check_evidence_coverage,
}


def run_derive(operator: str, **inputs: Any) -> dict:
    handler = DERIVE_OPERATORS.get(operator)
    if not handler:
        raise DeriveError(f"derive operator is not registered: {operator}")
    return handler(**inputs)
