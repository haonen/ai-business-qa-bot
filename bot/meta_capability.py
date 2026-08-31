from __future__ import annotations

import os
import re

from bot.routing_contracts import CAPABILITY_REGISTRY, CapabilitySpec
from bot.session import SessionState


PLATFORM_LABELS = {"TM": "天猫", "DY": "抖音", "JD": "京东", "TTL": "三平台"}
DIMENSION_LABELS = {
    "overall": "整体生意", "platform": "平台比较", "category": "品类",
    "key_driver": "Key Driver", "series": "系列", "sku": "SKU/Top商品",
    "product_title": "商品标题", "product_link": "商品链接", "month": "月度趋势",
}


def dynamic_meta_enabled() -> bool:
    return os.environ.get("DYNAMIC_META_CAPABILITY_ENABLED", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _unified_entry_enabled() -> bool:
    return os.environ.get("UNIFIED_RESPONSE_STRATEGY_ENABLED", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _entry_copy() -> str:
    if _unified_entry_enabled():
        return "你不需要区分“模板”或“追问”，直接描述问题即可。"
    return "当前统一自然语言入口仍在灰度；显式“追问：”与已支持的上下文省略问法都可继续使用。"


def classify_meta_question(text: str, state: SessionState | None = None) -> str:
    value = str(text or "").casefold()
    if any(token in value for token in ("数据最新", "更新到什么时候", "最新到几月", "数据到几月")):
        return "DATA_AVAILABILITY"
    if any(token in value for token in ("接下来还能", "还能继续", "下一步能", "还能看什么")):
        return "CURRENT_CONTEXT_NEXT_STEPS"
    if any(token in value for token in ("限制", "不支持", "不能做", "能不能", "能否")):
        return "LIMITATION"
    if any(token in value for token in ("怎么用", "怎么问", "如何使用", "例子")):
        return "USAGE_GUIDE"
    if any(token in value for token in ("key driver", "sku", "品类", "系列", "链接", "商品")):
        return "DIMENSION_CAPABILITY"
    if any(token in value for token in ("天猫", "抖音", "京东", "三平台", "bet", "媒体")):
        return "PLATFORM_CAPABILITY"
    return "CAPABILITY_OVERVIEW"


def _flag_enabled(spec: CapabilitySpec) -> bool:
    if not spec.enabled_flag:
        return True
    default = "1" if spec.enabled_flag == "FOLLOWUP_SKILL_V2_ENABLED" else "0"
    return os.environ.get(spec.enabled_flag, default).strip().lower() in {"1", "true", "yes", "on"}


def _platform_from_text(text: str) -> str | None:
    value = str(text or "").casefold()
    if "天猫" in value or "tmall" in value:
        return "TM"
    if "抖音" in value or "douyin" in value:
        return "DY"
    if "京东" in value or re.search(r"(?<![a-z])jd(?![a-z])", value):
        return "JD"
    if "三平台" in value or "全平台" in value:
        return "TTL"
    return None


def _ec_spec(platform: str) -> CapabilitySpec:
    key = {"TM": "tm_brand_business", "DY": "dy_brand_business", "JD": "jd_brand_business", "TTL": "ttl_brand_business"}[platform]
    return CAPABILITY_REGISTRY[key]


def _effective_dimensions(platform: str, spec: CapabilitySpec) -> tuple[str, ...]:
    dimensions = list(spec.dimensions)
    if platform == "DY" and os.environ.get("DOUYIN_PRODUCT_DAILY_V2_ENABLED", "0").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        dimensions = [item for item in dimensions if item not in {"series", "sku", "product_title", "product_link"}]
    return tuple(dimensions)


def _dimension_from_text(text: str) -> str | None:
    value = str(text or "").casefold()
    for dimension, patterns in (
        ("key_driver", ("key driver", "keydriver", "driver")),
        ("sku", ("sku", "商品", "单品")),
        ("product_link", ("链接",)),
        ("series", ("系列",)),
        ("category", ("品类", "类目")),
        ("platform", ("平台比较",)),
    ):
        if any(pattern in value for pattern in patterns):
            return dimension
    return None


def _latest_evidence(state: SessionState) -> dict:
    evidence = list(state.ec_context.recent_evidence or [])
    for item in reversed(evidence):
        if isinstance(item, dict) and (
            item.get("schema_version") or item.get("available_dimensions")
        ):
            return item
    cache = state.ec_context.report_cache or state.last_result_cache or {}
    report = dict(cache.get("ec_report_evidence") or {})
    return report or (evidence[-1] if evidence and isinstance(evidence[-1], dict) else {})


def _context_next_steps(state: SessionState) -> str:
    evidence = _latest_evidence(state)
    brand = evidence.get("brand") or state.ec_context.brand
    platform = evidence.get("platform") or state.ec_context.platform
    period = (
        (evidence.get("period") or {}).get("raw") or state.ec_context.period
        if isinstance(evidence.get("period"), dict) else state.ec_context.period
    )
    if not brand or not platform:
        return "目前还没有一份可继续下钻的生意报告。你可以直接告诉我品牌、时间和平台，我会自动决定跑完整报告还是定向分析。"
    spec = _ec_spec(platform)
    available = set(evidence.get("available_dimensions") or spec.dimensions)
    suggestions: list[str] = []
    selected = evidence.get("selected_category")
    if selected and "series" in available:
        suggestions.append(f"继续看{selected}的系列和Top商品")
    if "key_driver" in available:
        suggestions.append("比较各Key Driver的增长额和贡献")
    if "product_title" in available or "product_link" in available:
        suggestions.append("下钻表现最好或拖累最大的商品标题与链接")
    if platform == "TTL":
        suggestions.append("比较三平台并下钻增长额最大的平台")
    suggestions = suggestions[:3] or ["查看整体、品类或月度趋势"]
    scope = f"{brand}、{period or '原报告时间'}、{PLATFORM_LABELS.get(platform, platform)}"
    return f"我已经记住当前范围：{scope}。\n\n你可以直接继续问：\n\n" + "\n".join(f"- {item}。" for item in suggestions)


def _platform_answer(platform: str, text: str) -> str:
    spec = _ec_spec(platform)
    label = PLATFORM_LABELS[platform]
    effective_dimensions = _effective_dimensions(platform, spec)
    requested = _dimension_from_text(text)
    if requested:
        supported = requested in effective_dimensions and requested not in spec.unsupported
        dimension_label = DIMENSION_LABELS.get(requested, requested)
        if supported:
            answer = f"可以，{label}支持{dimension_label}分析。"
        else:
            answer = f"目前不支持{label}的{dimension_label}分析，也不会降级去查其他平台。"
        if spec.limitations:
            answer += "\n\n边界：" + "；".join(spec.limitations) + "。"
        return answer
    dimensions = "、".join(DIMENSION_LABELS.get(item, item) for item in effective_dimensions)
    examples = "\n".join(f"- {example}" for example in spec.example_questions)
    answer = (
        f"{label}目前可分析：{dimensions}。\n\n"
        f"{_entry_copy()}\n\n{examples}"
    )
    if platform == "DY" and os.environ.get("DOUYIN_FOLLOWUP_TOOL_ENABLED", "0").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        answer += "\n\n注：抖音明细追加查询开关当前未启用；已有报告证据仍可直接回答。"
    return answer


def _overview() -> str:
    enabled = {name: spec for name, spec in CAPABILITY_REGISTRY.items() if _flag_enabled(spec)}
    areas = [
        "品牌生意：天猫、抖音、京东和三平台整体分析",
        "市场与排名：大盘趋势、Top品牌和平台比较",
        "BET媒体投资：花费、费比、BKFS及受支持的媒体维度",
        "定向下钻：品类、Key Driver、系列、商品和月度趋势",
        "复杂分析：先排名或比较，再自动选择对象继续下钻",
    ]
    if "followup" not in enabled:
        areas[-2] += "（当前定向分析开关未启用）"
    return (
        f"{_entry_copy()}\n\n"
        + "\n".join(f"- {item}。" for item in areas)
        + "\n\n例如：\n\n"
        + "- 珀莱雅2026年6月抖音生意怎么样？\n"
        + "- 防晒品类增长不错，再看系列和Top商品。\n"
        + "- 找Pure Mass Top3品牌，比较三平台后下钻各自增长额最大的平台。"
    )


def render_meta_answer(text: str, state: SessionState) -> dict:
    subtype = classify_meta_question(text, state)
    if subtype == "CURRENT_CONTEXT_NEXT_STEPS":
        markdown = _context_next_steps(state)
    else:
        platform = _platform_from_text(text)
        if platform:
            markdown = _platform_answer(platform, text)
        elif "bet" in str(text).casefold() or "媒体" in text:
            spec = CAPABILITY_REGISTRY["overall_bet"]
            markdown = "BET可分析媒体花费、同比、费比、BKFS和部分渠道结构。\n\n边界：" + "；".join(spec.limitations) + "。"
        else:
            markdown = _overview()
    return {
        "markdown": markdown,
        "meta": {
            "document_ready": False,
            "meta_subtype": subtype,
            "requires_data": False,
            "capability_source": "registry",
        },
    }
