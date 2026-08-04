from __future__ import annotations

import json
import re
from collections import defaultdict

from bot.tools.common import filter_sku, load_function_tags, load_series_map, match_function_tag, split_periods, tool
from bot.utils import llm_client, safe_div, safe_evol


def _series_keywords_for_brand(brand: str) -> dict[str, dict]:
    series_map = load_series_map()
    entry = series_map.get(brand) or series_map.get(brand.upper()) or series_map.get(brand.lower()) or {}
    return entry.get("series", {}) if isinstance(entry, dict) else {}


def _ordered_series_defs(brand: str) -> list[tuple[str, dict, list[str]]]:
    series_defs = _series_keywords_for_brand(brand)
    rows = []
    for name, meta in series_defs.items():
        keywords = list(meta.get("keywords") or [])
        if name not in keywords:
            keywords.append(name)
        keywords = sorted({k for k in keywords if k}, key=len, reverse=True)
        rows.append((name, meta, keywords))
    return sorted(rows, key=lambda x: max((len(k) for k in x[2]), default=0), reverse=True)


def _infer_series_from_title(
    title: str,
    brand: str,
    llm_candidates: list[dict] | None = None,
) -> tuple[str, str | None, str]:
    title = title or ""
    for name, meta, keywords in _ordered_series_defs(brand):
        if any(keyword in title for keyword in keywords):
            return name, meta.get("function_tag"), "series_map"

    for item in llm_candidates or []:
        name = str(item.get("series") or item.get("product_line") or "").strip()
        keyword = str(item.get("keyword") or name).strip()
        if name and keyword and keyword in title:
            return name, match_function_tag(title), "llm_fallback"

    return "其他系列", match_function_tag(title), "fallback"


def _llm_series_fallback(brand: str, titles: list[str]) -> list[dict]:
    if not titles:
        return []
    prompt = (
        f"以下是品牌{brand}的Top链接标题，请归纳可能的产品系列名。"
        "系列名必须是标题中反复出现的真实产品系列/产品线词，不要输出功效词（如美白/抗老/修护）作为系列。"
        "只返回JSON数组，格式为[{\"series\":\"系列名\",\"keyword\":\"标题关键词\"}]，最多8个。"
        f"标题：{json.dumps(titles[:50], ensure_ascii=False)}"
    )
    try:
        resp = llm_client().chat.completions.create(
            model="qwen-plus",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=600,
        )
        raw = resp.choices[0].message.content or ""
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        return json.loads(match.group()) if match else []
    except Exception:
        return []


@tool
def query_series(
    brand: str,
    period: str,
    category: str,
    key_driver: str | None = None,
    brand_aliases: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """系列分布，优先码表，兜底标题规则/LLM归纳。"""
    try:
        df = filter_sku(
            brand,
            period,
            category=category,
            key_driver=key_driver,
            brand_aliases=brand_aliases,
        )
        if df.empty:
            return {"error": "no_data", "message": "指定条件下无SKU数据"}
        context = dict(df.attrs.get("ec_context") or {})
        prior_df, current_df = split_periods(df, "bus_date")
        total_current = float(current_df["gmv"].sum()) if not current_df.empty else 0.0
        total_prior = float(prior_df["gmv"].sum()) if not prior_df.empty else 0.0

        low_coverage = not _series_keywords_for_brand(brand)
        llm_candidates = _llm_series_fallback(
            brand,
            current_df.sort_values("gmv", ascending=False)["product_title"].dropna().head(50).tolist(),
        ) if low_coverage else []

        bucket: dict[str, dict] = defaultdict(lambda: {
            "series": "", "function_tag": None, "source": "",
            "gmv_current": 0.0, "gmv_prior": 0.0,
            "unit_current": 0.0, "unit_prior": 0.0, "link_count": 0,
        })
        for suffix, frame in [("prior", prior_df), ("current", current_df)]:
            for _, row in frame.iterrows():
                series, function_tag, source = _infer_series_from_title(row.get("product_title"), brand, llm_candidates)
                b = bucket[series]
                b["series"] = series
                b["function_tag"] = b["function_tag"] or function_tag
                b["source"] = b["source"] or source
                b[f"gmv_{suffix}"] += float(row.get("gmv") or 0)
                b[f"unit_{suffix}"] += float(row.get("unit") or 0)
                if suffix == "current":
                    b["link_count"] += 1

        rows = []
        for item in bucket.values():
            g_current, g_prior = item["gmv_current"], item["gmv_prior"]
            u_current, u_prior = item["unit_current"], item["unit_prior"]
            rows.append({
                "product_line": item["series"],
                "series": item["series"],
                "function_tag": item["function_tag"],
                "source": item["source"],
                "gmv_current": round(g_current),
                "gmv_prior": round(g_prior),
                "unit_current": round(u_current),
                "unit_prior": round(u_prior),
                "atv_current": round(g_current / u_current, 2) if u_current else None,
                "atv_prior": round(g_prior / u_prior, 2) if u_prior else None,
                "weight": safe_div(g_current, total_current),
                "evol": safe_evol(g_current, g_prior),
                "share_delta": None if not total_current or not total_prior else round(g_current / total_current - g_prior / total_prior, 4),
                "link_count": item["link_count"],
            })
        rows = sorted(rows, key=lambda x: x["gmv_current"], reverse=True)
        return {
            "brand": context.get("input_brand", brand),
            "input_brand": brand,
            "source_brand": context.get("source_brand"),
            "period": period,
            "period_meta": context,
            "category": category,
            "key_driver": key_driver,
            "series": rows,
            "product_lines": rows,
            "llm_candidates": llm_candidates,
            "coverage": "series_map" if not low_coverage else "fallback",
        }
    except Exception as exc:
        return {"error": "execution_error", "message": str(exc)}
