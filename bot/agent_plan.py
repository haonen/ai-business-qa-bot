from __future__ import annotations

"""Controlled Plan Layer for route-aware execution and user-facing progress.

The first rollout deliberately keeps execution inside the existing, tested chains.
This module compiles a validated DAG that selects one of those adapters and makes
the plan observable.  It does not give an LLM permission to call arbitrary tools.
"""

from dataclasses import asdict, dataclass, field
import hashlib
import json
import logging
import os
import re
from typing import Any

from bot.routing_contracts import infer_response_strategy


log = logging.getLogger(__name__)


NO_DATA_ROUTES = {
    "meta", "guide", "caliber_reject", "market_parameter_error",
    "unsupported_scope", "confirm_current_year", "provide_campaign_window",
    "confirm_brand_candidate", "clarify_time_roles", "clarify_media_scope",
    "clarify_v2_period", "clarify_v2_brand", "clarify_market_scope",
    "clarify_analysis_scope", "clarify_period", "clarify_business_platform",
    "clarify_ec_platform", "clarify_douyin_period", "clarify_jd_period",
    "clarify_three_platform_brand", "clarify_three_platform_period",
    "clarify_market_period",
}

CLARIFICATION_ROUTES = {
    route_type for route_type in NO_DATA_ROUTES
    if route_type.startswith("clarify_") or route_type.startswith("confirm_")
    or route_type == "provide_campaign_window"
}

ALLOWED_EXECUTORS = {
    "answer_from_knowledge",
    "request_clarification",
    "data_availability_lookup",
    "tmall_business_template",
    "douyin_business_template",
    "jd_business_template",
    "three_platform_business_template",
    "bet_investment_template",
    "ec_business_branch",
    "bet_investment_branch",
    "market_summary",
    "market_brand_ranking",
    "market_brand_drilldown",
    "market_brand_platform_matrix",
    "brand_three_platform_matrix",
    "derive_per_group_argmax",
    "market_brand_platform_template_fanout",
    "market_brand_bet_fanout",
    "followup_skill_planner",
    "followup_tool_bundle",
    "context_ec_report_evidence",
    "resolve_followup_subject",
    "check_evidence_coverage",
    "query_platform_followup",
    "synthesize_ec_followup",
    "filter_update",
    "synthesize_evidence",
}

OUTPUT_SCHEMA_FIELDS = {
    "market_ranking.v1": {
        "brand", "rank", "gmv_actual", "gmv_prior", "evol", "gmv_growth",
    },
    "brand_platform_matrix.v1": {
        "brand", "platform", "gmv_actual", "gmv_prior", "evol", "gmv_growth",
        "weight", "growth_contribution",
    },
    "brand_platform_selection.v1": {
        "brand", "platform", "gmv_actual", "gmv_prior", "evol", "gmv_growth",
        "weight", "growth_contribution", "selection_metric", "selection_value",
    },
}


def _flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def agent_plan_enabled() -> bool:
    return _flag("AGENT_PLAN_LAYER_ENABLED", "0")


def agent_plan_shadow_enabled() -> bool:
    return _flag("AGENT_PLAN_LAYER_SHADOW", "1")


def smart_progress_enabled() -> bool:
    return _flag("BOT_SMART_PROGRESS_ENABLED", "1")


@dataclass(frozen=True)
class PlanStep:
    step_id: str
    label: str
    executor: str
    depends_on: tuple[str, ...] = ()
    inputs: dict[str, Any] = field(default_factory=dict)
    optional: bool = False
    kind: str = "QUERY"
    capability_id: str | None = None
    foreach: dict[str, Any] | None = None
    required: bool = True
    output_schema: str | None = None
    condition_ref: str | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["depends_on"] = list(self.depends_on)
        data["capability_id"] = self.capability_id or self.executor
        data["required"] = self.required and not self.optional
        return data


@dataclass(frozen=True)
class AgentPlan:
    plan_id: str
    route_type: str
    title: str
    source: str
    execution_adapter: str
    requires_data: bool
    requires_synthesis: bool
    steps: tuple[PlanStep, ...]
    requested_dimensions: tuple[str, ...] = ()
    route_id: str | None = None
    version: str = "controlled-plan-v1"
    response_strategy: str | None = None

    def to_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "route_id": self.route_id,
            "version": self.version,
            "route_type": self.route_type,
            "response_strategy": self.response_strategy or infer_response_strategy(
                action=self.route_type,
            ),
            "title": self.title,
            "source": self.source,
            "execution_adapter": self.execution_adapter,
            "requires_data": self.requires_data,
            "requires_synthesis": self.requires_synthesis,
            "requested_dimensions": list(self.requested_dimensions),
            "steps": [step.to_dict() for step in self.steps],
        }


class PlanValidationError(ValueError):
    pass


def _requested_dimensions(text: str) -> tuple[str, ...]:
    dimensions: list[str] = []
    patterns = (
        ("platform", r"平台|天猫|抖音|京东|三平台"),
        ("assortment", r"选品|商品|产品|sku|品类|类目"),
        ("business_cadence", r"生意节奏|月度趋势|节奏|大促"),
        ("price", r"价格|价位|客单价|价格带"),
        ("promotion", r"促销|折扣|优惠|促销机制"),
        ("investment", r"bet|媒体投资|媒体花费|投放"),
        ("ranking", r"top\s*\d*|排名|头部品牌|品牌排行"),
    )
    for name, pattern in patterns:
        if re.search(pattern, text, re.I):
            dimensions.append(name)
    return tuple(dimensions)


def _comparison_metric(text: str) -> str:
    value = str(text or "").casefold()
    if any(word in value for word in ("贡献最大", "增长贡献", "contribution")):
        return "growth_contribution"
    if any(word in value for word in ("增速最高", "涨幅最高", "同比最高", "growth rate")):
        return "evol"
    if any(word in value for word in ("规模最大", "生意最好", "gmv最大", "销售额最大")):
        return "gmv_actual"
    return "gmv_growth"


def _plan_id(route_type: str, route_id: str | None, text: str) -> str:
    seed = f"{route_id or ''}\n{route_type}\n{text}"
    return "plan_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def compile_agent_plan(route_result: Any, user_text: str) -> AgentPlan:
    """Compile a route into a whitelisted DAG; never invent tools or entities."""
    route_type = getattr(route_result, "type", None) or "guide"
    decision = getattr(route_result, "route_decision", None) or {}
    route_id = decision.get("route_id")
    common = {
        "brand": getattr(route_result, "brand", None),
        "period": getattr(route_result, "period", None),
        "platform": getattr(route_result, "platform", None),
        "user_text": user_text,
    }
    dimensions = _requested_dimensions(user_text)
    comparison_metric = _comparison_metric(user_text)
    response_strategy = (
        getattr(route_result, "response_strategy", None)
        or decision.get("response_strategy")
        or infer_response_strategy(
            action=route_type,
            question_mode=getattr(route_result, "question_mode", None),
            reason_codes=(decision.get("reason_codes") or []),
            user_text=user_text,
        )
    )

    if route_type in NO_DATA_ROUTES:
        executor = "request_clarification" if route_type in CLARIFICATION_ROUTES else "answer_from_knowledge"
        title = "确认分析范围" if executor == "request_clarification" else "回答功能或口径问题"
        steps = (PlanStep("respond", title, executor, inputs=common),)
        plan = AgentPlan(
            _plan_id(route_type, route_id, user_text), route_type, title, "response",
            route_type, False, False, steps, dimensions, route_id,
        )
    elif route_type == "brand_business_investment_analysis":
        steps = (
            PlanStep("ec", "分析电商生意", "ec_business_branch", inputs=common),
            PlanStep("bet", "分析BET媒体投资", "bet_investment_branch", inputs=common),
            PlanStep("synthesize", "整合生意与投资证据", "synthesize_evidence", ("ec", "bet")),
        )
        plan = AgentPlan(
            _plan_id(route_type, route_id, user_text), route_type, "生意与BET联合分析", "recipe",
            route_type, True, True, steps, dimensions, route_id,
        )
    elif route_type == "market_brand_deep_dive":
        market_inputs = {
            **common,
            "segment": getattr(route_result, "segment", None),
            "category": getattr(route_result, "category", None),
            "ranking_metric": getattr(route_result, "ranking_metric", None),
            "ranking_limit": getattr(route_result, "ranking_limit", None),
            "requested_dimensions": list(dimensions),
        }
        steps_list = [
            PlanStep(
                "ranking", "确定头部品牌范围", "market_brand_ranking", inputs=market_inputs,
                kind="QUERY", capability_id="market.top_brands", output_schema="market_ranking.v1",
            ),
            PlanStep(
                "platform_matrix", "生成每个品牌的三平台生意矩阵", "market_brand_platform_matrix",
                ("ranking",), market_inputs, kind="QUERY",
                capability_id="market.brand_platform_matrix", output_schema="brand_platform_matrix.v1",
            ),
            PlanStep(
                "select_platform", "按品牌选出目标指标最大的平台", "derive_per_group_argmax",
                ("platform_matrix",), {
                    "operator": "per_group_argmax", "input_ref": "platform_matrix.rows",
                    "expected_groups_ref": "ranking.top_brands",
                    "group_by": "brand", "rank_by": comparison_metric,
                    "supported_values": ["TM", "DY", "JD"],
                }, kind="DERIVE", capability_id="derive.per_group_argmax",
                output_schema="brand_platform_selection.v1",
            ),
            PlanStep(
                "platform_templates", "逐品牌下钻已选平台的生意模板",
                "market_brand_platform_template_fanout", ("select_platform",), market_inputs,
                kind="TEMPLATE", capability_id="template.brand_platform_business",
                foreach={"ref": "select_platform.brand_platform_pairs", "max_items": 5},
                output_schema="brand_platform_reports.v1",
            ),
        ]
        synth_dependencies = ["platform_templates"]
        if getattr(route_result, "include_bet", False):
            steps_list.append(PlanStep(
                "bet_fanout", "逐品牌查询今年截至最新月BET",
                "market_brand_bet_fanout", ("ranking",), market_inputs,
                kind="TEMPLATE", capability_id="template.overall_bet",
                foreach={"ref": "ranking.top_brands", "max_items": 5},
                required=False, output_schema="brand_bet_reports.v1",
            ))
            synth_dependencies.append("bet_fanout")
        steps_list.append(PlanStep(
            "synthesize", "归纳可复用的事实模式", "synthesize_evidence",
            tuple(synth_dependencies),
            kind="SYNTHESIS", capability_id="synthesis.evidence_report",
            output_schema="agent_answer.v1",
        ))
        steps = tuple(steps_list)
        plan = AgentPlan(
            _plan_id(route_type, route_id, user_text), route_type, "大盘Top品牌跨平台深挖", "recipe",
            route_type, True, True, steps, dimensions, route_id, "executable-plan-v2",
        )
    elif route_type == "brand_platform_deep_dive":
        brand_inputs = {**common, "requested_dimensions": list(dimensions)}
        steps_list = [
            PlanStep(
                "platform_matrix", "获取品牌三平台生意矩阵", "brand_three_platform_matrix",
                inputs=brand_inputs, kind="QUERY", capability_id="brand.three_platform_matrix",
                output_schema="brand_platform_matrix.v1",
            ),
            PlanStep(
                "select_platform", "比较并选出目标指标最大的平台", "derive_per_group_argmax",
                ("platform_matrix",), {
                    "operator": "per_group_argmax", "input_ref": "platform_matrix.rows",
                    "expected_groups_ref": "platform_matrix.top_brands",
                    "group_by": "brand", "rank_by": comparison_metric,
                    "supported_values": ["TM", "DY", "JD"],
                }, kind="DERIVE", capability_id="derive.per_group_argmax",
                output_schema="brand_platform_selection.v1",
            ),
            PlanStep(
                "platform_templates", "下钻选中平台的现有生意模板",
                "market_brand_platform_template_fanout", ("select_platform",), brand_inputs,
                kind="TEMPLATE", capability_id="template.brand_platform_business",
                foreach={"ref": "select_platform.brand_platform_pairs", "max_items": 1},
                output_schema="brand_platform_reports.v1",
            ),
        ]
        dependencies = ["platform_templates"]
        if getattr(route_result, "include_bet", False):
            steps_list.append(PlanStep(
                "bet_fanout", "补充品牌BET投资分析", "market_brand_bet_fanout",
                ("platform_matrix",), {**brand_inputs, "brands_ref": "platform_matrix.top_brands"},
                kind="TEMPLATE", capability_id="template.overall_bet",
                foreach={"ref": "platform_matrix.top_brands", "max_items": 1},
                required=False, output_schema="brand_bet_reports.v1",
            ))
            dependencies.append("bet_fanout")
        steps_list.append(PlanStep(
            "synthesize", "整合三平台比较、平台下钻与投资证据", "synthesize_evidence",
            tuple(dependencies), kind="SYNTHESIS",
            capability_id="synthesis.single_brand_platform_report", output_schema="agent_answer.v1",
        ))
        plan = AgentPlan(
            _plan_id(route_type, route_id, user_text), route_type, "单品牌三平台比较与动态下钻", "recipe",
            route_type, True, True, tuple(steps_list), dimensions, route_id, "executable-plan-v2",
        )
    else:
        template_map = {
            "default_chain": ("天猫生意分析", "tmall_business_template"),
            "douyin_business_analysis": ("抖音生意分析", "douyin_business_template"),
            "jd_business_analysis": ("京东生意分析", "jd_business_template"),
            "three_platform_competitor_analysis": ("三平台生意分析", "three_platform_business_template"),
            "media_analysis": ("BET媒体投资分析", "bet_investment_template"),
            "market_analysis": ("大盘概览", "market_summary"),
            "market_brand_ranking": ("大盘品牌排名", "market_brand_ranking"),
            "skill_dispatch": ("定向业务分析", "followup_skill_planner"),
            "data_availability": ("查询数据覆盖时间", "data_availability_lookup"),
            "filter_update": ("更新分析筛选条件", "filter_update"),
        }
        title, executor = template_map.get(route_type, ("受控回答", "answer_from_knowledge"))
        if route_type == "skill_dispatch":
            steps = (
                PlanStep(
                    "context", "读取最新EC报告证据", "context_ec_report_evidence",
                    inputs=common, kind="QUERY", capability_id="context.ec_report_evidence",
                    output_schema="ec_report_context.v2",
                ),
                PlanStep(
                    "resolve_subject", "恢复品牌、时间、平台和分析对象", "resolve_followup_subject",
                    ("context",), {**common, "operator": "resolve_followup_subject", "input_ref": "context.rows"},
                    kind="DERIVE", capability_id="derive.resolve_followup_subject",
                    output_schema="ec_followup_subject.v2",
                ),
                PlanStep(
                    "coverage", "检查现有报告证据是否足够", "check_evidence_coverage",
                    ("resolve_subject",), {"operator": "check_evidence_coverage", "input_ref": "resolve_subject.rows"},
                    kind="DERIVE", capability_id="derive.check_evidence_coverage",
                    output_schema="ec_evidence_coverage.v2",
                ),
                PlanStep(
                    "query_missing", "仅查询报告证据缺失的维度", "query_platform_followup",
                    ("coverage",), common, kind="QUERY", capability_id="ec.platform_followup",
                    required=False, output_schema="ec_followup_evidence.v2",
                    condition_ref="coverage.requires_query",
                ),
                PlanStep(
                    "synthesize", "根据已校验证据直接回答", "synthesize_ec_followup",
                    ("coverage", "query_missing"), common, kind="SYNTHESIS",
                    capability_id="synthesis.ec_followup", output_schema="ec_followup_answer.v2",
                ),
            )
            source, requires_synthesis = "generated_from_registry", True
        else:
            template_capabilities = {
                "default_chain": "template.tm_business",
                "douyin_business_analysis": "template.dy_business",
                "jd_business_analysis": "template.jd_business",
                "three_platform_competitor_analysis": "template.ttl_business",
            }
            capability = template_capabilities.get(route_type)
            steps = (PlanStep(
                "execute", title, executor, inputs=common,
                kind="TEMPLATE" if capability else "QUERY",
                capability_id=capability or executor,
                output_schema="ec-report-evidence.v2" if capability else None,
            ),)
            source, requires_synthesis = "template", False
        requires_data = executor not in {"answer_from_knowledge", "filter_update"}
        plan = AgentPlan(
            _plan_id(route_type, route_id, user_text), route_type, title, source,
            route_type, requires_data, requires_synthesis, steps, dimensions, route_id,
            "executable-followup-v2" if route_type == "skill_dispatch" else "controlled-plan-v1",
        )

    validate_agent_plan(plan)
    if plan.response_strategy == response_strategy:
        return plan
    return AgentPlan(**{**plan.__dict__, "response_strategy": response_strategy})


def validate_agent_plan(plan: AgentPlan) -> None:
    max_steps = max(1, int(os.environ.get("AGENT_PLAN_MAX_STEPS", "8")))
    if len(plan.steps) > max_steps:
        raise PlanValidationError(f"plan has {len(plan.steps)} steps; max is {max_steps}")
    ids = [step.step_id for step in plan.steps]
    if len(ids) != len(set(ids)):
        raise PlanValidationError("plan step ids must be unique")
    known = set(ids)
    allowed_kinds = {"QUERY", "TEMPLATE", "DERIVE", "SYNTHESIS"}
    for step in plan.steps:
        if step.kind not in allowed_kinds:
            raise PlanValidationError(f"unknown step kind: {step.kind}")
        if step.executor not in ALLOWED_EXECUTORS:
            raise PlanValidationError(f"executor is not registered: {step.executor}")
        missing = set(step.depends_on) - known
        if missing:
            raise PlanValidationError(f"step {step.step_id} has unknown dependencies: {sorted(missing)}")
        if step.step_id in step.depends_on:
            raise PlanValidationError(f"step {step.step_id} depends on itself")
        if step.kind == "DERIVE":
            operator = str(step.inputs.get("operator") or "")
            from bot.derive_ops import DERIVE_OPERATORS
            if operator not in DERIVE_OPERATORS:
                raise PlanValidationError(f"derive operator is not registered: {operator}")
            if not step.inputs.get("input_ref"):
                raise PlanValidationError(f"derive step {step.step_id} requires input_ref")
        if step.foreach and int(step.foreach.get("max_items") or 0) > 5:
            raise PlanValidationError(f"step {step.step_id} foreach max exceeds 5")
        if step.condition_ref:
            source = step.condition_ref.split(".", 1)[0]
            if source not in set(step.depends_on):
                raise PlanValidationError(
                    f"step {step.step_id} condition must reference a direct dependency"
                )

    visiting: set[str] = set()
    visited: set[str] = set()
    graph = {step.step_id: step.depends_on for step in plan.steps}

    def visit(step_id: str) -> None:
        if step_id in visiting:
            raise PlanValidationError("plan contains a dependency cycle")
        if step_id in visited:
            return
        visiting.add(step_id)
        for dependency in graph[step_id]:
            visit(dependency)
        visiting.remove(step_id)
        visited.add(step_id)

    for step_id in ids:
        visit(step_id)

    seen: set[str] = set()
    schemas = {step.step_id: step.output_schema for step in plan.steps}
    for step in plan.steps:
        future = set(step.depends_on) - seen
        if future:
            raise PlanValidationError(f"step {step.step_id} references a future step: {sorted(future)}")
        if step.kind == "DERIVE":
            source = str(step.inputs.get("input_ref") or "").split(".", 1)[0]
            references = [
                str(value) for key, value in step.inputs.items()
                if key.endswith("_ref") and value
            ]
            unknown_refs = [ref for ref in references if ref.split(".", 1)[0] not in seen]
            if unknown_refs:
                raise PlanValidationError(
                    f"derive step {step.step_id} references unavailable outputs: {unknown_refs}"
                )
            fields = OUTPUT_SCHEMA_FIELDS.get(str(schemas.get(source) or ""))
            for field_name in ("group_by", "rank_by"):
                value = step.inputs.get(field_name)
                if value and fields is not None and value not in fields:
                    raise PlanValidationError(
                        f"derive step {step.step_id} field {value} is absent from {schemas.get(source)}"
                    )
        seen.add(step.step_id)


def planning_progress(plan: AgentPlan) -> str | None:
    if not smart_progress_enabled() or not plan.requires_data:
        return None
    if plan.route_type == "brand_business_investment_analysis":
        return "这个问题包含电商生意和BET两个部分，我正在确认品牌、时间与平台，并拆分分析步骤…"
    if plan.route_type == "market_brand_deep_dive":
        if any(step.executor == "market_brand_bet_fanout" for step in plan.steps):
            return "这个问题包含Top品牌排名、品牌×平台矩阵、增长平台模板和逐品牌BET，我正在按先后依赖安排分析…"
        return "这个问题需要先确定Top品牌，再做跨平台和商品拆解，我正在按依赖关系安排分析…"
    if plan.route_type == "brand_platform_deep_dive":
        return "我会先比较该品牌的天猫、抖音和京东，再对选中的平台继续下钻…"
    if plan.route_type == "skill_dispatch":
        return "我正在结合上下文，确认这次追问需要哪些数据维度…"
    return f"已识别为【{plan.title}】，正在核对品牌、时间和数据范围…"


def finalizing_progress(plan: AgentPlan) -> str | None:
    if not smart_progress_enabled() or not plan.requires_data or not plan.requires_synthesis:
        return None
    return "数据步骤已完成，正在检查证据是否完整，并整理成可读结论…"


def initial_status(*, queued: bool, job_id: str | None = None) -> str:
    if not smart_progress_enabled():
        if queued:
            return f"已收到请求，正在排队处理。任务编号：{(job_id or '')[-8:]}"
        return "正在分析数据，请稍候…"
    if queued:
        return f"收到，我会先理解问题再安排处理。任务编号：{(job_id or '')[-8:]}"
    return "收到，我先理解一下你的问题…"


def completion_status(route_type: str | None, meta: dict | None = None) -> str:
    if not smart_progress_enabled():
        return "已完成，结果如下："
    if route_type in CLARIFICATION_ROUTES or (meta or {}).get("awaiting"):
        return "我需要你确认一个范围："
    if route_type in {"meta", "guide", "caliber_reject"}:
        return "已经整理好回答："
    if route_type == "data_availability":
        return "已核对数据覆盖范围："
    plan = (meta or {}).get("agent_plan") or {}
    if plan and not plan.get("requires_data", True):
        return "已经整理好回答："
    if route_type in {"unsupported_scope", "market_parameter_error"}:
        return "已核对当前可用的数据范围："
    if route_type == "filter_update":
        return "已更新分析范围："
    return "分析已完成，结果如下："


def log_plan(plan: AgentPlan, *, shadow: bool) -> None:
    event = "agent_plan_shadow" if shadow else "agent_plan"
    log.info("[%s] %s", event, json.dumps(plan.to_dict(), ensure_ascii=False, default=str))
