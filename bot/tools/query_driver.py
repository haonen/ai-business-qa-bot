from __future__ import annotations

from bot.tools.common import combine_two_years, filter_kol, filter_sku, split_years, tool
from bot.utils import safe_div
from bot.tools.query_series import _infer_series_from_title, _llm_series_fallback, _series_keywords_for_brand


@tool
def query_driver(
    brand: str,
    period: str,
    category: str | None = None,
    link_type: str | None = None,
    series: str | None = None,
    function_tag: str | None = None,
) -> dict:
    """三渠道拆分，两年对比，并附情报通直播参考。"""
    try:
        df = filter_sku(brand, period, category=category, link_type=link_type, series=series, function_tag=function_tag)
        if df.empty:
            return {"error": "no_data", "message": "指定条件下无SKU数据"}
        df25, df26 = split_years(df, "bus_date", period)
        rows, total = combine_two_years(df25, df26, ["kol_driver"])
        by_driver = {r["kol_driver"]: r for r in rows}
        drivers = [by_driver.get(name, {
            "kol_driver": name, "gmv_2026": 0, "gmv_2025": 0, "unit_2026": 0,
            "unit_2025": 0, "atv_2026": None, "atv_2025": None, "weight": 0,
            "evol": None, "share_delta": None, "gmv_diff": 0,
        }) for name in ["Non-KOL", "T2", "李佳琦"]]

        low_coverage = not _series_keywords_for_brand(brand)
        llm_candidates = _llm_series_fallback(
            brand,
            df26.sort_values("gmv", ascending=False)["product_title"].dropna().head(50).tolist(),
        ) if low_coverage else []

        driver_top_skus = []
        for driver_name in ["李佳琦", "T2", "Non-KOL"]:
            d26 = df26[df26["kol_driver"] == driver_name]
            d25 = df25[df25["kol_driver"] == driver_name]
            total_driver_26 = float(d26["gmv"].sum()) if not d26.empty else 0.0
            total_driver_25 = float(d25["gmv"].sum()) if not d25.empty else 0.0
            if total_driver_26 <= 0:
                continue

            buckets = {}
            for year, frame in [(2025, d25), (2026, d26)]:
                for _, row in frame.iterrows():
                    series_name, function_tag, source = _infer_series_from_title(row.get("product_title"), brand, llm_candidates)
                    if series_name not in buckets:
                        buckets[series_name] = {
                            "product_line": series_name,
                            "series": series_name,
                            "function_tag": function_tag,
                            "source": source,
                            "gmv_2026": 0.0,
                            "gmv_2025": 0.0,
                            "unit_2026": 0.0,
                            "unit_2025": 0.0,
                            "item_ids": set(),
                        }
                    buckets[series_name][f"gmv_{year}"] += float(row.get("gmv") or 0)
                    buckets[series_name][f"unit_{year}"] += float(row.get("unit") or 0)
                    if year == 2026:
                        buckets[series_name]["item_ids"].add(str(row.get("item_id")))

            product_lines = []
            for item in buckets.values():
                g26 = item["gmv_2026"]
                g25 = item["gmv_2025"]
                u26 = item["unit_2026"]
                item["gmv_2026"] = round(g26)
                item["gmv_2025"] = round(g25)
                item["unit_2026"] = round(u26)
                item["unit_2025"] = round(item["unit_2025"])
                item["atv_2026"] = round(g26 / u26, 2) if u26 else None
                item["weight"] = safe_div(g26, total_driver_26)
                item["share_delta"] = None if not total_driver_26 or not total_driver_25 else round(g26 / total_driver_26 - g25 / total_driver_25, 4)
                item["evol"] = None if not g25 else round((g26 - g25) / g25, 4)
                item["item_ids"] = sorted(item["item_ids"])
                product_lines.append(item)

            top_skus = []
            if not d26.empty:
                sku26 = d26.groupby("item_id", dropna=False).agg(
                    gmv_2026=("gmv", "sum"),
                    unit=("unit", "sum"),
                    product_title=("product_title", "first"),
                    category_cn=("category_cn", "first"),
                    link_type=("link_type", "first"),
                ).sort_values("gmv_2026", ascending=False).head(10).reset_index()
                for _, row in sku26.iterrows():
                    gmv = float(row["gmv_2026"] or 0)
                    unit = float(row["unit"] or 0)
                    top_skus.append({
                        "item_id": str(row["item_id"]),
                        "product_title": row["product_title"],
                        "category_cn": row["category_cn"],
                        "link_type": row["link_type"],
                        "kol_driver": driver_name,
                        "gmv_2026": round(gmv),
                        "weight": safe_div(gmv, total_driver_26),
                        "unit": round(unit),
                        "atv": round(gmv / unit, 2) if unit else None,
                    })

            driver_top_skus.append({
                "kol_driver": driver_name,
                "total_gmv_2026": round(total_driver_26),
                "total_gmv_2025": round(total_driver_25),
                "product_lines": sorted(product_lines, key=lambda x: x["gmv_2026"], reverse=True),
                "top_skus": top_skus,
            })

        kol = filter_kol(brand, period)
        live_reference = {"note": "以下数据来自情报通，仅含直播渠道", "breakdown": []}
        if not kol.empty:
            _, kol26 = split_years(kol, "live_start_date", period)
            total_live = float(kol26["live_sales_amount"].sum()) if not kol26.empty else 0
            live_reference["ttl_live_gmv_2026"] = round(total_live)
            for kt in ["李佳琦", "T2", "店播"]:
                val = float(kol26.loc[kol26["kol_type"] == kt, "live_sales_amount"].sum())
                unit = float(kol26.loc[kol26["kol_type"] == kt, "live_sales_unit"].sum())
                live_reference["breakdown"].append({
                    "kol_type": kt,
                    "gmv_2026": round(val),
                    "unit_2026": round(unit),
                    "atv_2026": round(val / unit, 2) if unit else None,
                    "of_ttl_live": safe_div(val, total_live),
                })

        return {
            "brand": brand,
            "period": period,
            "category": category,
            "link_type": link_type,
            "series": series,
            "function_tag": function_tag,
            "driver_summary": {
                "total_gmv_2026": total["gmv_2026"],
                "total_gmv_2025": total["gmv_2025"],
                "total_unit_2026": total["unit_2026"],
                "total_unit_2025": total["unit_2025"],
                "drivers": drivers,
            },
            "live_reference": live_reference,
            "driver_top_skus": driver_top_skus,
        }
    except Exception as exc:
        return {"error": "execution_error", "message": str(exc)}
