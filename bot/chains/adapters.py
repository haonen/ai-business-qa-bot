from __future__ import annotations

from bot.tools.common import filter_sku, split_periods


def attach_series_to_sku(sku_result: dict, series_result: dict) -> dict:
    result = dict(sku_result or {})
    result["product_lines"] = series_result.get("product_lines") or series_result.get("series") or []
    return result


def build_fraud_result(
    brand: str,
    period: str,
    brand_aliases: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """Business QA flagging: item_id-level ATV > 1000 in the current period.

    This intentionally mirrors the pilot logic: aggregate each item_id first,
    compute ATV=GMV/unit, then flag high-ATV links and roll them up.
    """
    df = filter_sku(brand, period, brand_aliases=brand_aliases)
    if df.empty:
        return {"error": "no_data", "message": "无SKU数据，无法进行生意质检"}
    _, current_df = split_periods(df, "bus_date")
    if current_df.empty:
        return {"error": "no_data", "message": "本期指定时间段无SKU数据，无法进行生意质检"}

    item_df = (
        current_df.groupby("item_id", dropna=False)
        .agg(
            gmv_current=("gmv", "sum"),
            unit=("unit", "sum"),
            key_driver=("key_driver", "first"),
        )
        .reset_index()
    )
    item_df["atv"] = item_df.apply(
        lambda r: float(r["gmv_current"]) / float(r["unit"]) if float(r["unit"] or 0) else 0,
        axis=1,
    )
    item_df["status"] = item_df["atv"].apply(lambda v: "疑似刷单" if v > 1000 else "正常")

    total_gmv = float(item_df["gmv_current"].sum())
    total_unit = float(item_df["unit"].sum())

    breakdown = []
    for status in ["正常", "疑似刷单"]:
        status_df = item_df[item_df["status"] == status]
        status_gmv = float(status_df["gmv_current"].sum())
        status_unit = float(status_df["unit"].sum())
        drivers = []
        for driver, group in status_df.groupby("key_driver", dropna=False):
            gmv = float(group["gmv_current"].sum())
            unit = float(group["unit"].sum())
            drivers.append({
                "key_driver": driver or "其他",
                "gmv_current": round(gmv),
                "unit": round(unit),
                "atv": round(gmv / unit) if unit else 0,
                "weight": round(gmv / total_gmv, 4) if total_gmv else 0,
            })
        breakdown.append({
            "status": status,
            "gmv_current": round(status_gmv),
            "unit": round(status_unit),
            "atv": round(status_gmv / status_unit) if status_unit else 0,
            "weight": round(status_gmv / total_gmv, 4) if total_gmv else 0,
            "drivers": sorted(drivers, key=lambda d: d["gmv_current"], reverse=True),
        })

    risky_gmv = float(item_df.loc[item_df["status"] == "疑似刷单", "gmv_current"].sum())
    return {
        "brand": df.attrs.get("ec_context", {}).get("input_brand", brand),
        "source_brand": df.attrs.get("ec_context", {}).get("source_brand"),
        "period": period,
        "period_meta": df.attrs.get("ec_context", {}),
        "total_gmv": round(total_gmv),
        "total_unit": round(total_unit),
        "fraud_pct": round(risky_gmv / total_gmv, 4) if total_gmv else 0,
        "fraud_flag": "high" if (risky_gmv / total_gmv if total_gmv else 0) > 0.30 else "normal",
        "breakdown": breakdown,
    }


def build_fraud_result_from_driver(category_result: dict, driver_result: dict) -> dict:
    total = category_result.get("total", {})
    total_gmv = total.get("gmv_current") or 0
    total_unit = total.get("unit_current") or 0
    drivers = driver_result.get("driver_summary", {}).get("drivers", [])
    risky = []
    normal = []
    for d in drivers:
        atv = d.get("atv_current") or 0
        target = risky if atv > 1000 else normal
        target.append({
            "key_driver": d.get("key_driver"),
            "gmv_current": d.get("gmv_current", 0),
            "unit": d.get("unit_current", 0),
            "atv": atv,
            "weight": d.get("weight"),
        })

    risky_gmv = sum(d["gmv_current"] for d in risky)
    normal_gmv = max(0, total_gmv - risky_gmv)
    normal_unit = max(0, total_unit - sum(d["unit"] for d in risky))
    breakdown = [
        {
            "status": "正常",
            "gmv_current": normal_gmv,
            "unit": normal_unit,
            "atv": round(normal_gmv / normal_unit) if normal_unit else 0,
            "weight": round(normal_gmv / total_gmv, 4) if total_gmv else 0,
            "drivers": normal,
        },
        {
            "status": "疑似刷单",
            "gmv_current": risky_gmv,
            "unit": sum(d["unit"] for d in risky),
            "atv": round(risky_gmv / sum(d["unit"] for d in risky)) if sum(d["unit"] for d in risky) else 0,
            "weight": round(risky_gmv / total_gmv, 4) if total_gmv else 0,
            "drivers": risky,
        },
    ]
    return {
        "total_gmv": total_gmv,
        "total_unit": total_unit,
        "fraud_pct": round(risky_gmv / total_gmv, 4) if total_gmv else 0,
        "breakdown": breakdown,
    }
