from __future__ import annotations

"""Shared requested-period to effective-period coverage policy."""

from datetime import date, timedelta
from typing import Any


def normalize_period_to_latest(
    period_meta: dict[str, Any], latest_available_date: str | None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Clamp a partially covered request and preserve equal elapsed days.

    A request that starts after the latest date is not shifted and remains the
    caller's responsibility to reject.  Only the trailing uncovered slice is
    removed.
    """
    values = dict(period_meta)
    if not latest_available_date:
        return values, None
    latest = date.fromisoformat(str(latest_available_date)[:10])
    current_start = date.fromisoformat(str(values["current_start"]))
    requested_end = date.fromisoformat(str(values["current_end"]))
    if requested_end <= latest or latest < current_start:
        return values, None
    prior_start = date.fromisoformat(str(values["prior_start"]))
    requested_prior_end = date.fromisoformat(str(values["prior_end"]))
    elapsed_days = (latest - current_start).days
    effective_prior_end = min(prior_start + timedelta(days=elapsed_days), requested_prior_end)
    values.update({
        "requested_start": current_start.isoformat(),
        "requested_end": requested_end.isoformat(),
        "requested_prior_start": prior_start.isoformat(),
        "requested_prior_end": requested_prior_end.isoformat(),
        "current_end": latest.isoformat(),
        "prior_end": effective_prior_end.isoformat(),
        "current_label": f"{current_start.isoformat()}至{latest.isoformat()}",
        "prior_label": f"{prior_start.isoformat()}至{effective_prior_end.isoformat()}",
        "coverage_mode": "MTD_ALIGNED",
    })
    adjustment = {
        "reason": "requested_period_partially_covered",
        "requested_start": current_start.isoformat(),
        "requested_end": requested_end.isoformat(),
        "effective_start": current_start.isoformat(),
        "effective_end": latest.isoformat(),
        "requested_prior_start": prior_start.isoformat(),
        "requested_prior_end": requested_prior_end.isoformat(),
        "effective_prior_start": prior_start.isoformat(),
        "effective_prior_end": effective_prior_end.isoformat(),
        "elapsed_days": elapsed_days + 1,
    }
    values["period_adjustment"] = adjustment
    return values, adjustment


def common_latest_date(*values: str | None) -> str | None:
    parsed = [str(value)[:10] for value in values if value]
    return min(parsed) if parsed else None
