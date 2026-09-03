from __future__ import annotations

"""Translate recoverable execution failures back into conversation state."""


def build_period_revision_request(
    result: dict,
    *,
    original_text: str,
    brand: str | None,
    brand_aliases: list[str] | tuple[str, ...],
    platform: str | None,
    intents: list[str] | tuple[str, ...],
    comparison_spec: dict,
    time_scope: dict,
    business_spec: dict | None = None,
) -> dict | None:
    meta = dict((result or {}).get("meta") or {})
    if meta.get("error_code") != "period_after_latest_date":
        return None
    return {
        "intent": "v2_period",
        "original_text": original_text,
        "brand": brand,
        "brand_aliases": list(brand_aliases or []),
        "platform": platform,
        "intents": list(intents or ["EC_BUSINESS"]),
        "comparison_spec": dict(comparison_spec or {}),
        "time_scope": dict(time_scope or {}),
        "business_spec": dict(business_spec or {}),
        "latest_date": meta.get("latest_date"),
        "execution_error_code": meta.get("error_code"),
    }
