from __future__ import annotations

"""Platform-neutral evidence published by completed EC report templates."""

from dataclasses import asdict, dataclass, field
import hashlib
from typing import Any

from bot.brand_reference import english_brand_for_chinese


@dataclass
class ECReportEvidenceV2:
    schema_version: str = "ec-report-evidence.v2"
    task_id: str | None = None
    brand: str | None = None
    brand_aliases: list[str] = field(default_factory=list)
    period: dict[str, Any] = field(default_factory=dict)
    platform: str | None = None
    report_type: str | None = None
    overall_metrics: dict[str, Any] = field(default_factory=dict)
    categories: list[dict] = field(default_factory=list)
    selected_category: str | None = None
    key_drivers: list[dict] = field(default_factory=list)
    platforms: list[dict] = field(default_factory=list)
    series_scopes: dict[str, list[dict]] = field(default_factory=dict)
    product_scopes: dict[str, list[dict]] = field(default_factory=dict)
    available_dimensions: list[str] = field(default_factory=list)
    unsupported_dimensions: list[str] = field(default_factory=list)
    quality: dict[str, Any] = field(default_factory=dict)
    data_sources: list[dict] = field(default_factory=list)
    evidence_id: str | None = None
    canonical_brand: str | None = None
    brand_surface: str | None = None
    period_range: dict[str, Any] = field(default_factory=dict)
    manifest: dict[str, Any] = field(default_factory=dict)
    records: list[dict] = field(default_factory=list)
    omitted_slices: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _period(raw: str | None, meta: dict | None) -> dict:
    values = dict(meta or {})
    return {
        **values,
        "raw": raw,
        "start_date": values.get("current_start"),
        "end_date": values.get("current_end"),
        "comparison_start_date": values.get("prior_start"),
        "comparison_end_date": values.get("prior_end"),
    }


def _number(row: dict, *keys: str) -> float | None:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number == number:
            return number
    return None


def _subject(row: dict, dimension: str) -> str:
    keys = {
        "overall": ("brand", "name"),
        "category": ("category_cn", "category", "name"),
        "key_driver": ("key_driver", "driver", "name"),
        "platform": ("platform", "name"),
        "series": ("product_line", "series", "name"),
        "sku": ("product_name", "product_title", "item_name", "item_id", "sku"),
    }.get(dimension, ("name",))
    return next((str(row.get(key)).strip() for key in keys if row.get(key)), dimension)


def _dimension_records(rows: list[dict], dimension: str, prefix: str) -> list[dict]:
    current_values = [
        _number(row, "gmv_current", "gmv_actual", "current_gmv", "sales_gmv") or 0
        for row in rows
    ]
    prior_values = [
        _number(row, "gmv_prior", "prior_gmv", "previous_gmv") or 0
        for row in rows
    ]
    total_current, total_prior = sum(current_values), sum(prior_values)
    records = []
    for index, row in enumerate(rows, 1):
        subject = _subject(row, dimension)
        if not subject or subject in {"未分类", "NULL", "None"}:
            continue
        current = current_values[index - 1]
        prior = prior_values[index - 1]
        growth = _number(row, "gmv_growth", "gmv_change", "gmv_diff")
        if growth is None:
            growth = current - prior
        evol = _number(row, "evol", "gmv_evol")
        if evol is None and prior:
            evol = growth / prior
        share = _number(row, "share", "weight", "gmv_weight")
        if share is None and total_current:
            share = current / total_current
        prior_share = prior / total_prior if total_prior else None
        share_delta = _number(row, "share_delta", "weight_change", "wgt_change")
        if share_delta is None and share is not None and prior_share is not None:
            share_delta = share - prior_share
        records.append({
            "evidence_id": f"{prefix}:{dimension}:{index}",
            "dimension": dimension,
            "subject": subject,
            "gmv_actual": current,
            "gmv_prior": prior,
            "gmv_growth": growth,
            "evol": evol,
            "share": share,
            "share_delta": share_delta,
            "growth_contribution": _number(row, "growth_contribution", "contribution"),
            "concentration": _number(row, "concentration"),
            "rank": row.get("rank") or index,
        })
    return records


def _envelope(evidence: ECReportEvidenceV2) -> ECReportEvidenceV2:
    surface = evidence.brand
    canonical = english_brand_for_chinese(surface) or surface
    identity = "|".join((str(canonical or ""), str(evidence.period.get("raw") or ""), str(evidence.platform or "")))
    envelope_id = "ev_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    records: list[dict] = []
    if evidence.overall_metrics:
        records += _dimension_records([evidence.overall_metrics], "overall", envelope_id)
    records += _dimension_records(evidence.categories, "category", envelope_id)
    records += _dimension_records(evidence.key_drivers, "key_driver", envelope_id)
    records += _dimension_records(evidence.platforms, "platform", envelope_id)
    for scope, rows in evidence.series_scopes.items():
        enriched = [{**row, "scope": scope} for row in rows]
        records += _dimension_records(enriched, "series", envelope_id)
    for scope, rows in evidence.product_scopes.items():
        enriched = [{**row, "scope": scope} for row in rows]
        records += _dimension_records(enriched, "sku", envelope_id)
    counts: dict[str, int] = {}
    for record in records:
        counts[record["dimension"]] = counts.get(record["dimension"], 0) + 1
    evidence.evidence_id = envelope_id
    evidence.canonical_brand = canonical
    evidence.brand_surface = surface
    evidence.period_range = dict(evidence.period)
    evidence.records = records
    evidence.manifest = {
        "schema_version": evidence.schema_version,
        "record_count": len(records),
        "dimension_counts": counts,
        "available_dimensions": list(evidence.available_dimensions),
        "unsupported_dimensions": list(evidence.unsupported_dimensions),
        "quality": dict(evidence.quality),
    }
    evidence.omitted_slices = []
    return evidence


def _tmall_evidence(meta: dict, task_id: str | None) -> ECReportEvidenceV2:
    cache = meta.get("last_result_cache") or {}
    category = cache.get("category_result") or {}
    driver = cache.get("driver_result") or {}
    selected = str(meta.get("selected_category") or "")
    series = list((cache.get("series_result") or {}).get("series") or [])
    products = list((cache.get("sku_result") or {}).get("products") or [])
    return ECReportEvidenceV2(
        task_id=task_id, brand=meta.get("brand"), brand_aliases=list(meta.get("brand_aliases") or []),
        period=_period(meta.get("period"), meta.get("period_meta")), platform="TM",
        report_type="tmall_business", overall_metrics=(category.get("overall_total") or {}),
        categories=list(category.get("categories") or []), selected_category=selected or None,
        key_drivers=list(((driver.get("driver_summary") or {}).get("drivers") or [])),
        series_scopes={selected: series} if selected and series else {},
        product_scopes={selected: products} if selected and products else {},
        available_dimensions=["overall", "category", "key_driver", "series", "sku", "product_title"],
        data_sources=list(category.get("sources") or []),
    )


def _douyin_evidence(meta: dict, task_id: str | None) -> ECReportEvidenceV2:
    cache = meta.get("last_result_cache") or {}
    result = cache.get("douyin_business_result") or {}
    product = result.get("product_analysis") or {}
    selected = str(product.get("selected_category") or result.get("selected_category") or "")
    series = list(product.get("selected_category_series") or [])
    products = list(product.get("selected_category_top_links") or [])
    driver_products = {
        f"driver:{row.get('key_driver')}": list(row.get("top_links") or [])
        for row in (product.get("driver_drilldowns") or []) if row.get("key_driver")
    }
    return ECReportEvidenceV2(
        task_id=task_id, brand=meta.get("brand"), brand_aliases=list(meta.get("brand_aliases") or []),
        period=_period(meta.get("period"), meta.get("period_meta")), platform="DY",
        report_type="douyin_business", overall_metrics=dict(result.get("brand_result") or {}),
        categories=list(product.get("analysis_categories") or result.get("categories") or []),
        selected_category=selected or None, key_drivers=list(product.get("key_drivers") or []),
        series_scopes={selected: series} if selected and series else {},
        product_scopes={**({selected: products} if selected and products else {}), **driver_products},
        available_dimensions=[
            "overall", "category", "key_driver", "series", "sku", "product_title", "product_link",
        ],
        quality=dict(product.get("quality") or {}), data_sources=list(result.get("sources") or []),
    )


def _jd_evidence(meta: dict, task_id: str | None) -> ECReportEvidenceV2:
    cache = meta.get("last_result_cache") or {}
    result = cache.get("jd_business_result") or {}
    return ECReportEvidenceV2(
        task_id=task_id, brand=meta.get("brand"), brand_aliases=list(meta.get("brand_aliases") or []),
        period=_period(meta.get("period"), meta.get("period_meta")), platform="JD",
        report_type="jd_business", overall_metrics=dict(result.get("brand_result") or {}),
        categories=list(result.get("all_categories") or result.get("categories") or []),
        selected_category=result.get("selected_category"), available_dimensions=["overall", "category"],
        unsupported_dimensions=["key_driver", "series", "sku", "product_title", "product_link"],
        data_sources=list(result.get("sources") or []),
    )


def _ttl_evidence(meta: dict, task_id: str | None) -> ECReportEvidenceV2:
    cache = meta.get("last_result_cache") or {}
    result = cache.get("three_platform_competitor_result") or {}
    platform_rows = list(result.get("overall") or [])
    ttl_row = next(
        (row for row in platform_rows if str(row.get("platform") or "").upper() == "TTL"),
        {},
    )
    return ECReportEvidenceV2(
        task_id=task_id, brand=meta.get("brand"), period=_period(meta.get("period"), meta.get("period_meta")),
        platform="TTL", report_type="three_platform_business",
        overall_metrics=dict(ttl_row),
        platforms=[
            dict(row) for row in platform_rows
            if str(row.get("platform") or "").upper() != "TTL"
        ],
        available_dimensions=["overall", "platform"],
        unsupported_dimensions=["key_driver", "series", "sku", "product_title", "product_link"],
        data_sources=list(result.get("sources") or []),
    )


def build_ec_report_evidence(meta: dict, platform: str, task_id: str | None = None) -> dict:
    builders = {"TM": _tmall_evidence, "DY": _douyin_evidence, "JD": _jd_evidence, "TTL": _ttl_evidence}
    builder = builders.get(str(platform or "").upper())
    return _envelope(builder(meta, task_id)).to_dict() if builder else {}


def evidence_from_cache(cache: dict | None, *, brand: str | None = None, period: str | None = None,
                        platform: str | None = None) -> dict:
    """Read V2 evidence first and adapt legacy report caches when necessary."""
    payload = dict(cache or {})
    evidence = payload.get("ec_report_evidence")
    if isinstance(evidence, dict):
        return evidence
    meta = {"brand": brand, "period": period, "last_result_cache": payload}
    if "douyin_business_result" in payload:
        result = payload["douyin_business_result"] or {}
        meta["period_meta"] = result.get("period_meta") or {}
        return _envelope(_douyin_evidence(meta, None)).to_dict()
    if "jd_business_result" in payload:
        result = payload["jd_business_result"] or {}
        meta["period_meta"] = result.get("period_meta") or {}
        return _envelope(_jd_evidence(meta, None)).to_dict()
    if "category_result" in payload:
        meta["selected_category"] = (payload.get("category_result") or {}).get("selected_category")
        return _envelope(_tmall_evidence(meta, None)).to_dict()
    if platform == "TTL" or "three_platform_competitor_result" in payload:
        return _envelope(_ttl_evidence(meta, None)).to_dict()
    return {}
