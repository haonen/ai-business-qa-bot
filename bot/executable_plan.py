from __future__ import annotations

"""Executable, capability-gated DAG runtime for controlled agent plans."""

from dataclasses import dataclass, field
import logging
import os
import re
import time
from typing import Any, Callable

from bot.agent_plan import AgentPlan, PlanStep
from bot.chains.default_chain import run_default_chain
from bot.chains.douyin_business_chain import run_douyin_business_chain
from bot.chains.jd_business_chain import run_jd_business_chain
from bot.chains.three_platform_competitor_chain import run_three_platform_competitor_chain
from bot.chains.media_chain import run_media_chain
from bot.derive_ops import run_derive
from bot.tools.query_market_brand_deep_dive import build_brand_platform_matrix
from bot.tools.query_market_top_brands import query_market_top_brands
from bot.tools.query_market_trend import query_market_trend
from bot.tools.query_three_platform_competitor import query_three_platform_competitor
from bot.three_platform_competitor_formatter import format_three_platform_competitor_report
from bot.session import SessionState
from bot.ec_report_evidence import build_ec_report_evidence, evidence_from_cache
from bot.brand_reference import english_brand_for_chinese
from bot.followup_plan import build_followup_plan
from bot.followup_formatter import format_followup_result
from bot.opportunity_insight import build_reasoning_pack, run_opportunity_reasoning_v3
from bot.semantic_analysis import reasoning_v3_enabled


log = logging.getLogger(__name__)
PLATFORM_LABELS = {"TM": "天猫", "DY": "抖音", "JD": "京东"}
SUPPORTED_PLATFORMS = tuple(PLATFORM_LABELS)


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def executable_plan_enabled() -> bool:
    return _flag("EXECUTABLE_PLAN_V2_ENABLED")


def executable_plan_shadow_enabled() -> bool:
    return _flag("EXECUTABLE_PLAN_V2_SHADOW", "1")


def derive_enabled() -> bool:
    return _flag("EXECUTABLE_PLAN_DERIVE_ENABLED")


def ec_followup_plan_enabled() -> bool:
    return _flag("EC_FOLLOWUP_PLANNER_V2_ENABLED")


def ec_followup_plan_shadow_enabled() -> bool:
    return _flag("EC_FOLLOWUP_PLANNER_V2_SHADOW", "1")


def can_execute_plan(plan: AgentPlan) -> bool:
    if plan.version == "llm-dynamic-plan-v1":
        return True
    if plan.route_type == "opportunity_analysis" and plan.version == "agent-reasoning-v3":
        return reasoning_v3_enabled()
    if (
        plan.route_type == "skill_dispatch"
        and plan.version == "executable-followup-v2"
        and ec_followup_plan_enabled()
    ):
        return True
    return (
        executable_plan_enabled()
        and derive_enabled()
        and plan.route_type in {"market_brand_deep_dive", "brand_platform_deep_dive"}
        and plan.version == "executable-plan-v2"
    )


def _evidence_matches(evidence: dict, *, brand: str, period: str, platform: str) -> bool:
    if str(platform or "").upper() == "DY" and not any(
        source.get("product_scope") == "level4_nonempty_v1"
        for source in evidence.get("data_sources") or []
    ):
        return False
    evidence_brand = str(evidence.get("brand_surface") or evidence.get("brand") or "").casefold()
    evidence_canonical = str(evidence.get("canonical_brand") or "").casefold()
    requested = str(brand or "").casefold()
    requested_canonical = str(english_brand_for_chinese(brand) or brand or "").casefold()
    evidence_period = str((evidence.get("period_range") or evidence.get("period") or {}).get("raw") or "")
    return bool(
        requested
        and bool({requested, requested_canonical} & {evidence_brand, evidence_canonical})
        and evidence_period == str(period or "")
        and str(evidence.get("platform") or "").upper() == str(platform or "").upper()
    )


def _execute_opportunity_report(
    step: PlanStep, session: SessionState | None,
    on_progress: Callable[[str], None] | None,
) -> dict:
    brand = str(step.inputs.get("brand") or "").strip()
    period = str(step.inputs.get("period") or "").strip()
    platform = str(step.inputs.get("platform") or "").upper()
    if not brand or not period or platform not in {"TM", "DY", "JD", "TTL"}:
        raise ValueError("opportunity analysis requires brand, period and platform")
    cached = None
    if session is not None:
        ctx = session.ec_context
        cached = ctx.latest_report_evidence
        if not cached and ctx.recent_evidence and isinstance(ctx.recent_evidence[-1], dict):
            cached = ctx.recent_evidence[-1]
        if not cached:
            cached = evidence_from_cache(
                ctx.report_cache or session.last_result_cache or {},
                brand=ctx.brand, period=ctx.period, platform=ctx.platform,
            )
    if cached and _evidence_matches(cached, brand=brand, period=period, platform=platform):
        return {"evidence": cached, "reused": True, "report_result": None}
    if on_progress:
        on_progress(f"已锁定{brand}、{period}和{PLATFORM_LABELS.get(platform, '三平台')}范围，正在读取生意证据…")
    aliases = [brand]
    runners = {
        "TM": lambda: run_default_chain(brand, period, brand_aliases=aliases, on_progress=on_progress),
        "DY": lambda: run_douyin_business_chain(brand, period, brand_aliases=aliases, on_progress=on_progress),
        "JD": lambda: run_jd_business_chain(brand, period, brand_aliases=aliases, on_progress=on_progress),
        "TTL": lambda: run_three_platform_competitor_chain(brand, period, on_progress=on_progress),
    }
    report = runners[platform]()
    if not report.get("ok", True):
        raise RuntimeError(report.get("markdown") or "business report did not return evidence")
    meta = dict(report.get("meta") or {})
    meta.setdefault("brand", brand)
    meta.setdefault("period", period)
    evidence = build_ec_report_evidence(meta, platform)
    if not evidence:
        raise RuntimeError("business report could not be converted to evidence envelope")
    return {"evidence": evidence, "reused": False, "report_result": report}


@dataclass
class StepExecution:
    step_id: str
    kind: str
    capability_id: str
    status: str
    elapsed_ms: int
    output: dict = field(default_factory=dict)
    error: str | None = None

    def trace_dict(self) -> dict:
        return {
            "step_id": self.step_id,
            "kind": self.kind,
            "capability_id": self.capability_id,
            "status": self.status,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
            "output_summary": _output_summary(self.output),
        }


def _output_summary(output: dict) -> dict:
    summary: dict[str, Any] = {}
    for key in ("rows", "top_brands", "brand_platform_pairs", "reports", "missing_groups", "failures"):
        value = output.get(key)
        if isinstance(value, list):
            summary[f"{key}_count"] = len(value)
        elif isinstance(value, dict):
            summary[f"{key}_count"] = len(value)
    if output.get("error"):
        summary["error"] = output.get("error")
    return summary


def _resolve_ref(reference: str, outputs: dict[str, dict]) -> Any:
    parts = str(reference).split(".")
    if not parts or parts[0] not in outputs:
        raise KeyError(f"unknown step output reference: {reference}")
    value: Any = outputs[parts[0]]
    for part in parts[1:]:
        if not isinstance(value, dict) or part not in value:
            raise KeyError(f"unknown output path: {reference}")
        value = value[part]
    return value


def _market_inputs(step: PlanStep) -> dict:
    return {
        "period": step.inputs.get("period"),
        "segment": step.inputs.get("segment") or "PURE MASS",
        "platform": step.inputs.get("platform") or "TTL",
        "category": step.inputs.get("category") or "TOTAL BEAUTY",
        "ranking_metric": step.inputs.get("ranking_metric") or "gmv_actual",
        "ranking_limit": min(max(int(step.inputs.get("ranking_limit") or 5), 1), 5),
    }


def _execute_ranking(step: PlanStep, _: dict[str, dict]) -> dict:
    values = _market_inputs(step)
    result = query_market_top_brands(
        values["period"], values["segment"], values["platform"],
        values["ranking_metric"], values["ranking_limit"], values["category"],
    )
    if result.get("error"):
        raise RuntimeError(result.get("message") or str(result["error"]))
    rows = list(result.get("rows") or [])[: values["ranking_limit"]]
    return {**result, "rows": rows, "top_brands": [str(row.get("brand")) for row in rows]}


def _execute_platform_matrix(step: PlanStep, outputs: dict[str, dict]) -> dict:
    ranking = next(
        (outputs[dep] for dep in step.depends_on if outputs.get(dep, {}).get("top_brands") is not None),
        None,
    )
    if ranking is None:
        raise RuntimeError("platform matrix requires a ranking dependency")
    ranking_rows = list(ranking.get("rows") or [])[:10]
    brands = [str(row.get("brand") or "") for row in ranking_rows if row.get("brand")]
    breakdown = build_brand_platform_matrix(list(ranking.get("coverage") or []), brands)
    brand_rows, flat_rows = [], []
    for ranking_row in ranking_rows:
        brand = str(ranking_row.get("brand") or "")
        detail = breakdown.get(brand) or {"platforms": [], "months": []}
        platforms = []
        for row in detail.get("platforms") or []:
            enriched = {"brand": brand, **row}
            platforms.append(enriched)
            if float(enriched.get("gmv_actual") or 0) != 0 or float(enriched.get("gmv_prior") or 0) != 0:
                flat_rows.append(enriched)
        brand_rows.append({**ranking_row, **detail, "platforms": platforms})
    return {
        "brands": brand_rows,
        "rows": flat_rows,
        "top_brands": brands,
        "query_meta": ranking.get("query_meta") or {},
    }


def _execute_brand_platform_matrix(step: PlanStep, _: dict[str, dict]) -> dict:
    brand = str(step.inputs.get("brand") or "").strip()
    period = str(step.inputs.get("period") or "").strip()
    if not brand or not period:
        raise ValueError("brand and period are required for three-platform comparison")
    result = query_three_platform_competitor(brand, period)
    if result.get("error"):
        raise RuntimeError(result.get("message") or str(result["error"]))
    source_rows = [
        row for row in (result.get("overall") or [])
        if str(row.get("platform") or "").upper() in SUPPORTED_PLATFORMS
    ]
    ttl_current = sum(float(row.get("gmv_current") or 0) for row in source_rows)
    ttl_growth = sum(
        float(row.get("gmv_current") or 0) - float(row.get("gmv_prior") or 0)
        for row in source_rows
    )
    rows = []
    for row in source_rows:
        current = float(row.get("gmv_current") or 0)
        prior = float(row.get("gmv_prior") or 0)
        growth = current - prior
        rows.append({
            "brand": brand,
            "platform": str(row.get("platform") or "").upper(),
            "gmv_actual": current,
            "gmv_prior": prior,
            "evol": row.get("evol"),
            "gmv_growth": growth,
            "weight": current / ttl_current if ttl_current else None,
            "growth_contribution": growth / ttl_growth if ttl_growth else None,
        })
    return {
        "rows": rows, "top_brands": [brand], "brand": brand,
        "raw_result": result, "formatted_report": format_three_platform_competitor_report(result),
    }


def _execute_derive(step: PlanStep, outputs: dict[str, dict]) -> dict:
    reference = str(step.inputs.get("input_ref") or "")
    rows = _resolve_ref(reference, outputs)
    if not isinstance(rows, list):
        raise TypeError(f"derive input is not a list: {reference}")
    expected_reference = step.inputs.get("expected_groups_ref")
    expected_groups = _resolve_ref(str(expected_reference), outputs) if expected_reference else None
    return run_derive(
        str(step.inputs.get("operator") or ""), rows=rows,
        group_by=step.inputs.get("group_by"), rank_by=step.inputs.get("rank_by"),
        supported_values=step.inputs.get("supported_values"),
        expected_groups=expected_groups,
    )


def _execute_platform_templates(
    step: PlanStep, outputs: dict[str, dict], on_progress: Callable[[str], None] | None,
) -> dict:
    max_items = min(10, max(1, int((step.foreach or {}).get("max_items") or 10)))
    pairs_reference = (step.foreach or {}).get("ref")
    if pairs_reference:
        pairs_value = _resolve_ref(str(pairs_reference), outputs)
    else:
        pairs_value = next(
            (
                outputs[dep].get("brand_platform_pairs")
                for dep in step.depends_on
                if outputs.get(dep, {}).get("brand_platform_pairs") is not None
            ),
            None,
        )
    pairs = list(pairs_value or [])[:max_items]
    period = str(step.inputs.get("period") or "")
    runners = {"TM": run_default_chain, "DY": run_douyin_business_chain, "JD": run_jd_business_chain}
    reports, failures = {}, []
    for index, pair in enumerate(pairs, 1):
        brand = str(pair.get("brand") or "")
        platform = str(pair.get("platform") or "").upper()
        if not brand or platform not in runners:
            failures.append({"brand": brand, "platform": platform, "error": "unsupported_pair"})
            continue
        if on_progress:
            on_progress(
                f"平台比较完成，正在下钻{index}/{len(pairs)}【{brand}】的"
                f"{PLATFORM_LABELS[platform]}生意…"
            )
        try:
            result = runners[platform](brand, period, brand_aliases=[brand])
            reports[brand] = {"platform": platform, "result": result, "selection": pair}
            if not result.get("ok", True):
                failures.append({"brand": brand, "platform": platform, "error": "template_no_data"})
        except Exception as exc:
            log.exception("executable platform template failed brand=%s platform=%s", brand, platform)
            failures.append({"brand": brand, "platform": platform, "error": str(exc)})
    return {"reports": reports, "failures": failures, "rows": pairs}


def _execute_bet(
    step: PlanStep, outputs: dict[str, dict], on_progress: Callable[[str], None] | None,
) -> dict:
    brands_reference = step.inputs.get("brands_ref")
    max_items = min(10, max(1, int((step.foreach or {}).get("max_items") or 10)))
    if brands_reference:
        brands = list(_resolve_ref(str(brands_reference), outputs) or [])[:max_items]
    else:
        brands = list(next(
            (
                outputs[dep].get("top_brands")
                for dep in step.depends_on
                if outputs.get(dep, {}).get("top_brands") is not None
            ),
            [],
        ) or [])[:max_items]
    period = str(step.inputs.get("period") or "")
    reports, failures = {}, []
    for index, brand in enumerate(brands, 1):
        if on_progress:
            on_progress(f"正在补充{index}/{len(brands)}【{brand}】在锁定期间内的BET…")
        try:
            result = run_media_chain(
                brand, period, brand_aliases=[brand],
                media_scope="full_bet", media_mode="OVERALL_BET",
            )
            reports[brand] = result
            if not result.get("ok", True):
                failures.append({"brand": brand, "error": "bet_no_data"})
        except Exception as exc:
            log.exception("executable BET failed brand=%s", brand)
            failures.append({"brand": brand, "error": str(exc)})
    return {"reports": reports, "failures": failures, "period": period}


def _money(value: Any) -> str:
    try:
        return f"{float(value) / 1_000_000:.1f}M"
    except (TypeError, ValueError):
        return "—"


def _pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:+.1f}%"
    except (TypeError, ValueError):
        return "—"


def _markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    """Keep table lines contiguous so the Feishu renderer creates a native table."""
    header = "| " + " | ".join(headers) + " |"
    separator = "|" + "|".join("---:" if index else "---" for index in range(len(headers))) + "|"
    body = ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join([header, separator, *body])


def _synthesize(outputs: dict[str, dict]) -> dict:
    ranking = outputs["ranking"]
    matrix = outputs["platform_matrix"]
    selection = outputs["select_platform"]
    templates = outputs["platform_templates"]
    ranking_rows = list(ranking.get("rows") or [])
    selected_by_brand = {str(row.get("brand")): row for row in selection.get("brand_platform_pairs") or []}
    first = ranking_rows[0] if ranking_rows else {}
    ranking_metric = str((ranking.get("query_meta") or {}).get("ranking_metric") or "gmv_actual")
    ranking_label = {
        "gmv_actual": "本期GMV规模", "gmv_growth": "GMV增长额", "evol": "同比增速",
    }.get(ranking_metric, ranking_metric)
    answer = (
        f"先给结论：本期Top{len(ranking_rows)}中{first.get('brand')}按{ranking_label}排名第1；"
        "系统已分别比较每个品牌的天猫、抖音和京东，并对各自目标指标表现最突出的平台继续下钻。"
        if ranking_rows else "先给结论：当前期间没有足够的可比品牌数据。"
    )
    sections = [answer, "# Top品牌与三平台比较"]
    if ranking_rows:
        sections.append(_markdown_table(
            ["排名", "品牌", "GMV Actual", "同期GMV", "Evol%", "GMV增长额"],
            [[
                str(row.get("rank")), str(row.get("brand")), _money(row.get("gmv_actual")),
                _money(row.get("gmv_prior")), _pct(row.get("evol")), _money(row.get("gmv_growth")),
            ] for row in ranking_rows],
        ))
    for brand_row in matrix.get("brands") or []:
        brand = str(brand_row.get("brand") or "")
        chosen = selected_by_brand.get(brand) or {}
        sections.extend([
            f"## {brand}｜三平台生意",
            _markdown_table(
                ["平台", "GMV Actual", "同期GMV", "Evol%", "GMV增长额", "平台占比", "增长贡献"],
                [[
                    str(PLATFORM_LABELS.get(str(row.get("platform")), row.get("platform"))),
                    _money(row.get("gmv_actual")), _money(row.get("gmv_prior")), _pct(row.get("evol")),
                    _money(row.get("gmv_growth")), _pct(row.get("weight")),
                    _pct(row.get("growth_contribution")),
                ] for row in brand_row.get("platforms") or []],
            ),
        ])
        if chosen:
            metric = str(chosen.get("selection_metric") or "gmv_growth")
            metric_labels = {
                "gmv_actual": "本期GMV规模", "gmv_growth": "GMV增长额",
                "evol": "同比增速", "growth_contribution": "增长贡献",
            }
            direction = (
                "GMV降幅最小" if chosen.get("all_negative") and metric == "gmv_growth" else
                "同比降幅最小" if chosen.get("all_negative") and metric == "evol" else
                f"{metric_labels.get(metric, metric)}最大"
            )
            tie = "（主指标并列，按本期GMV作第二排序）" if chosen.get("tie") else ""
            sections.append(
                f"比较结果：选择{PLATFORM_LABELS.get(str(chosen.get('platform')), chosen.get('platform'))}"
                f"，因为它是该品牌三平台中{direction}的平台{tie}。"
            )
    for brand, report in templates.get("reports", {}).items():
        platform = report.get("platform")
        result = report.get("result") or {}
        sections.extend([
            f"# {brand}｜{PLATFORM_LABELS.get(platform, platform)}深度分析",
            result.get("markdown") or "该分支未返回可用分析。",
        ])
    bet = outputs.get("bet_fanout") or {}
    if bet.get("reports"):
        sections.append("# Top品牌BET补充分析")
        for brand, result in bet["reports"].items():
            sections.extend([f"## {brand}", result.get("markdown") or "BET未返回可用结果。"])
    failures = list(templates.get("failures") or []) + list(bet.get("failures") or [])
    missing_groups = list(selection.get("missing_groups") or [])
    if failures or missing_groups:
        sections.append("# 数据缺口与未完成分支")
        sections.extend([f"- {item}" for item in failures])
        sections.extend([f"- {brand}：没有可用的平台比较数据。" for brand in missing_groups])
    sections.extend([
        "# 分析边界",
        "- 平台选择是基于可观测GMV变化的比较，不表示媒体或促销对GMV的因果关系。",
        "- 缺失数据不会按0参与比较；失败分支不影响其他品牌继续执行。",
    ])
    return {
        "ok": bool(ranking_rows), "markdown": "\n\n".join(sections),
        "meta": {
            "document_ready": True, "domain": "market",
            "document_title": "Pure Mass Top品牌三平台比较与动态下钻",
            "top_brands": [str(row.get("brand")) for row in ranking_rows],
            "selected_platform_by_brand": list(selection.get("brand_platform_pairs") or []),
            "market_result": matrix, "partial_failures": failures,
        },
    }


def _synthesize_single_brand(outputs: dict[str, dict]) -> dict:
    matrix = outputs["platform_matrix"]
    selection = outputs["select_platform"]
    templates = outputs["platform_templates"]
    selected = list(selection.get("brand_platform_pairs") or [])
    chosen = selected[0] if selected else {}
    brand = str(matrix.get("brand") or chosen.get("brand") or "该品牌")
    platform = str(chosen.get("platform") or "")
    metric = str(chosen.get("selection_metric") or "gmv_growth")
    metric_label = {
        "gmv_actual": "本期GMV规模", "gmv_growth": "GMV增长额",
        "evol": "同比增速", "growth_contribution": "增长贡献",
    }.get(metric, metric)
    if chosen:
        direction = "降幅最小" if chosen.get("all_negative") else "最高"
        answer = (
            f"先给结论：{brand}三平台中，{PLATFORM_LABELS.get(platform, platform)}的"
            f"{metric_label}{direction}，因此已继续下钻该平台的生意表现。"
        )
    else:
        answer = f"先给结论：{brand}当前没有足够的平台级数据完成比较与下钻。"
    sections = [answer, matrix.get("formatted_report") or ""]
    for report_brand, report in (templates.get("reports") or {}).items():
        result = report.get("result") or {}
        sections.extend([
            f"# {report_brand}｜{PLATFORM_LABELS.get(str(report.get('platform')), report.get('platform'))}下钻分析",
            result.get("markdown") or "该平台未返回可用分析。",
        ])
    bet = outputs.get("bet_fanout") or {}
    if bet.get("reports"):
        sections.append("# BET投资分析")
        for report_brand, result in bet["reports"].items():
            sections.extend([f"## {report_brand}", result.get("markdown") or "BET未返回可用分析。"])
    failures = list(templates.get("failures") or []) + list(bet.get("failures") or [])
    if failures:
        sections.extend(["# 未完成分支", *[f"- {item}" for item in failures]])
    return {
        "ok": bool(chosen), "markdown": "\n\n".join(part for part in sections if part),
        "meta": {
            "document_ready": True, "domain": "brand_platform_deep_dive",
            "document_title": f"{brand}三平台比较与动态下钻",
            "brand": brand,
            "period": (matrix.get("raw_result") or {}).get("period"),
            "top_brands": [brand], "selected_platform_by_brand": selected,
            "market_result": {"rows": list(matrix.get("rows") or [])},
            "last_result_cache": {"three_platform_competitor_result": matrix.get("raw_result")},
            "partial_failures": failures,
        },
    }


class PlanExecutor:
    def __init__(self, on_progress: Callable[[str], None] | None = None, session: SessionState | None = None):
        self.on_progress = on_progress
        self.session = session

    def execute(self, plan: AgentPlan) -> dict:
        self.plan = plan
        outputs: dict[str, dict] = {}
        executions: list[StepExecution] = []
        statuses: dict[str, str] = {}
        optional_steps = {
            step.step_id for step in plan.steps if step.optional or not step.required
        }
        for step in plan.steps:
            required = step.required and not step.optional
            blocked = [
                dep for dep in step.depends_on
                if statuses.get(dep) not in {"success", "partial"} and dep not in optional_steps
            ]
            if blocked:
                statuses[step.step_id] = "skipped"
                executions.append(StepExecution(
                    step.step_id, step.kind, step.capability_id or step.executor,
                    "skipped", 0, error=f"blocked dependencies: {blocked}",
                ))
                continue
            if step.condition_ref:
                try:
                    should_run = bool(_resolve_ref(step.condition_ref, outputs))
                except KeyError as exc:
                    should_run = False
                    log.warning("plan condition could not be resolved step=%s error=%s", step.step_id, exc)
                if not should_run:
                    statuses[step.step_id] = "skipped"
                    executions.append(StepExecution(
                        step.step_id, step.kind, step.capability_id or step.executor,
                        "skipped", 0, output={"condition": step.condition_ref},
                    ))
                    continue
            started = time.monotonic()
            try:
                output = self._execute_step(step, outputs)
                status = "partial" if output.get("failures") else "success"
                outputs[step.step_id] = output
                statuses[step.step_id] = status
                executions.append(StepExecution(
                    step.step_id, step.kind, step.capability_id or step.executor,
                    status, int((time.monotonic() - started) * 1000), output,
                ))
            except Exception as exc:
                log.exception("executable plan step failed plan=%s step=%s", plan.plan_id, step.step_id)
                statuses[step.step_id] = "failed"
                executions.append(StepExecution(
                    step.step_id, step.kind, step.capability_id or step.executor,
                    "failed", int((time.monotonic() - started) * 1000), error=str(exc),
                ))
                if required:
                    break
                outputs[step.step_id] = {
                    "failures": [
                        f"可选步骤“{step.label}”未完成：{exc}"
                    ],
                    "optional_failure": True,
                }
        if "synthesize" not in outputs:
            failed = next((item.error for item in executions if item.status == "failed" and item.error), None)
            raise RuntimeError(failed or "executable plan did not produce synthesis")
        result = outputs["synthesize"]
        result.setdefault("meta", {})["plan_execution"] = {
            "plan_id": plan.plan_id, "version": plan.version,
            "status": "partial" if any(item.status in {"partial", "failed"} for item in executions) else "success",
            "steps": [item.trace_dict() for item in executions],
        }
        return result

    def _execute_step(self, step: PlanStep, outputs: dict[str, dict]) -> dict:
        capability = step.capability_id or step.executor
        if capability in {
            "template.tm_business", "template.dy_business",
            "template.jd_business", "template.ttl_business",
        }:
            brand = str(step.inputs.get("brand") or "").strip()
            period = step.inputs.get("period")
            aliases = [brand] if brand else []
            runners = {
                "template.tm_business": run_default_chain,
                "template.dy_business": run_douyin_business_chain,
                "template.jd_business": run_jd_business_chain,
                "template.ttl_business": run_three_platform_competitor_chain,
            }
            from bot.report_events import notify
            platform={'template.tm_business':'TM','template.dy_business':'DY','template.jd_business':'JD','template.ttl_business':'TTL'}[capability]
            notify('start',platform,brand,period)
            try:
                if capability == "template.ttl_business":
                    result = runners[capability](brand, period, on_progress=self.on_progress)
                else:
                    result = runners[capability](
                        brand, period, brand_aliases=aliases, on_progress=self.on_progress,
                    )
            except Exception:
                notify('finish',platform,brand,period,{'ok':False,'meta':{'failure_kind':'EC_TEMPLATE_EXCEPTION'}})
                raise
            notify('finish',platform,brand,period,result)
            if not result.get("ok", True):
                raise RuntimeError(result.get("markdown") or f"{capability} failed")
            return result
        if capability == "template.bet":
            result = run_media_chain(
                str(step.inputs.get("brand") or ""), step.inputs.get("period"),
                brand_aliases=[str(step.inputs.get("brand") or "")],
                media_scope="full_bet" if step.inputs.get("media_mode") == "OVERALL_BET" else "channel_only",
                media_mode=step.inputs.get("media_mode"),
                media_channels=list(step.inputs.get("media_channels") or []),
                on_progress=self.on_progress,
            )
            if not result.get("ok", True):
                raise RuntimeError(result.get("markdown") or "BET analysis failed")
            return result
        if capability == "market.trend":
            result = query_market_trend(
                step.inputs.get("period"), step.inputs.get("segment") or "PURE MASS",
                step.inputs.get("platform") or "TTL", "summary",
                step.inputs.get("category") or "TOTAL BEAUTY",
            )
            if result.get("error"):
                raise RuntimeError(result.get("message") or str(result.get("error")))
            return result
        if capability == "followup.answer":
            if self.session is None:
                raise RuntimeError("follow-up capability requires session context")
            from bot.chains.followup_v2_chain import run_followup_v2_chain
            result = run_followup_v2_chain(
                str(step.inputs.get("user_text") or ""), self.session,
                brand=step.inputs.get("brand"), period=step.inputs.get("period"),
                brand_aliases=self.session.ec_context.brand_aliases,
                on_progress=self.on_progress,
            )
            if not result.get("ok", True):
                raise RuntimeError(result.get("markdown") or "follow-up analysis failed")
            return result
        if capability == "synthesis.grounded_answer":
            # An atomic ranking is already fully computed and checked by the
            # ranking capability.  The existing market formatter renders its
            # five rows without asking the long-report synthesizer to ingest
            # thousands of candidate and coverage rows.
            if (
                self.plan.route_type == "market_brand_ranking"
                and len(outputs) == 1
                and (ranking := next(iter(outputs.values()), {})).get("query_meta", {}).get("tool")
                == "query_market_top_brands"
            ):
                from bot.market_formatter import format_market_result
                return format_market_result(ranking)
            from bot.execution_policy import publish_available_data
            publish_available_data(outputs, self.on_progress)
            from bot.grounded_summary import synthesize_grounded_answer
            return synthesize_grounded_answer(
                str(step.inputs.get("user_text") or ""),
                self.plan.to_dict(), outputs,
                platform=step.inputs.get("platform"),
            )
        if capability == "opportunity.business_report":
            return _execute_opportunity_report(step, self.session, self.on_progress)
        if capability == "derive.opportunity_reasoning_pack":
            evidence = dict(outputs.get("evidence", {}).get("evidence") or {})
            if not evidence:
                raise RuntimeError("opportunity plan has no evidence envelope")
            if self.on_progress:
                self.on_progress("生意数据已完成，正在组织品类、渠道、系列和商品证据…")
            return build_reasoning_pack(evidence)
        if capability == "synthesis.opportunity_insight":
            evidence_step = outputs.get("evidence") or {}
            evidence = dict(evidence_step.get("evidence") or {})
            if self.on_progress:
                self.on_progress("证据已准备完成，正在结合分析Skills识别机会并核对行动边界…")
            reasoning = run_opportunity_reasoning_v3(
                str(step.inputs.get("user_text") or ""), evidence,
                selected_skills=list(step.inputs.get("selected_skills") or []),
            )
            report = evidence_step.get("report_result") or {}
            report_markdown = str(report.get("markdown") or "").strip()
            markdown = reasoning["markdown"]
            if report_markdown:
                markdown = f"{markdown}\n\n# 生意事实报告\n\n{report_markdown}"
            brand = str(step.inputs.get("brand") or evidence.get("brand_surface") or evidence.get("brand") or "")
            period = str(step.inputs.get("period") or (evidence.get("period") or {}).get("raw") or "")
            platform = str(step.inputs.get("platform") or evidence.get("platform") or "").upper()
            report_meta = dict(report.get("meta") or {})
            return {
                "ok": True,
                "markdown": markdown,
                "meta": {
                    **report_meta,
                    "brand": brand, "period": period, "platform": platform,
                    "domain": "ec", "report_type": "opportunity_analysis",
                    "document_ready": True,
                    "document_title": f"{brand} {period} 生意机会与行动方案",
                    "last_result_cache": report_meta.get("last_result_cache") or {},
                    "ec_report_evidence": evidence,
                    "reasoning_pack": reasoning.get("reasoning_pack"),
                    "semantic_request": step.inputs.get("semantic_request"),
                    "selected_skills": list(step.inputs.get("selected_skills") or []),
                    "used_evidence_ids": reasoning.get("used_evidence_ids") or [],
                    "evidence_requests": reasoning.get("evidence_requests") or [],
                    "validation_result": {
                        "used_llm": reasoning.get("used_llm"),
                        "error": reasoning.get("validation_error"),
                        "evidence_rounds": reasoning.get("evidence_rounds", 0),
                    },
                    "evidence_reused": bool(evidence_step.get("reused")),
                },
            }
        if capability == "market.top_brands":
            if self.on_progress:
                self.on_progress("正在确定Pure Mass Top品牌…")
            return _execute_ranking(step, outputs)
        if capability == "market.brand_platform_matrix":
            if self.on_progress:
                count = len(outputs.get("ranking", {}).get("top_brands") or [])
                self.on_progress(f"已找到{count}个品牌，正在比较它们在天猫、抖音和京东的增长表现…")
            return _execute_platform_matrix(step, outputs)
        if capability == "brand.three_platform_matrix":
            if self.on_progress:
                self.on_progress("正在获取该品牌的天猫、抖音和京东生意数据…")
            return _execute_brand_platform_matrix(step, outputs)
        if capability == "derive.per_group_argmax":
            return _execute_derive(step, outputs)
        if capability == "template.brand_platform_business":
            return _execute_platform_templates(step, outputs, self.on_progress)
        if capability == "template.overall_bet":
            return _execute_bet(step, outputs, self.on_progress)
        if capability == "synthesis.evidence_report":
            if self.on_progress:
                self.on_progress("数据步骤已完成，正在整理品牌之间的共性和差异…")
            return _synthesize(outputs)
        if capability == "synthesis.single_brand_platform_report":
            if self.on_progress:
                self.on_progress("平台比较和下钻已完成，正在整合生意与投资结论…")
            return _synthesize_single_brand(outputs)
        if capability == "context.ec_report_evidence":
            if self.session is None:
                raise RuntimeError("follow-up executable plan requires session context")
            ctx = self.session.ec_context
            evidence = ctx.latest_report_evidence
            if not evidence and ctx.recent_evidence and isinstance(ctx.recent_evidence[-1], dict):
                evidence = ctx.recent_evidence[-1]
            if not evidence:
                evidence = evidence_from_cache(
                    ctx.report_cache or self.session.last_result_cache or {}, brand=ctx.brand,
                    period=ctx.period, platform=ctx.platform,
                )
            return {
                "rows": [evidence] if evidence else [], "evidence": evidence,
                "brand": ctx.brand, "period": ctx.period, "platform": ctx.platform,
                "user_text": str(step.inputs.get("user_text") or ""),
            }
        if capability == "derive.resolve_followup_subject":
            if self.session is None:
                raise RuntimeError("follow-up executable plan requires session context")
            plan = build_followup_plan(
                str(step.inputs.get("user_text") or ""), self.session,
                brand=step.inputs.get("brand"), period=step.inputs.get("period"),
            )
            return {"rows": [plan.to_dict()], "followup_plan": plan}
        if capability == "derive.check_evidence_coverage":
            from bot.chains.followup_v2_chain import _evidence_result
            followup = outputs["resolve_subject"]["followup_plan"]
            cached = _evidence_result(
                followup, self.session, str(outputs.get("context", {}).get("user_text") or ""),
            )
            formatted = format_followup_result(followup.to_dict(), cached) if cached else None
            return {
                "rows": list((cached or {}).get("rows") or []),
                "requires_query": cached is None, "cached_result": cached,
                "formatted": formatted, "followup_plan": followup,
            }
        if capability == "ec.platform_followup":
            from bot.chains.followup_v2_chain import run_followup_v2_chain
            if self.session is None:
                raise RuntimeError("follow-up executable plan requires session context")
            followup = outputs["coverage"]["followup_plan"]
            return run_followup_v2_chain(
                str(step.inputs.get("user_text") or ""), self.session,
                brand=followup.brand, period=followup.period.get("raw"),
                brand_aliases=self.session.ec_context.brand_aliases,
                on_progress=self.on_progress,
            )
        if capability == "synthesis.ec_followup":
            result = outputs.get("query_missing") or outputs.get("coverage", {}).get("formatted")
            if not result:
                raise RuntimeError("follow-up plan produced no grounded evidence")
            return result
        raise RuntimeError(f"capability is not executable: {capability}")


def execute_agent_plan(plan: AgentPlan, on_progress=None, session: SessionState | None = None) -> dict:
    return PlanExecutor(on_progress=on_progress, session=session).execute(plan)
