from __future__ import annotations

"""Two-stage, capability-gated LLM planner."""

import json
import logging
import os
import time
from typing import Any

from bot.utils import extract_json_object, llm_client, llm_model, llm_plan_extra_body


log = logging.getLogger(__name__)


CAPABILITIES: dict[str, dict[str, Any]] = {
    "template.tm_business": {"executor": "tmall_business_template", "kind": "TEMPLATE", "schema": "ec-report-evidence.v2"},
    "template.dy_business": {"executor": "douyin_business_template", "kind": "TEMPLATE", "schema": "ec-report-evidence.v2"},
    "template.jd_business": {"executor": "jd_business_template", "kind": "TEMPLATE", "schema": "ec-report-evidence.v2"},
    "template.ttl_business": {"executor": "three_platform_business_template", "kind": "TEMPLATE", "schema": "ec-report-evidence.v2"},
    "template.bet": {"executor": "bet_investment_template", "kind": "TEMPLATE", "schema": "bet-report.v1"},
    "market.trend": {"executor": "market_summary", "kind": "QUERY", "schema": "market_trend.v1"},
    "market.top_brands": {"executor": "market_brand_ranking", "kind": "QUERY", "schema": "market_ranking.v1"},
    "market.brand_platform_matrix": {"executor": "market_brand_platform_matrix", "kind": "QUERY", "schema": "brand_platform_matrix.v1"},
    "brand.three_platform_matrix": {"executor": "brand_three_platform_matrix", "kind": "QUERY", "schema": "brand_platform_matrix.v1"},
    "derive.per_group_argmax": {"executor": "derive_per_group_argmax", "kind": "DERIVE", "schema": "brand_platform_selection.v1"},
    "template.brand_platform_business": {"executor": "market_brand_platform_template_fanout", "kind": "TEMPLATE", "schema": "brand_platform_reports.v1"},
    "template.overall_bet": {"executor": "market_brand_bet_fanout", "kind": "TEMPLATE", "schema": "brand_bet_reports.v1"},
    "followup.answer": {"executor": "followup_tool_bundle", "kind": "QUERY", "schema": "ec-followup-answer.v2"},
    "synthesis.grounded_answer": {"executor": "synthesize_evidence", "kind": "SYNTHESIS", "schema": "answer-envelope.v1"},
}

MODEL_INPUT_KEYS = {
    "operator", "input_ref", "expected_groups_ref", "group_by", "rank_by",
    "supported_values", "brands_ref", "filters", "metrics", "sort", "limit",
    "view", "requested_dimensions",
}
MODEL_INPUT_ALIASES = {"dimensions": "requested_dimensions"}
LOCKED_SCOPE_ECHO_KEYS = {"focus_period", "comparison_periods"}
LOCKED_INPUT_KEYS = {"brand", "period", "platform", "segment", "category"}
CAPABILITY_OUTPUT_PATHS = {
    "market.top_brands": {"rows", "top_brands", "period", "candidate_pool"},
    "market.brand_platform_matrix": {"rows", "top_brands", "raw_result", "failures"},
    "brand.three_platform_matrix": {"rows", "top_brands", "raw_result", "failures"},
    "derive.per_group_argmax": {"rows", "brand_platform_pairs", "selected_platform_by_brand"},
    "followup.answer": {"rows", "tables", "markdown", "meta"},
}


def _usage(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    if hasattr(usage, "model_dump"):
        return dict(usage.model_dump())
    return {
        key: getattr(usage, key) for key in (
            "prompt_tokens", "completion_tokens", "total_tokens",
        ) if getattr(usage, key, None) is not None
    }


def dynamic_plan_enabled() -> bool:
    return (
        os.environ.get("LEGACY_PIPELINE_EMERGENCY", "0").strip().lower() not in {"1", "true", "yes", "on"}
        and os.environ.get("LLM_DYNAMIC_PLAN_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
    )


def _normalized_locked_slots(route_result: Any) -> dict[str, Any]:
    """Translate conversation labels into executable market-tool slots."""
    decision = getattr(route_result, "route_decision", None) or {}
    period = str(getattr(route_result, "period", None) or "") or None
    segment = getattr(route_result, "segment", None)
    category = getattr(route_result, "category", None)
    route_type = str(getattr(route_result, "type", None) or "")
    if route_type.startswith("market_") or route_type == "market_analysis":
        focus = decision.get("focus_period") or {}
        if isinstance(focus, dict) and focus.get("start_date") and focus.get("end_date"):
            period = f"{focus['start_date']}~{focus['end_date']}"
        segment_value = str(segment or "").strip().upper()
        if segment_value not in {"BEAUTY MARKET", "PURE MASS", "SELECTIVE", "PROFESSIONAL"}:
            segment = "PURE MASS"
        category_value = str(category or "").strip().upper()
        category = {
            "TTL": "TOTAL BEAUTY", "TTL BEAUTY": "TOTAL BEAUTY",
            "TOTAL BEAUTY": "TOTAL BEAUTY",
            "FEMALE_SKIN": "FEMALE SKINCARE", "SKIN": "FEMALE SKINCARE",
            "SKINCARE": "FEMALE SKINCARE", "FEMALE SKINCARE": "FEMALE SKINCARE",
            "MEX": "MALE SKINCARE", "MALE SKINCARE": "MALE SKINCARE",
            "HAIR": "HAIR", "HAIRCARE": "HAIR", "MAKEUP": "MAKEUP",
        }.get(category_value, category or "TOTAL BEAUTY")
    return {"period": period, "segment": segment, "category": category}


def _locked_scope(route_result: Any, user_text: str) -> dict[str, Any]:
    normalized = _normalized_locked_slots(route_result)
    return {
        "route_type": getattr(route_result, "type", None),
        "brand": getattr(route_result, "brand", None),
        "period": normalized["period"],
        "platform": getattr(route_result, "platform", None),
        "segment": normalized["segment"],
        "category": normalized["category"],
        "media_mode": getattr(route_result, "media_mode", None),
        "media_channels": list(getattr(route_result, "media_channels", None) or []),
        "ranking_metric": getattr(route_result, "ranking_metric", None),
        "ranking_limit": getattr(route_result, "ranking_limit", None),
        "include_bet": bool(getattr(route_result, "include_bet", False)),
        "response_strategy": getattr(route_result, "response_strategy", None),
        "user_text": user_text,
        "route_decision": getattr(route_result, "route_decision", None) or {},
    }


def _planner_prompt(scope: dict[str, Any], error: str | None = None) -> str:
    correction = f"\n上一版计划校验失败：{error}。请重新规划。" if error else ""
    return f"""你是品牌生意分析Agent的深度规划器。
锁定范围（不可修改）：{json.dumps(scope, ensure_ascii=False, default=str)}

白名单Capability：
{json.dumps(CAPABILITIES, ensure_ascii=False)}

请根据完整问题设计最小但充分的分析DAG。可以并行查询；只有用户目标需要时才增加下钻或BET。
禁止输出SQL、表名、代码、任意工具；只能使用上述capability_id。
最后一步必须是synthesis.grounded_answer，并依赖全部需要进入最终回答的步骤。
最多12步，foreach.max_items最多10。输出一份可机器整理的计划提案。{correction}"""


def _normalizer_prompt(scope: dict[str, Any], draft: str) -> str:
    return f"""把下面的分析计划提案严格整理成JSON，不增加或修改锁定实体。
锁定范围：{json.dumps(scope, ensure_ascii=False, default=str)}
白名单：{json.dumps(CAPABILITIES, ensure_ascii=False)}
提案：{draft}

只返回JSON：
{{"title":"","steps":[{{"step_id":"snake_case","label":"","capability_id":"",
"depends_on":[],"inputs":{{}},"optional":false,"foreach":null}}]}}
inputs只能包含操作参数或前序输出引用；品牌、期间、平台由系统注入。
可用inputs字段：{json.dumps(sorted(MODEL_INPUT_KEYS), ensure_ascii=False)}。
不要输出dimensions、focus_period或comparison_periods；范围已由系统锁定。
合成步只需在depends_on列出证据步，inputs保持为空，不要猜测工具名或结果字段作为_ref。
如果其他步需要引用前序输出，只能使用对应Capability声明的输出字段：{json.dumps({key: sorted(value) for key, value in CAPABILITY_OUTPUT_PATHS.items()}, ensure_ascii=False)}。
最后一步step_id必须为synthesize、capability_id必须为synthesis.grounded_answer。"""


def _capability_allowed(capability: str, route_result: Any) -> bool:
    if capability == "synthesis.grounded_answer":
        return True
    route_type = str(getattr(route_result, "type", None) or "")
    platform = str(getattr(route_result, "platform", None) or "").upper()
    platform_templates = {
        "template.tm_business": "TM", "template.dy_business": "DY",
        "template.jd_business": "JD", "template.ttl_business": "TTL",
    }
    if capability in platform_templates:
        return (
            platform == platform_templates[capability]
            and route_type in {
                "default_chain", "douyin_business_analysis", "jd_business_analysis",
                "three_platform_competitor_analysis", "brand_business_investment_analysis",
                "opportunity_analysis",
            }
        )
    if capability == "template.bet":
        return route_type in {
            "media_analysis", "brand_business_investment_analysis",
        } or bool(getattr(route_result, "include_bet", False))
    if capability == "followup.answer":
        return route_type == "skill_dispatch"
    if capability.startswith("market."):
        return route_type.startswith("market_") or route_type == "market_analysis"
    if capability == "brand.three_platform_matrix":
        return route_type == "brand_platform_deep_dive"
    if capability == "derive.per_group_argmax":
        return route_type in {"market_brand_deep_dive", "brand_platform_deep_dive"}
    if capability == "template.brand_platform_business":
        return route_type in {"market_brand_deep_dive", "brand_platform_deep_dive"}
    if capability == "template.overall_bet":
        return route_type in {"market_brand_deep_dive", "brand_platform_deep_dive"} and bool(
            getattr(route_result, "include_bet", False)
        )
    return False


def _build_plan(payload: dict[str, Any], route_result: Any, user_text: str):
    from bot.agent_plan import AgentPlan, PlanStep, PlanValidationError, _plan_id, validate_agent_plan

    if not isinstance(payload, dict) or not isinstance(payload.get("steps"), list):
        raise PlanValidationError("planner output has no steps")
    normalized = _normalized_locked_slots(route_result)
    common = {
        "brand": getattr(route_result, "brand", None),
        "period": normalized["period"],
        "platform": getattr(route_result, "platform", None),
        "segment": normalized["segment"],
        "category": normalized["category"],
        "ranking_metric": getattr(route_result, "ranking_metric", None),
        "ranking_limit": getattr(route_result, "ranking_limit", None),
        "media_mode": getattr(route_result, "media_mode", None),
        "media_channels": list(getattr(route_result, "media_channels", None) or []),
        "requested_dimensions": list(
            (getattr(route_result, "route_decision", None) or {}).get("dimensions") or []
        ),
        "user_text": user_text,
    }
    steps = []
    for raw in payload["steps"]:
        if not isinstance(raw, dict):
            raise PlanValidationError("plan step is not an object")
        capability = str(raw.get("capability_id") or "")
        spec = CAPABILITIES.get(capability)
        if not spec:
            raise PlanValidationError(f"capability is not registered: {capability}")
        if not _capability_allowed(capability, route_result):
            raise PlanValidationError(
                f"capability {capability} is outside the locked route scope"
            )
        step_id = str(raw.get("step_id") or "").strip()
        if not step_id:
            raise PlanValidationError("plan step id is missing")
        foreach = raw.get("foreach") if isinstance(raw.get("foreach"), dict) else None
        if foreach and int(foreach.get("max_items") or 0) > 10:
            raise PlanValidationError("plan foreach exceeds 10")
        inputs = dict(raw.get("inputs") or {})
        for alias, canonical in MODEL_INPUT_ALIASES.items():
            if alias in inputs and canonical not in inputs:
                inputs[canonical] = inputs[alias]
            inputs.pop(alias, None)
        # Some models echo locked routing scope inside step inputs. Discarding
        # these fields is safe because the authoritative values are injected
        # from RouteResult immediately below.
        for key in LOCKED_SCOPE_ECHO_KEYS:
            inputs.pop(key, None)
        # Grounded synthesis consumes the complete dependency-output map.  It
        # does not dereference model-authored field paths; dropping those paths
        # prevents harmless aliases such as ``ranking.fetch_top_brands`` from
        # invalidating an otherwise safe plan.
        if capability == "synthesis.grounded_answer":
            inputs = {key: value for key, value in inputs.items() if not key.endswith("_ref")}
        unknown_input_keys = set(inputs) - MODEL_INPUT_KEYS - LOCKED_INPUT_KEYS
        if unknown_input_keys:
            raise PlanValidationError(
                f"planner inputs are not allowed: {sorted(unknown_input_keys)}"
            )
        for key in LOCKED_INPUT_KEYS:
            if key in inputs and inputs[key] not in {None, common.get(key)}:
                raise PlanValidationError(f"planner attempted to modify locked slot: {key}")
        if spec["kind"] in {"QUERY", "TEMPLATE", "SYNTHESIS"}:
            inputs = {**inputs, **common}
        steps.append(PlanStep(
            step_id=step_id,
            label=str(raw.get("label") or capability),
            executor=spec["executor"],
            depends_on=tuple(str(item) for item in raw.get("depends_on") or []),
            inputs=inputs,
            optional=bool(raw.get("optional", False)),
            kind=spec["kind"], capability_id=capability,
            foreach=foreach, required=not bool(raw.get("optional", False)),
            output_schema=spec["schema"],
        ))
    if not steps or steps[-1].step_id != "synthesize" or steps[-1].capability_id != "synthesis.grounded_answer":
        raise PlanValidationError("last step must be synthesize/grounded_answer")
    evidence_steps = {step.step_id for step in steps[:-1]}
    missing_synthesis_dependencies = evidence_steps - set(steps[-1].depends_on)
    if missing_synthesis_dependencies:
        raise PlanValidationError(
            "synthesis must depend on every evidence step: "
            f"{sorted(missing_synthesis_dependencies)}"
        )
    step_ids = {step.step_id for step in steps}
    step_by_id = {step.step_id: step for step in steps}
    for step in steps:
        refs = [
            str(value) for key, value in step.inputs.items()
            if key.endswith("_ref") and isinstance(value, str) and value
        ]
        foreach_ref = (step.foreach or {}).get("ref")
        if isinstance(foreach_ref, str) and foreach_ref:
            refs.append(foreach_ref)
        for ref in refs:
            source = ref.split(".", 1)[0]
            if source not in step_ids or source not in set(step.depends_on):
                raise PlanValidationError(
                    f"step {step.step_id} input reference is not a direct dependency: {ref}"
                )
            path = ref.split(".", 1)[1] if "." in ref else ""
            allowed_paths = CAPABILITY_OUTPUT_PATHS.get(
                str(step_by_id[source].capability_id or ""),
            )
            if allowed_paths is not None and path.split(".", 1)[0] not in allowed_paths:
                raise PlanValidationError(
                    f"step {step.step_id} reference {ref} is absent from "
                    f"{step_by_id[source].output_schema}"
                )
    plan = AgentPlan(
        plan_id=_plan_id("llm_dynamic", None, user_text),
        route_type=getattr(route_result, "type", "default_chain"),
        title=str(payload.get("title") or "动态业务分析"),
        source="llm_dynamic", execution_adapter="capability_dag",
        requires_data=True, requires_synthesis=True, steps=tuple(steps),
        requested_dimensions=tuple(
            (getattr(route_result, "route_decision", None) or {}).get("dimensions") or []
        ),
        route_id=(getattr(route_result, "route_decision", None) or {}).get("route_id"),
        version="llm-dynamic-plan-v1",
        response_strategy=getattr(route_result, "response_strategy", None),
        semantic_request={
            "locked_scope": _locked_scope(route_result, user_text),
            "planner_model": llm_model("plan"),
            "reasoning_effort": os.environ.get("DASHSCOPE_PLAN_REASONING_EFFORT", "high"),
        },
    )
    validate_agent_plan(plan)
    return plan


def compile_dynamic_plan(route_result: Any, user_text: str):
    if not os.environ.get("DASHSCOPE_API_KEY"):
        raise RuntimeError("DASHSCOPE_API_KEY is not configured for dynamic planning")
    scope = _locked_scope(route_result, user_text)
    client = llm_client(max_retries=0)
    error = None
    started = time.monotonic()
    for attempt in range(2):
        try:
            draft_response = client.chat.completions.create(
                model=llm_model("plan"),
                messages=[{"role": "user", "content": _planner_prompt(scope, error)}],
                max_tokens=int(os.environ.get("PLAN_LLM_MAX_TOKENS", "3000")),
                timeout=float(os.environ.get("PLAN_LLM_TIMEOUT", "60")),
                extra_body=llm_plan_extra_body(),
            )
            draft = draft_response.choices[0].message.content or ""
            normalized_response = client.chat.completions.create(
                model=llm_model("plan"),
                messages=[{"role": "user", "content": _normalizer_prompt(scope, draft)}],
                max_tokens=int(os.environ.get("PLAN_FORMATTER_MAX_TOKENS", "2200")),
                timeout=float(os.environ.get("PLAN_FORMATTER_TIMEOUT", "20")),
                response_format={"type": "json_object"},
                extra_body={"enable_thinking": False},
            )
            payload = extract_json_object(normalized_response.choices[0].message.content or "")
            plan = _build_plan(payload, route_result, user_text)
            log.info(
                "[dynamic_plan] model=%s attempt=%s elapsed_ms=%s steps=%s "
                "draft_usage=%s normalize_usage=%s",
                llm_model("plan"), attempt + 1, int((time.monotonic() - started) * 1000),
                [step.capability_id for step in plan.steps],
                _usage(draft_response), _usage(normalized_response),
            )
            return plan
        except Exception as exc:
            error = str(exc)
            log.warning("[dynamic_plan] attempt=%s rejected: %s", attempt + 1, error)
    raise RuntimeError(error or "dynamic planning failed")
