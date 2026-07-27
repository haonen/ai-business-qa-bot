from __future__ import annotations

import json
import re
from collections import defaultdict

from bot.tools.common import filter_sku, load_function_tags, load_series_map, match_function_tag, split_years, tool
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
    kol_driver: str | None = None,
    link_type: str | None = None,
) -> dict:
    """系列分布，优先码表，兜底标题规则/LLM归纳。"""
    try:
        df = filter_sku(brand, period, category=category, kol_driver=kol_driver, link_type=link_type)
        if df.empty:
            return {"error": "no_data", "message": "指定条件下无SKU数据"}
        df25, df26 = split_years(df, "bus_date", period)
        total_26 = float(df26["gmv"].sum()) if not df26.empty else 0.0
        total_25 = float(df25["gmv"].sum()) if not df25.empty else 0.0

        low_coverage = not _series_keywords_for_brand(brand)
        llm_candidates = _llm_series_fallback(
            brand,
            df26.sort_values("gmv", ascending=False)["product_title"].dropna().head(50).tolist(),
        ) if low_coverage else []

        bucket: dict[str, dict] = defaultdict(lambda: {
            "series": "", "function_tag": None, "source": "", "gmv_2026": 0.0,
            "gmv_2025": 0.0, "unit_2026": 0.0, "unit_2025": 0.0, "link_count": 0,
        })
        for year, frame in [(2025, df25), (2026, df26)]:
            for _, row in frame.iterrows():
                series, function_tag, source = _infer_series_from_title(row.get("product_title"), brand, llm_candidates)
                b = bucket[series]
                b["series"] = series
                b["function_tag"] = b["function_tag"] or function_tag
                b["source"] = b["source"] or source
                b[f"gmv_{year}"] += float(row.get("gmv") or 0)
                b[f"unit_{year}"] += float(row.get("unit") or 0)
                if year == 2026:
                    b["link_count"] += 1

        rows = []
        for item in bucket.values():
            g26, g25 = item["gmv_2026"], item["gmv_2025"]
            u26, u25 = item["unit_2026"], item["unit_2025"]
            rows.append({
                "product_line": item["series"],
                "series": item["series"],
                "function_tag": item["function_tag"],
                "source": item["source"],
                "gmv_2026": round(g26),
                "gmv_2025": round(g25),
                "unit_2026": round(u26),
                "unit_2025": round(u25),
                "atv_2026": round(g26 / u26, 2) if u26 else None,
                "atv_2025": round(g25 / u25, 2) if u25 else None,
                "weight": safe_div(g26, total_26),
                "evol": safe_evol(g26, g25),
                "share_delta": None if not total_26 or not total_25 else round(g26 / total_26 - g25 / total_25, 4),
                "link_count": item["link_count"],
            })
        rows = sorted(rows, key=lambda x: x["gmv_2026"], reverse=True)
        return {
            "brand": brand,
            "period": period,
            "category": category,
            "kol_driver": kol_driver,
            "link_type": link_type,
            "series": rows,
            "product_lines": rows,
            "llm_candidates": llm_candidates,
            "coverage": "series_map" if not low_coverage else "fallback",
        }
    except Exception as exc:
        return {"error": "execution_error", "message": str(exc)}
