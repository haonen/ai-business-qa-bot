from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import re
from functools import lru_cache

from bot.media_period import normalize_media_period_hint
from bot.media_period import parse_media_period
from bot.session import SessionState
from bot.utils import extract_json_object, llm_client, normalize_period_hint, parse_ec_period


log = logging.getLogger(__name__)
_SKILL_ROOT = Path(__file__).resolve().parent / "skills" / "followup"
_CONTRACTS = {
    "analysis_drill": _SKILL_ROOT / "analysis-drill" / "contract.json",
    "data_organizer": _SKILL_ROOT / "data-organizer" / "contract.json",
}

NARROW_HINTS = (
    "按月", "by month", "整理", "列出", "表格", "趋势", "排名", "top", "对比", "比较",
    "拖累", "贡献", "构成", "靠哪些", "性价比", "同步", "背离", "有没有涨",
)
BET_HINTS = ("媒体", "费比", "ait", "bkfs", "bkfst", "search", "搜索", "kol", "达人", "engage", "cpe", "投放")
EC_HINTS = ("生意", "gmv", "品类", "类目", "系列", "链接", "sku", "key driver", "渠道", "功能线")
EC_METRICS = {"gmv_actual", "gmv_evol", "unit_actual", "unit_evol", "atv_actual"}
BET_METRICS = {
    "spend_actual", "spend_evol", "spend_weight", "spend_weight_change",
    "nso_actual", "nso_evol", "fee_ratio", "fee_ratio_change",
    "search_actual", "search_evol", "cost_actual", "cost_evol",
    "cost_weight", "cost_weight_change", "engage_actual", "engage_evol", "cpe",
}


@dataclass
class FollowupPlan:
    skill: str
    domain: str
    mode: str
    brand: str
    period: dict[str, str]
    filters: dict[str, str | None] = field(default_factory=dict)
    group_by: list[str] = field(default_factory=list)
    metrics: list[str] = field(default_factory=list)
    comparison: str = "yoy"
    sort: dict[str, str] = field(default_factory=lambda: {"metric": "", "direction": "desc"})
    limit: int = 20

    def to_dict(self) -> dict:
        return {
            "skill": self.skill, "domain": self.domain, "mode": self.mode,
            "brand": self.brand, "period": self.period, "filters": self.filters,
            "group_by": self.group_by, "metrics": self.metrics,
            "comparison": self.comparison, "sort": self.sort, "limit": self.limit,
        }


def is_narrow_followup(text: str) -> bool:
    lowered = str(text or "").casefold()
    return any(h.casefold() in lowered for h in NARROW_HINTS)


def _context_payload(state: SessionState) -> dict:
    ec_cache = state.ec_context.report_cache or state.last_result_cache or {}
    categories = [
        row.get("category_cn")
        for row in ((ec_cache.get("category_result") or {}).get("categories") or [])
        if row.get("category_cn")
    ][:20]
    drivers = [
        row.get("key_driver")
        for row in (((ec_cache.get("driver_result") or {}).get("driver_summary") or {}).get("drivers") or [])
        if row.get("key_driver")
    ][:20]
    return {
        "ec": {
            "brand": state.ec_context.brand or state.drilldown_ctx.brand,
            "period": state.ec_context.period or state.drilldown_ctx.period,
            "filters": state.ec_context.filters,
            "available_categories": categories,
            "available_key_drivers": drivers,
        },
        "bet": {
            "brand": state.bet_context.brand,
            "period": state.bet_context.period,
            "filters": state.bet_context.filters,
        },
    }


@lru_cache(maxsize=1)
def _skill_guidance() -> str:
    parts = []
    for directory in ("analysis-drill", "data-organizer"):
        path = _SKILL_ROOT / directory / "SKILL.md"
        if path.exists():
            parts.append(path.read_text(encoding="utf-8"))
    return "\n\n".join(parts)


def _rule_plan(text: str, state: SessionState, brand: str | None, period: str | None) -> dict:
    lowered = text.casefold()
    has_bet = any(token in lowered for token in BET_HINTS)
    has_ec = any(token in lowered for token in EC_HINTS)
    domain = "ec_bet" if (has_bet and has_ec) or ("搜索" in text and "生意" in text) else ("bet" if has_bet else "ec")
    ctx = state.bet_context if domain == "bet" else state.ec_context
    resolved_brand = brand or ctx.brand or state.drilldown_ctx.brand or ""
    resolved_period = period or ctx.period or state.drilldown_ctx.period or ""
    skill = "data_organizer" if is_narrow_followup(text) else "analysis_drill"
    if "拖累" in text or "贡献" in text or "来源" in text:
        mode = "change_attribution"
    elif "对比" in text or "比较" in text or "哪个" in text:
        mode = "comparison"
    elif domain == "ec_bet" or ("同步" in text or "背离" in text):
        mode = "trend_alignment"
    elif "构成" in text or "靠哪些" in text or "结构" in text:
        mode = "composition"
    elif "排名" in text or "top" in lowered:
        mode = "ranking"
    elif "按月" in text or "by month" in lowered or "趋势" in text:
        mode = "monthly_trend"
    elif skill == "data_organizer":
        mode = "period_summary"
    else:
        mode = "performance"
    group_by = ["month"] if mode in {"monthly_trend", "trend_alignment"} else []
    filters = {}
    for label, pattern in (("tier", r"(?<![A-Za-z0-9])(T[1-5]|KOC)(?![A-Za-z0-9])"), ("ait", r"(?<![A-Za-z])(Awareness|Influencer|Transaction)(?![A-Za-z])")):
        match = re.search(pattern, text, flags=re.I)
        if match:
            filters[label] = match.group(1)
    tiers = re.findall(r"(?<![A-Za-z0-9])(T[1-5]|KOC)(?![A-Za-z0-9])", text, flags=re.I)
    if domain == "ec" and tiers:
        filters.pop("tier", None)
        filters["key_driver"] = tiers[0].upper()
    elif len(tiers) > 1:
        filters["tier"] = [value.upper() for value in dict.fromkeys(tiers)]
    if "小红书" in text or "RED" in text.upper():
        filters["platform"] = "red"
    elif "抖音" in text:
        filters["platform"] = "douyin"
    if domain == "ec_bet":
        metrics = ["gmv_actual", "gmv_evol"]
        if "搜索" in text:
            metrics.extend(["search_actual", "search_evol"])
        elif "费比" in text:
            metrics.extend(["fee_ratio", "fee_ratio_change"])
        else:
            metrics.extend(["spend_actual", "spend_evol"])
    elif "费比" in text:
        metrics = ["fee_ratio", "fee_ratio_change"]
        if group_by == ["month"] and re.search(r"(?<![A-Za-z])(Awareness|Influencer|Transaction)(?![A-Za-z])", text, re.I):
            group_by.append("ait")
    elif "搜索" in text:
        metrics = ["search_actual", "search_evol"]
    elif "cpe" in lowered or "性价比" in text:
        metrics = ["cost_actual", "cost_weight", "engage_actual", "cpe"]
    else:
        metrics = ["gmv_actual", "gmv_evol"] if domain == "ec" else ["spend_actual", "spend_evol"]
    if domain == "ec":
        cache = state.ec_context.report_cache or state.last_result_cache or {}
        categories = ((cache.get("category_result") or {}).get("categories") or [])
        for item in categories:
            candidate = str(item.get("category_cn") or "")
            short = candidate.split("-")[-1]
            if candidate and (candidate in text or short in text):
                filters["category"] = candidate
                break
        if "品类" in text and mode in {"composition", "change_attribution", "ranking"} and "category" not in filters:
            group_by = ["category"]
        if any(token in text.casefold() for token in ("链接", "sku", "top链接")):
            group_by = ["sku"]
        elif filters.get("key_driver") and any(token in text for token in ("哪些品类", "什么品类", "品类结构")):
            group_by = ["category"]
    else:
        if "bkfs" in lowered or "bkfst" in lowered:
            group_by = (["month", "bkfst"] if "month" in group_by else ["bkfst"])
            metrics = ["spend_weight", "spend_weight_change"]
        elif ("性价比" in text or "cpe" in lowered) and tiers:
            group_by = ["tier"]
        elif filters.get("ait") and "month" in group_by and "ait" not in group_by:
            group_by.append("ait")
    return {
        "skill": skill, "domain": domain, "mode": mode, "brand": resolved_brand,
        "period": {"raw": resolved_period}, "filters": filters,
        "group_by": group_by, "metrics": metrics, "comparison": "yoy",
        "sort": {"metric": metrics[0] if metrics else "", "direction": "desc"},
        "limit": 50 if mode == "cross_table" or len(group_by) == 2 else 20,
    }


def _llm_plan(text: str, state: SessionState, brand: str | None, period: str | None) -> dict | None:
    if not os.environ.get("DASHSCOPE_API_KEY"):
        return None
    prompt = f"""
你只负责把业务问题转成分析计划，不能生成SQL或Tool调用。
当前上下文：{json.dumps(_context_payload(state), ensure_ascii=False)}
入口已抽取：brand={brand or 'null'}，period={period or 'null'}
用户问题：{text}

必须遵守以下Skill业务方法：
{_skill_guidance()}

只返回JSON，字段严格为：skill,domain,mode,brand,period,filters,group_by,metrics,comparison,sort,limit。
skill只能analysis_drill或data_organizer；domain只能ec/bet/ec_bet。
分析mode：performance/composition/change_attribution/comparison/trend_alignment；
整理mode：period_summary/monthly_trend/cross_table/ranking。
group_by只能month/category/key_driver/series/sku/ait/platform/bkfst/kol_platform/tier/kol_type/kol。
filters只能category/key_driver/series/function_tag/platform/ait/bkfst/tier/kol_type。
metrics只能gmv_actual/gmv_evol/unit_actual/unit_evol/atv_actual/spend_actual/spend_evol/spend_weight/spend_weight_change/nso_actual/nso_evol/fee_ratio/fee_ratio_change/search_actual/search_evol/cost_actual/cost_evol/cost_weight/cost_weight_change/engage_actual/engage_evol/cpe。
不要补造品牌或时间；缺失就空字符串。用户说按月/整理/列出/表格/趋势/排名/对比时优先data_organizer，EC与BET同月对照用analysis_drill+ec_bet+trend_alignment。
"""
    try:
        response = llm_client(max_retries=0).chat.completions.create(
            model=os.environ.get("DASHSCOPE_ROUTER_MODEL", "qwen3.7-plus"),
            messages=[{"role": "user", "content": prompt}], temperature=0,
            max_tokens=500, timeout=float(os.environ.get("FOLLOWUP_PLAN_TIMEOUT", "12")),
            response_format={"type": "json_object"}, extra_body={"enable_thinking": False},
        )
        return extract_json_object(response.choices[0].message.content or "") or None
    except Exception as exc:
        log.warning("[followup_v2] planner failed: %s", exc)
        return None


def _load_contract(skill: str) -> dict:
    path = _CONTRACTS.get(skill)
    if not path:
        raise ValueError("未知追问Skill。")
    return json.loads(path.read_text(encoding="utf-8"))


def _normalize_name(value: str) -> str:
    return re.sub(r"[\s/／\\\-_（）()]+", "", str(value or "")).casefold()


def _resolve_ec_category(value, state: SessionState):
    if not value or isinstance(value, list):
        return value
    cache = state.ec_context.report_cache or state.last_result_cache or {}
    categories = ((cache.get("category_result") or {}).get("categories") or [])
    requested = _normalize_name(str(value))
    matches = []
    for item in categories:
        full = str(item.get("category_cn") or "")
        short = full.split("-")[-1]
        candidates = {_normalize_name(full), _normalize_name(short)}
        if requested in candidates or any(requested and requested in candidate for candidate in candidates):
            matches.append(full)
    unique = list(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else value


def validate_plan(raw: dict, state: SessionState, *, brand: str | None = None, period: str | None = None) -> FollowupPlan:
    skill = str(raw.get("skill") or "analysis_drill")
    mode = str(raw.get("mode") or "performance")
    if mode in {"performance", "composition", "change_attribution", "comparison", "trend_alignment"}:
        skill = "analysis_drill"
    elif mode in {"period_summary", "monthly_trend", "cross_table", "ranking"}:
        skill = "data_organizer"
    domain = str(raw.get("domain") or "ec")
    requested_metric_names = {str(value) for value in (raw.get("metrics") or [])}
    has_ec_metrics = bool(requested_metric_names & EC_METRICS)
    has_bet_metrics = bool(requested_metric_names & BET_METRICS)
    # Metric ownership is deterministic. Never let an LLM domain mistake turn a
    # BET metric into a product-link/EC query (or vice versa).
    if not (mode == "trend_alignment" and domain == "ec_bet"):
        if has_ec_metrics and has_bet_metrics:
            domain = "ec_bet"
        elif has_bet_metrics:
            domain = "bet"
        elif has_ec_metrics:
            domain = "ec"
    contract = _load_contract(skill)
    if domain not in contract["domains"] or mode not in contract["modes"]:
        raise ValueError("追问模式与Skill契约不匹配。")
    ctx = state.bet_context if domain == "bet" else state.ec_context
    resolved_brand = str(raw.get("brand") or brand or ctx.brand or state.drilldown_ctx.brand or "").strip()
    raw_period = raw.get("period") or {}
    if isinstance(raw_period, dict):
        period_value = raw_period.get("raw")
        if not period_value and raw_period.get("start") and raw_period.get("end"):
            period_value = f"{raw_period['start']}~{raw_period['end']}"
    else:
        period_value = raw_period
    period_text = str(period_value or period or ctx.period or state.drilldown_ctx.period or "").strip()
    if not resolved_brand:
        raise ValueError("missing_brand")
    if not period_text:
        raise ValueError("missing_period")
    allowed_filters = {"category", "key_driver", "series", "function_tag", "platform", "ait", "bkfst", "tier", "kol_type"}
    filters = {k: v for k, v in dict(raw.get("filters") or {}).items() if k in allowed_filters and v not in (None, "")}
    if domain == "ec" and filters.get("category"):
        filters["category"] = _resolve_ec_category(filters["category"], state)
    group_aliases = {
        "月份": "month",
        "month": "month",
        "period": None,
        "time_period": None,
        "date_range": None,
        "期间": None,
        "时间段": None,
    }
    group_by = []
    for value in (raw.get("group_by") or []):
        name = str(value).strip()
        normalized = group_aliases.get(name.casefold(), name)
        if normalized and normalized not in group_by:
            group_by.append(normalized)
    allowed_groups = set(contract.get("group_by", {}).get(domain, [])) if isinstance(contract.get("group_by"), dict) else {"month", "category", "key_driver", "series", "sku", "ait", "platform", "bkfst", "tier", "kol_type", "kol"}
    if any(value not in allowed_groups for value in group_by) or len(group_by) > int(contract.get("max_group_by", 2)):
        raise ValueError("不支持的分析维度组合。")
    if len(group_by) == 2 and group_by not in contract.get("allowed_pairs", []) and list(reversed(group_by)) not in contract.get("allowed_pairs", []):
        raise ValueError("该交叉维度未开放。")
    if "sku" in group_by and not any(filters.get(k) for k in ("category", "key_driver", "series", "function_tag")):
        raise ValueError("SKU必须带品类、渠道、系列或功能线筛选。")
    allowed_metrics = EC_METRICS | BET_METRICS
    contract_metrics = set((contract.get("metrics") or {}).get(domain, allowed_metrics))
    metrics = [str(v) for v in raw.get("metrics") or [] if str(v) in allowed_metrics and str(v) in contract_metrics]
    if requested_metric_names & allowed_metrics and not metrics:
        raise ValueError("请求指标与分析领域不匹配。")
    if not metrics:
        if domain == "ec":
            metrics = ["gmv_actual", "gmv_evol"]
        elif domain == "bet" and any(dimension in group_by for dimension in ("kol_platform", "tier", "kol_type", "kol")):
            metrics = ["cost_actual", "cost_evol", "engage_actual", "engage_evol", "cpe"]
        elif domain == "bet":
            metrics = ["spend_actual", "spend_evol"]
    if any(metric in {"fee_ratio", "fee_ratio_change"} for metric in metrics):
        if any(dimension not in {"month", "ait"} for dimension in group_by):
            raise ValueError("媒体费比仅支持TTL或AIT口径，不计算交易平台费比。")
    default_limit = 50 if mode == "cross_table" or len(group_by) == 2 else 20
    limit = max(1, min(int(raw.get("limit") or default_limit), int(contract.get("max_rows", 50))))
    sort = dict(raw.get("sort") or {})
    if sort.get("metric") not in allowed_metrics:
        sort["metric"] = metrics[0] if metrics else ""
    sort["direction"] = "asc" if sort.get("direction") == "asc" else "desc"
    try:
        if domain in {"bet", "ec_bet"}:
            parsed_period = parse_media_period(period_text)
            period_object = {"raw": period_text, "start": parsed_period.focus_start, "end": parsed_period.focus_end}
        else:
            parsed_period = parse_ec_period(period_text, 2026)
            period_object = {"raw": period_text, "start": parsed_period["current_start"], "end": parsed_period["current_end"]}
    except ValueError:
        period_object = {"raw": period_text, "start": "", "end": ""}
    return FollowupPlan(skill, domain, mode, resolved_brand, period_object, filters, group_by, metrics, str(raw.get("comparison") or "yoy"), sort, limit)


def build_followup_plan(text: str, state: SessionState, *, brand: str | None = None, period: str | None = None) -> FollowupPlan:
    llm_raw = _llm_plan(text, state, brand, period)
    if llm_raw:
        try:
            return validate_plan(llm_raw, state, brand=brand, period=period)
        except ValueError as exc:
            log.warning(
                "[followup_v2] invalid llm plan, falling back to rules: error=%s plan=%s",
                exc,
                llm_raw,
            )
    rule_raw = _rule_plan(text, state, brand, period)
    return validate_plan(rule_raw, state, brand=brand, period=period)
