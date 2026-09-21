from __future__ import annotations

"""Structured semantic goals for evidence-grounded Agent Reasoning V3.

Entities are already locked by the deterministic resolver before this module
runs.  The LLM may select goals, capabilities and Skills, but cannot create or
replace brand, period or platform values.
"""

from dataclasses import asdict, dataclass, field
import json
import logging
import os
import re
from typing import Any

from bot.routing_contracts import CAPABILITY_REGISTRY
from bot.utils import extract_json_object, llm_client, llm_model, llm_plan_extra_body


log = logging.getLogger(__name__)

ANALYSIS_GOALS = {
    "DESCRIBE_BUSINESS",
    "EXPLAIN_CHANGE",
    "COMPARE",
    "DRILL_DOWN",
    "FIND_OPPORTUNITIES",
    "RECOMMEND_ACTIONS",
}

SKILLS = {
    "analysis-drill",
    "opportunity-insight",
    "category-drill",
    "key-driver",
    "sku-investigation",
    "media-analysis",
    "market-analysis",
}


def reasoning_v3_enabled() -> bool:
    return os.environ.get("AGENT_REASONING_V3_ENABLED", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def reasoning_v3_shadow_enabled() -> bool:
    return os.environ.get("AGENT_REASONING_V3_SHADOW", "1").strip().lower() in {
        "1", "true", "yes", "on",
    }


@dataclass(frozen=True)
class SemanticAnalysisRequest:
    original_question: str
    current_turn: str
    locked_scope: dict[str, Any]
    analysis_goals: tuple[str, ...]
    requested_dimensions: tuple[str, ...]
    requested_outputs: tuple[str, ...]
    candidate_capabilities: tuple[str, ...]
    selected_capabilities: tuple[str, ...]
    selected_skills: tuple[str, ...]
    missing_slots: tuple[str, ...] = ()
    response_strategy: str = "TARGETED_ANSWER"
    reasoning_mode: str = "evidence_grounded"
    source: str = "deterministic"

    def to_dict(self) -> dict:
        value = asdict(self)
        for key in (
            "analysis_goals", "requested_dimensions", "requested_outputs",
            "candidate_capabilities", "selected_capabilities", "selected_skills", "missing_slots",
        ):
            value[key] = list(value[key])
        return value


def _platform_capability(platform: str | None) -> str | None:
    return {
        "TM": "tm_brand_business",
        "DY": "dy_brand_business",
        "JD": "jd_brand_business",
        "TTL": "ttl_brand_business",
    }.get(str(platform or "").upper())


def _deterministic_semantics(route_result: Any, question: str) -> dict:
    lowered = str(question or "").casefold()
    decision = getattr(route_result, "route_decision", None) or {}
    patch = decision.get("task_context_patch") or {}
    goals = list(patch.get("goals") or [])
    if any(token in lowered for token in ("生意", "表现", "经营", "gmv", "销售")):
        goals.append("DESCRIBE_BUSINESS")
    if any(token in lowered for token in ("为什么", "原因", "驱动", "解释")):
        goals.append("EXPLAIN_CHANGE")
    if any(token in lowered for token in ("比较", "对比", "哪个", "最高", "最大")):
        goals.append("COMPARE")
    if any(token in lowered for token in ("下钻", "系列", "sku", "链接", "商品", "品类", "key driver")):
        goals.append("DRILL_DOWN")
    if any(token in lowered for token in ("机会点", "增长机会", "增长点", "潜力", "值得关注")):
        goals.append("FIND_OPPORTUNITIES")
    if any(token in lowered for token in (
        "行动方案", "怎么提升", "如何提升", "如何增长", "怎么优化", "建议", "下一步怎么做",
    )):
        goals.append("RECOMMEND_ACTIONS")
    goals = list(dict.fromkeys(goal for goal in goals if goal in ANALYSIS_GOALS))
    if not goals:
        goals = ["DESCRIBE_BUSINESS"]

    dimensions = []
    for dimension, pattern in (
        ("category", r"品类|类目"),
        ("key_driver", r"key\s*driver|渠道|李佳琦|t2|non[-\s]*kol|自播|直播|商品卡"),
        ("series", r"系列"),
        ("sku", r"sku|链接|商品|产品"),
        ("platform", r"平台|天猫|抖音|京东|三平台"),
        ("investment", r"bet|媒体投资|媒体花费|投放"),
    ):
        if re.search(pattern, lowered, re.I):
            dimensions.append(dimension)

    platform = getattr(route_result, "platform", None)
    base_capability = _platform_capability(platform)
    candidates = [name for name in (base_capability, "opportunity_insight") if name]
    selected = list(candidates)
    skills = ["analysis-drill"]
    if "FIND_OPPORTUNITIES" in goals or "RECOMMEND_ACTIONS" in goals:
        skills.append("opportunity-insight")
    if "category" in dimensions:
        skills.append("category-drill")
    if "key_driver" in dimensions:
        skills.append("key-driver")
    if "sku" in dimensions:
        skills.append("sku-investigation")
    outputs = ["direct_answer", "evidence_summary"]
    if "FIND_OPPORTUNITIES" in goals:
        outputs.append("opportunities")
    if "RECOMMEND_ACTIONS" in goals:
        outputs.extend(["action_hypotheses", "validation_plan"])
    missing = [
        name for name, value in (
            ("brand", getattr(route_result, "brand", None)),
            ("period", getattr(route_result, "period", None)),
            ("platform", platform),
        ) if not value
    ]
    return {
        "analysis_goals": goals,
        "requested_dimensions": dimensions,
        "requested_outputs": list(dict.fromkeys(outputs)),
        "candidate_capabilities": candidates,
        "selected_capabilities": selected,
        "selected_skills": list(dict.fromkeys(skills)),
        "missing_slots": missing,
    }


def _llm_semantics(question: str, scope: dict, baseline: dict) -> dict | None:
    if not reasoning_v3_enabled() or not os.environ.get("DASHSCOPE_API_KEY"):
        return None
    capability_summaries = {
        name: {
            "platforms": list(spec.platforms),
            "dimensions": list(spec.dimensions),
            "limitations": list(spec.limitations),
        }
        for name, spec in CAPABILITY_REGISTRY.items()
        if name in set(baseline["candidate_capabilities"])
    }
    prompt = f"""你是生意分析Agent的语义规划器。完整用户问题：
{question}

已经由确定性实体解析器锁定的范围（不可修改）：
{json.dumps(scope, ensure_ascii=False)}

可选分析目标：{sorted(ANALYSIS_GOALS)}
可选Skills：{sorted(SKILLS)}
可选Capabilities：
{json.dumps(capability_summaries, ensure_ascii=False)}

请识别用户真正要完成的所有目标。只能选择上述枚举；不能创造品牌、时间、平台、SQL或工具。
返回JSON：{{"analysis_goals":[],"requested_dimensions":[],"requested_outputs":[],
"selected_capabilities":[],"selected_skills":[]}}"""
    try:
        client = llm_client(max_retries=0)
        response = client.chat.completions.create(
            model=llm_model("router"),
            messages=[{"role": "user", "content": prompt}],
            max_tokens=int(os.environ.get("PLAN_LLM_MAX_TOKENS", "3000")),
            timeout=float(os.environ.get("PLAN_LLM_TIMEOUT", "30")),
            response_format={"type": "json_object"},
            extra_body=llm_plan_extra_body(),
        )
        return extract_json_object(response.choices[0].message.content or "")
    except Exception as exc:
        log.warning("[reasoning_v3] semantic planner failed: %s", exc)
        return None


def build_semantic_analysis_request(route_result: Any, question: str) -> SemanticAnalysisRequest:
    original = str(getattr(route_result, "original_text", None) or question or "")
    scope = {
        "brand": getattr(route_result, "brand", None),
        "period": getattr(route_result, "period", None),
        "platform": getattr(route_result, "platform", None),
        "segment": getattr(route_result, "segment", None),
        "category": getattr(route_result, "category", None),
    }
    baseline = _deterministic_semantics(route_result, original)
    proposed = _llm_semantics(original, scope, baseline)
    source = "deterministic"
    if isinstance(proposed, dict):
        goals = [goal for goal in proposed.get("analysis_goals") or [] if goal in ANALYSIS_GOALS]
        capabilities = [
            name for name in proposed.get("selected_capabilities") or []
            if name in baseline["candidate_capabilities"] and name in CAPABILITY_REGISTRY
        ]
        skills = [name for name in proposed.get("selected_skills") or [] if name in SKILLS]
        if goals:
            baseline["analysis_goals"] = list(dict.fromkeys(goals))
        if capabilities:
            baseline["selected_capabilities"] = list(dict.fromkeys(capabilities))
        if skills:
            baseline["selected_skills"] = list(dict.fromkeys(skills))
        for key in ("requested_dimensions", "requested_outputs"):
            values = [str(value) for value in proposed.get(key) or [] if str(value).strip()]
            if values:
                baseline[key] = list(dict.fromkeys(values))
        source = "llm_validated"
    multiple = len(baseline["analysis_goals"]) > 1 or any(
        goal in baseline["analysis_goals"] for goal in ("FIND_OPPORTUNITIES", "RECOMMEND_ACTIONS")
    )
    return SemanticAnalysisRequest(
        original_question=original,
        current_turn=str(question or ""),
        locked_scope=scope,
        analysis_goals=tuple(baseline["analysis_goals"]),
        requested_dimensions=tuple(baseline["requested_dimensions"]),
        requested_outputs=tuple(baseline["requested_outputs"]),
        candidate_capabilities=tuple(baseline["candidate_capabilities"]),
        selected_capabilities=tuple(baseline["selected_capabilities"]),
        selected_skills=tuple(baseline["selected_skills"]),
        missing_slots=tuple(baseline["missing_slots"]),
        response_strategy="MULTI_STEP_ANALYSIS" if multiple else "TARGETED_ANSWER",
        source=source,
    )
