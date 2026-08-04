from __future__ import annotations

from functools import lru_cache
import json
import math
from pathlib import Path


LABELS = {
    "month": "月份", "category": "品类", "key_driver": "Key Driver", "series": "系列",
    "sku": "SKU", "ait": "AIT", "platform": "平台", "bkfst": "BKFST", "kol_platform": "KOL平台",
    "tier": "Tier", "kol_type": "KOL Type", "kol": "KOL",
    "gmv_actual": "GMV Actual", "gmv_evol": "GMV Evol%", "unit_actual": "销量 Actual",
    "unit_evol": "销量 Evol%", "atv_actual": "客单价", "spend_actual": "媒体花费 Actual",
    "spend_evol": "花费 Evol%", "spend_weight": "花费 Wgt%", "spend_weight_change": "Wgt Change",
    "nso_actual": "NSO Actual", "nso_evol": "NSO Evol%", "fee_ratio": "媒体费比",
    "fee_ratio_change": "费比变化", "search_actual": "搜索量 Actual", "search_evol": "搜索量 Evol%",
    "cost_actual": "KOL花费 Actual", "cost_evol": "KOL花费 Evol%", "cost_weight": "花费 Wgt%",
    "cost_weight_change": "Wgt Change", "engage_actual": "Engage", "engage_evol": "Engage Evol%",
    "cpe": "CPE", "change_amount": "变化额", "growth_contribution": "增长贡献度",
    "decline_drag": "下滑拖累度", "alignment_signal": "投入与生意信号", "search_signal": "搜索与生意信号",
}

PCT_KEYS = {"gmv_evol", "unit_evol", "spend_evol", "spend_weight", "nso_evol", "fee_ratio", "search_evol", "cost_evol", "cost_weight", "engage_evol", "growth_contribution", "decline_drag"}
PP_KEYS = {"spend_weight_change", "cost_weight_change", "fee_ratio_change"}
AMOUNT_KEYS = {"gmv_actual", "spend_actual", "nso_actual", "cost_actual", "change_amount"}
INT_KEYS = {"unit_actual", "search_actual", "engage_actual"}

_ANALYSIS_CONTRACT = (
    Path(__file__).resolve().parent
    / "skills"
    / "followup"
    / "analysis-drill"
    / "contract.json"
)


@lru_cache(maxsize=1)
def _insight_rules() -> dict:
    defaults = {
        "dominant_share": 0.70,
        "leading_share": 0.50,
        "series_top_n": 2,
        "series_concentration_share": 0.60,
    }
    try:
        contract = json.loads(_ANALYSIS_CONTRACT.read_text(encoding="utf-8"))
        return {**defaults, **(contract.get("insight_rules") or {})}
    except Exception:
        return defaults


def _missing(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def _amount(value) -> str:
    if _missing(value):
        return "—"
    number = float(value)
    sign = "-" if number < 0 else ""
    number = abs(number)
    if number >= 1_000_000:
        return f"{sign}{number / 1_000_000:,.1f}M"
    if number >= 1_000:
        return f"{sign}{number / 1_000:,.1f}K"
    return f"{sign}{number:,.0f}"


def _format(key: str, value) -> str:
    if _missing(value):
        return "—"
    if key in PCT_KEYS:
        return f"{float(value) * 100:+.1f}%" if key.endswith("evol") else f"{float(value) * 100:.1f}%"
    if key in PP_KEYS:
        return f"{float(value) * 100:+.1f}pp"
    if key in AMOUNT_KEYS:
        return _amount(value)
    if key in INT_KEYS:
        return f"{int(round(float(value))):,}"
    if key == "atv_actual" or key == "cpe":
        return f"{float(value):,.2f}"
    return str(value)


def _number(value) -> float | None:
    if _missing(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _short_subject(plan: dict) -> str:
    filters = plan.get("filters") or {}
    value = next((value for value in filters.values() if value not in (None, "", [])), None)
    if value is None:
        return plan.get("brand") or "当前范围"
    text = str(value)
    return text.split("-")[-1] if "-" in text else text


def _scope_label(plan: dict) -> str:
    filters = plan.get("filters") or {}
    if filters.get("category"):
        return "该品类"
    if filters.get("key_driver"):
        return "该渠道"
    if filters.get("series"):
        return "该系列"
    if filters.get("function_tag"):
        return "该功能线"
    return "当前范围"


def _table_rows(tables: list[dict], title: str) -> list[dict]:
    table = next((item for item in tables if item.get("title") == title), None)
    return list((table or {}).get("rows") or [])


def _ec_drill_bullets(plan: dict, rows: list[dict], tables: list[dict]) -> list[str]:
    """Synthesize an EC drill bundle across summary, driver, and series tables."""
    if plan.get("domain") != "ec" or not rows or not tables:
        return []
    summary = rows[0]
    total_gmv = _number(summary.get("gmv_actual"))
    subject = _short_subject(plan)
    scope_label = _scope_label(plan)
    bullets = []

    if total_gmv is not None:
        sentence = f"{subject}的GMV为{_format('gmv_actual', total_gmv)}"
        if _number(summary.get("gmv_evol")) is not None:
            sentence += f"，同比{_format('gmv_evol', summary['gmv_evol'])}"
        bullets.append(sentence + "。")

    rules = _insight_rules()
    driver_rows = _table_rows(tables, "Key Driver结构")
    driver_values = [row for row in driver_rows if _number(row.get("gmv_actual")) is not None]
    if driver_values:
        top = max(driver_values, key=lambda row: _number(row.get("gmv_actual")) or 0)
        top_gmv = _number(top.get("gmv_actual")) or 0
        denominator = total_gmv or sum(_number(row.get("gmv_actual")) or 0 for row in driver_values)
        share = min(top_gmv / denominator, 1.0) if denominator else None
        driver = top.get("key_driver") or "头部渠道"
        if share is not None and share >= float(rules["dominant_share"]):
            wording = f"{subject}的生意主要由{driver}带来"
        elif share is not None and share >= float(rules["leading_share"]):
            wording = f"{subject}的生意以{driver}为主"
        else:
            wording = f"{driver}是{subject}贡献最高的Key Driver"
        detail = f"，贡献{_format('gmv_actual', top_gmv)}"
        if share is not None:
            detail += f"，占{scope_label}GMV的{share * 100:.1f}%"
        bullets.append(wording + detail + "。")

    series_rows = _table_rows(tables, "系列结构")
    series_values = [row for row in series_rows if _number(row.get("gmv_actual")) is not None]
    if series_values:
        ordered = sorted(series_values, key=lambda row: _number(row.get("gmv_actual")) or 0, reverse=True)
        top_n = max(1, int(rules["series_top_n"]))
        leaders = ordered[:top_n]
        leaders_gmv = sum(_number(row.get("gmv_actual")) or 0 for row in leaders)
        denominator = total_gmv or sum(_number(row.get("gmv_actual")) or 0 for row in series_values)
        share = min(leaders_gmv / denominator, 1.0) if denominator else None
        names = [str(row.get("series") or "其他系列") for row in leaders]
        joined_names = "和".join(names)
        if share is not None and share >= float(rules["series_concentration_share"]):
            bullets.append(
                f"系列上，{joined_names}合计贡献{_format('gmv_actual', leaders_gmv)}，"
                f"占{scope_label}GMV的{share * 100:.1f}%，生意较集中在这{len(leaders)}个系列。"
            )
        else:
            top = leaders[0]
            bullets.append(
                f"系列上，{top.get('series') or '其他系列'}贡献最高，"
                f"GMV为{_format('gmv_actual', top.get('gmv_actual'))}。"
            )
    return bullets[:3]


def _columns(rows: list[dict], requested: list[str]) -> list[str]:
    available = []
    for row in rows:
        for key in row:
            if key.startswith("_") or key.endswith("_prior") or key in {"gmv_diff", "weight_prior", "row_count"}:
                continue
            if key not in available:
                available.append(key)
    dims = [key for key in available if key in {"month", "category", "key_driver", "series", "sku", "ait", "platform", "bkfst", "kol_platform", "tier", "kol_type", "kol"}]
    if requested:
        metrics = [key for key in requested if key in available]
        if "change_amount" in available and "change_amount" not in metrics:
            metrics.append("change_amount")
        for key in ("growth_contribution", "decline_drag", "alignment_signal", "search_signal"):
            if key in available and key not in metrics:
                metrics.append(key)
    else:
        metrics = [key for key in available if key not in dims and key in LABELS]
    return dims + metrics


def _table(rows: list[dict], metrics: list[str]) -> str:
    if not rows:
        return "_指定范围内没有可用数据。_"
    columns = _columns(rows, metrics)
    header = "| " + " | ".join(LABELS.get(key, key) for key in columns) + " |"
    align = "|" + "|".join("---" if key in {"month", "category", "key_driver", "series", "sku", "ait", "platform", "bkfst", "kol_platform", "tier", "kol_type", "kol", "alignment_signal", "search_signal"} else "---:" for key in columns) + "|"
    body = ["| " + " | ".join(_format(key, row.get(key)) for key in columns) + " |" for row in rows]
    return "\n".join([header, align, *body])


def _analysis_bullets(rows: list[dict], mode: str) -> list[str]:
    if not rows:
        return []
    if mode == "trend_alignment":
        signals = [row for row in rows if row.get("alignment_signal") or row.get("search_signal")]
        return [f"{row['month']}：{row.get('alignment_signal') or row.get('search_signal')}。" for row in signals[:3]]
    actual_candidates = ("gmv_actual", "spend_actual", "cost_actual", "engage_actual", "search_actual")
    metric = next((key for key in actual_candidates if any(row.get(key) is not None for row in rows)), None)
    evol_key = next((key for key in ("gmv_evol", "spend_evol", "cost_evol", "engage_evol", "search_evol") if any(row.get(key) is not None for row in rows)), None)
    label_keys = ("category", "key_driver", "series", "sku", "ait", "platform", "bkfst", "tier", "kol_type", "kol", "month")
    label_key = next((key for key in label_keys if any(row.get(key) for row in rows)), None)
    bullets = []
    if metric and label_key:
        top = max(rows, key=lambda row: float(row.get(metric) or 0))
        bullets.append(f"{top.get(label_key)}的{LABELS.get(metric, metric)}最高，为{_format(metric, top.get(metric))}。")
    if mode == "change_attribution":
        drags = [row for row in rows if row.get("decline_drag")]
        growth = [row for row in rows if row.get("growth_contribution")]
        if growth and label_key:
            row = max(growth, key=lambda item: item["growth_contribution"])
            bullets.append(f"{row.get(label_key)}的正向变化贡献最大，占全部正向变化的{_format('growth_contribution', row['growth_contribution'])}。")
        if drags and label_key:
            row = max(drags, key=lambda item: item["decline_drag"])
            bullets.append(f"{row.get(label_key)}的下滑拖累最大，占全部负向变化的{_format('decline_drag', row['decline_drag'])}。")
    elif evol_key and label_key:
        comparable = [row for row in rows if row.get(evol_key) is not None]
        if comparable:
            fastest = max(comparable, key=lambda row: float(row[evol_key]))
            bullets.append(f"{fastest.get(label_key)}的同比变化最强，为{_format(evol_key, fastest[evol_key])}。")
    return bullets[:3]


def _filtered_summary_bullets(rows: list[dict], filters: dict) -> list[str]:
    if not rows:
        return []
    label = next((str(value) for value in filters.values() if value not in (None, "", [])), "TTL")
    row = rows[0]
    actual_key = next((key for key in ("gmv_actual", "spend_actual", "cost_actual", "search_actual", "engage_actual") if row.get(key) is not None), None)
    evol_key = next((key for key in ("gmv_evol", "spend_evol", "cost_evol", "search_evol", "engage_evol") if row.get(key) is not None), None)
    bullets = []
    if actual_key:
        bullets.append(f"{label}的{LABELS.get(actual_key, actual_key)}为{_format(actual_key, row[actual_key])}。")
    if evol_key:
        bullets.append(f"{label}的同比变化为{_format(evol_key, row[evol_key])}。")
    return bullets


def format_followup_result(plan: dict, result: dict) -> dict:
    if result.get("error"):
        return {"markdown": result.get("message") or "查询失败。", "meta": {"document_ready": False, "row_count": 0, "table_count": 0}}
    rows = result.get("rows") or []
    tables = result.get("tables") or []
    brand = plan["brand"]
    period = plan["period"]["raw"]
    scope = f"范围：{brand}，{period}。"
    parts = [scope]
    if plan["skill"] == "analysis_drill":
        bundle_bullets = _ec_drill_bullets(plan, rows, tables)
        bullets = bundle_bullets
        if not bullets:
            bullets = _analysis_bullets(rows, plan["mode"])
        if not bullets:
            bullets = _filtered_summary_bullets(rows, plan.get("filters") or {})
        if tables and not bundle_bullets:
            for table in tables:
                bullets.extend(_analysis_bullets(table.get("rows") or [], "performance")[:1])
        parts.extend(f"- {line}" for line in bullets[:3])
    if rows:
        parts.append(_table(rows, plan.get("metrics") or []))
    for table in tables:
        parts.append(f"### {table['title']}\n\n{_table(table.get('rows') or [], table.get('metrics') or [])}")
    if (
        "series" in plan.get("group_by", [])
        or plan.get("filters", {}).get("series")
        or any(table.get("title") == "系列结构" for table in tables)
    ):
        parts.append("_产品系列由AI根据产品链接归纳总结，存在误差。_")
    row_count = len(rows) + sum(len(table.get("rows") or []) for table in tables)
    table_count = (1 if rows else 0) + len(tables)
    # A drill bundle is designed as a small report. Even one detailed sub-table
    # belongs in a Feishu document instead of an oversized chat card.
    has_drill_bundle = plan["skill"] == "analysis_drill" and bool(tables)
    document_ready = has_drill_bundle or row_count > 20 or table_count > 1
    return {
        "markdown": "\n\n".join(parts),
        "meta": {
            "brand": brand, "period": period, "report_type": "followup_v2",
            "skill": plan["skill"], "mode": plan["mode"], "domain": plan["domain"],
            "row_count": row_count, "table_count": table_count, "document_ready": document_ready,
            "document_title": f"{brand} {period} 数据分析",
            "evidence": result.get("evidence") or [], "query_meta": result.get("query_meta") or {},
        },
    }
