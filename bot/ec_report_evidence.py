from __future__ import annotations

"""Platform-neutral evidence published by completed EC report templates."""

from dataclasses import asdict, dataclass, field
from typing import Any


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
    series_scopes: dict[str, list[dict]] = field(default_factory=dict)
    product_scopes: dict[str, list[dict]] = field(default_factory=dict)
    available_dimensions: list[str] = field(default_factory=list)
    unsupported_dimensions: list[str] = field(default_factory=list)
    quality: dict[str, Any] = field(default_factory=dict)
    data_sources: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _period(raw: str | None, meta: dict | None) -> dict:
    values = dict(meta or {})
    return {
        "raw": raw,
        "start_date": values.get("current_start"),
        "end_date": values.get("current_end"),
    }


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
    return ECReportEvidenceV2(
        task_id=task_id, brand=meta.get("brand"), period=_period(meta.get("period"), meta.get("period_meta")),
        platform="TTL", report_type="three_platform_business",
        overall_metrics={"platform_rows": list(result.get("overall") or [])},
        available_dimensions=["overall", "platform"],
        unsupported_dimensions=["key_driver", "series", "sku", "product_title", "product_link"],
        data_sources=list(result.get("sources") or []),
    )


def build_ec_report_evidence(meta: dict, platform: str, task_id: str | None = None) -> dict:
    builders = {"TM": _tmall_evidence, "DY": _douyin_evidence, "JD": _jd_evidence, "TTL": _ttl_evidence}
    builder = builders.get(str(platform or "").upper())
    return builder(meta, task_id).to_dict() if builder else {}


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
        return _douyin_evidence(meta, None).to_dict()
    if "jd_business_result" in payload:
        result = payload["jd_business_result"] or {}
        meta["period_meta"] = result.get("period_meta") or {}
        return _jd_evidence(meta, None).to_dict()
    if "category_result" in payload:
        meta["selected_category"] = (payload.get("category_result") or {}).get("selected_category")
        return _tmall_evidence(meta, None).to_dict()
    if platform == "TTL" or "three_platform_competitor_result" in payload:
        return _ttl_evidence(meta, None).to_dict()
    return {}
