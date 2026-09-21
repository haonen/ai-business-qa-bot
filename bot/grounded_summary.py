from __future__ import annotations

"""Evidence-addressable, two-stage final answer synthesis."""

from dataclasses import asdict, dataclass, field
import json
import logging
import math
import os
import re
import time
from typing import Any

from bot.brand_reference import _load_reference
from bot.ec_report_evidence import build_ec_report_evidence
from bot.utils import extract_json_object, llm_client, llm_model


log = logging.getLogger(__name__)
CAUSAL_ASSERTIONS = ("导致了", "证明了", "一定会", "必然会", "确定是因为", "直接带来")
PLATFORM_TERMS = {
    "TM": ("天猫", "TMALL"), "DY": ("抖音", "DOUYIN"),
    "JD": ("京东", "JINGDONG"), "TTL": ("三平台",),
}
PLACEHOLDER_RE = re.compile(r"\{\{\s*(evidence|calc|scope|period):([^{}]+?)\s*\}\}")
CALCULATION_OPERATORS = {
    "subtract", "add", "divide", "percent_change", "percentage_point_change",
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


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    metric: str
    value: Any
    unit: str | None = None
    subject: str | None = None
    dimension: str | None = None
    brand: str | None = None
    platform: str | None = None
    period: dict[str, Any] = field(default_factory=dict)
    source: str | None = None
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["limitations"] = list(self.limitations)
        return value


@dataclass(frozen=True)
class AnswerClaim:
    text: str
    evidence_ids: tuple[str, ...]
    kind: str = "fact"
    confidence: str = "medium"


@dataclass(frozen=True)
class AnswerEnvelope:
    direct_answer: str
    sections: tuple[dict[str, Any], ...]
    direct_evidence_ids: tuple[str, ...] = ()
    data_gaps: tuple[str, ...] = ()
    coverage_notice: str | None = None
    used_evidence_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "direct_answer": self.direct_answer,
            "direct_evidence_ids": list(self.direct_evidence_ids),
            "sections": list(self.sections),
            "data_gaps": list(self.data_gaps),
            "coverage_notice": self.coverage_notice,
            "used_evidence_ids": list(self.used_evidence_ids),
        }


class AnswerValidationError(ValueError):
    pass


def _platform(meta: dict[str, Any], fallback: str | None) -> str | None:
    explicit = str(meta.get("platform") or fallback or "").upper()
    if explicit in {"TM", "DY", "JD", "TTL"}:
        return explicit
    domain = str(meta.get("domain") or "").casefold()
    return "DY" if "douyin" in domain else "JD" if "jd" in domain else "TM" if "tmall" in domain else fallback


def _coverage_notice(meta: dict[str, Any]) -> str | None:
    period = meta.get("period_meta") or {}
    adjustment = period.get("period_adjustment") or meta.get("period_adjustment") or {}
    if not adjustment:
        requested_end = period.get("requested_end")
        effective_end = period.get("current_end")
        if requested_end and effective_end and requested_end != effective_end:
            adjustment = {"requested_end": requested_end, "effective_end": effective_end}
    if not adjustment:
        return None
    return (
        f"> **数据覆盖提示**：请求截止到{adjustment.get('requested_end') or adjustment.get('requested_display')}，"
        f"实际按最新可用日{adjustment.get('effective_end') or adjustment.get('effective_display')}进行同日同比。"
    )


def _from_ec_envelope(envelope: dict[str, Any]) -> list[EvidenceRecord]:
    records = []
    period = dict(envelope.get("period_range") or envelope.get("period") or {})
    source = ",".join(
        str(item.get("table") or item.get("source") or "")
        for item in envelope.get("data_sources") or [] if isinstance(item, dict)
    ) or envelope.get("report_type")
    for row in envelope.get("records") or []:
        evidence_id = str(row.get("evidence_id") or "")
        if not evidence_id:
            continue
        for metric in (
            "gmv_actual", "gmv_prior", "gmv_change", "gmv_growth", "evol", "share",
            "share_delta", "growth_contribution", "concentration", "rank",
        ):
            if row.get(metric) is None:
                continue
            records.append(EvidenceRecord(
                evidence_id=f"{evidence_id}:{metric}", metric=metric,
                value=row.get(metric), unit="ratio" if metric in {
                    "evol", "share", "share_delta", "growth_contribution", "concentration",
                } else "yuan" if metric.startswith("gmv") else None,
                subject=str(row.get("subject") or ""), dimension=row.get("dimension"),
                brand=envelope.get("brand_surface") or envelope.get("brand"),
                platform=envelope.get("platform"), period=period, source=str(source or ""),
                limitations=tuple(envelope.get("unsupported_dimensions") or []),
            ))
    return records


def _inferred_unit(metric: str) -> str | None:
    key = str(metric).casefold()
    if any(token in key for token in (
        "evol", "share", "weight", "ratio", "rate", "contribution", "concentration",
    )):
        return "ratio"
    if key.endswith("_yuan") or key.startswith("gmv"):
        return "yuan"
    if key.endswith("_million"):
        return "million_yuan"
    return None


def _generic_records(
    value: Any, prefix: str, *, subject: str | None = None,
    context: dict[str, Any] | None = None,
) -> list[EvidenceRecord]:
    records: list[EvidenceRecord] = []
    context = context or {}
    period = context.get("period_meta") or context.get("period") or {}
    if not isinstance(period, dict):
        period = {"raw": str(period)}
    limitations = tuple(str(item) for item in context.get("limitations") or [])
    if isinstance(value, dict):
        next_subject = str(value.get("brand") or value.get("subject") or value.get("name") or subject or "")
        actual = value.get("gmv_actual")
        prior = value.get("gmv_prior")
        if (
            value.get("gmv_change") is None
            and isinstance(actual, (int, float)) and not isinstance(actual, bool)
            and isinstance(prior, (int, float)) and not isinstance(prior, bool)
            and math.isfinite(float(actual)) and math.isfinite(float(prior))
        ):
            records.append(EvidenceRecord(
                f"{prefix}.gmv_change_derived", "gmv_change",
                float(actual) - float(prior), unit="yuan",
                subject=next_subject or None,
                brand=str(context.get("brand") or "") or None,
                platform=_platform(context, None), period=dict(period),
                source=str(context.get("report_type") or context.get("domain") or "plan_output"),
                limitations=limitations,
            ))
        if (
            value.get("evol") is None
            and isinstance(actual, (int, float)) and not isinstance(actual, bool)
            and isinstance(prior, (int, float)) and not isinstance(prior, bool)
            and math.isfinite(float(actual)) and math.isfinite(float(prior))
            and float(prior) != 0
        ):
            records.append(EvidenceRecord(
                f"{prefix}.evol_derived", "evol",
                (float(actual) - float(prior)) / float(prior), unit="ratio",
                subject=next_subject or None,
                brand=str(context.get("brand") or "") or None,
                platform=_platform(context, None), period=dict(period),
                source=str(context.get("report_type") or context.get("domain") or "plan_output"),
                limitations=limitations,
            ))
        for key, item in value.items():
            if key in {"markdown", "meta", "raw_result", "formatted_report"}:
                continue
            path = f"{prefix}.{key}"
            if isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(float(item)):
                records.append(EvidenceRecord(
                    path, key, item, unit=_inferred_unit(key),
                    subject=next_subject or None,
                    brand=str(context.get("brand") or "") or None,
                    platform=_platform(context, None), period=dict(period),
                    source=str(context.get("report_type") or context.get("domain") or "plan_output"),
                    limitations=limitations,
                ))
            elif isinstance(item, (dict, list)):
                records.extend(_generic_records(
                    item, path, subject=next_subject or subject, context=context,
                ))
    elif isinstance(value, list):
        for index, item in enumerate(value[:100]):
            records.extend(_generic_records(
                item, f"{prefix}[{index}]", subject=subject, context=context,
            ))
    return records


def build_evidence_envelope(outputs: dict[str, dict], *, platform: str | None = None) -> dict[str, Any]:
    records: list[EvidenceRecord] = []
    source_reports = []
    data_gaps = []
    notices = []
    base_meta: dict[str, Any] = {}
    for step_id, output in outputs.items():
        if step_id == "synthesize" or not isinstance(output, dict):
            continue
        meta = dict(output.get("meta") or {})
        if not meta:
            meta = {
                key: output.get(key) for key in (
                    "brand", "period", "period_meta", "platform", "limitations",
                    "report_type", "domain",
                ) if output.get(key) is not None
            }
        if meta and not base_meta:
            base_meta = meta
        report_markdown = str(output.get("markdown") or "").strip()
        if report_markdown:
            source_reports.append({"step_id": step_id, "markdown": report_markdown})
        notice = _coverage_notice(meta)
        if notice and notice not in notices:
            notices.append(notice)
        resolved_platform = _platform(meta, platform)
        ec = build_ec_report_evidence(meta, resolved_platform) if meta.get("last_result_cache") else {}
        if ec and ec.get("records"):
            records.extend(_from_ec_envelope(ec))
        else:
            records.extend(_generic_records(output, f"ev:{step_id}", context=meta))
            if meta.get("last_result_cache"):
                records.extend(_generic_records(
                    meta["last_result_cache"], f"ev:{step_id}.cache",
                    subject=str(meta.get("brand") or "") or None,
                    context=meta,
                ))
        if output.get("failures"):
            data_gaps.extend(str(item) for item in output.get("failures") or [])
    deduped = {record.evidence_id: record for record in records}
    return {
        "schema_version": "evidence-envelope.v1",
        "scope": {
            "brand": base_meta.get("brand"), "period": str(base_meta.get("period") or ""),
            "platform": _platform(base_meta, platform),
        },
        "records": [record.to_dict() for record in deduped.values()],
        "coverage_notice": "\n\n".join(notices) if notices else None,
        "data_gaps": data_gaps,
        "source_reports": source_reports,
        "base_meta": base_meta,
    }


def _compact_number(value: float, decimals: int = 2) -> str:
    rounded = round(value, decimals)
    if math.isclose(rounded, round(rounded), abs_tol=10 ** -(decimals + 1)):
        return f"{int(round(rounded)):,}"
    return f"{rounded:,.{decimals}f}".rstrip("0").rstrip(".")


def _format_bound_value(value: Any, unit: str | None) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if unit == "ratio":
        return f"{_compact_number(number * 100)}%"
    if unit == "pp":
        return f"{_compact_number(number)}pp"
    if unit == "million_yuan":
        number *= 1_000_000
        unit = "yuan"
    if unit == "yuan":
        absolute = abs(number)
        if absolute >= 100_000_000:
            return f"{_compact_number(number / 100_000_000)}亿元"
        if absolute >= 10_000:
            return f"{_compact_number(number / 10_000)}万元"
        return f"{_compact_number(number)}元"
    return _compact_number(number)


def _calculation_values(
    calculations: Any, record_map: dict[str, dict[str, Any]], cited_ids: tuple[str, ...],
) -> dict[str, str]:
    rendered: dict[str, str] = {}
    for raw in calculations or []:
        if not isinstance(raw, dict):
            raise AnswerValidationError("calculation is not an object")
        calculation_id = str(raw.get("calculation_id") or "").strip()
        operator = str(raw.get("operator") or "").strip()
        operand_ids = tuple(str(item) for item in raw.get("operand_evidence_ids") or [])
        if not calculation_id or calculation_id in rendered:
            raise AnswerValidationError("calculation_id is missing or duplicated")
        if operator not in CALCULATION_OPERATORS:
            raise AnswerValidationError(f"calculation operator is not allowed: {operator}")
        if len(operand_ids) != 2:
            raise AnswerValidationError("calculation requires exactly two evidence operands")
        if any(item not in record_map for item in operand_ids):
            raise AnswerValidationError("calculation references unknown evidence")
        if any(item not in cited_ids for item in operand_ids):
            raise AnswerValidationError("calculation operands must be cited by the claim")
        try:
            left, right = (float(record_map[item].get("value")) for item in operand_ids)
        except (TypeError, ValueError):
            raise AnswerValidationError("calculation operands must be numeric") from None
        if operator == "subtract":
            value = left - right
        elif operator == "add":
            value = left + right
        elif operator == "divide":
            if right == 0:
                raise AnswerValidationError("calculation divides by zero")
            value = left / right
        elif operator == "percent_change":
            if right == 0:
                raise AnswerValidationError("percent change divides by zero")
            value = (left - right) / right
        else:
            value = (left - right) * 100
        requested_unit = str(raw.get("unit") or "").strip() or None
        if requested_unit not in {None, "yuan", "million_yuan", "ratio", "pp"}:
            raise AnswerValidationError(f"calculation unit is not allowed: {requested_unit}")
        default_unit = (
            "ratio" if operator in {"divide", "percent_change"}
            else "pp" if operator == "percentage_point_change"
            else record_map[operand_ids[0]].get("unit")
        )
        rendered[calculation_id] = _format_bound_value(value, requested_unit or default_unit)
    return rendered


def _scope_bindings(evidence: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    scope = {
        str(key): str(value) for key, value in (evidence.get("scope") or {}).items()
        if value is not None
    }
    base_meta = evidence.get("base_meta") or {}
    period_meta = base_meta.get("period_meta") or {}
    period: dict[str, str] = {}
    if isinstance(period_meta, dict):
        for key, value in period_meta.items():
            if isinstance(value, (str, int, float)):
                period[str(key)] = str(value)
        adjustment = period_meta.get("period_adjustment") or {}
        if isinstance(adjustment, dict):
            for key, value in adjustment.items():
                if isinstance(value, (str, int, float)):
                    period.setdefault(str(key), str(value))
    return scope, period


def _render_bound_text(
    template: str, *, cited_ids: tuple[str, ...], calculations: Any,
    record_map: dict[str, dict[str, Any]], evidence: dict[str, Any],
) -> str:
    calc_values = _calculation_values(calculations, record_map, cited_ids)
    scope, period = _scope_bindings(evidence)

    def replace(match: re.Match[str]) -> str:
        namespace, key = match.group(1), match.group(2).strip()
        if namespace == "evidence":
            if key not in record_map:
                raise AnswerValidationError(f"unknown evidence placeholder: {key}")
            if key not in cited_ids:
                raise AnswerValidationError(f"uncited evidence placeholder: {key}")
            row = record_map[key]
            return _format_bound_value(row.get("value"), row.get("unit"))
        if namespace == "calc":
            if key not in calc_values:
                raise AnswerValidationError(f"unknown calculation placeholder: {key}")
            return calc_values[key]
        values = scope if namespace == "scope" else period
        if key not in values:
            raise AnswerValidationError(f"unknown {namespace} placeholder: {key}")
        return values[key]

    rendered = PLACEHOLDER_RE.sub(replace, template)
    if "{{" in rendered or "}}" in rendered:
        raise AnswerValidationError("answer contains an invalid placeholder")
    return rendered.strip()


def _validate_answer(payload: dict[str, Any], evidence: dict[str, Any]) -> AnswerEnvelope:
    if not isinstance(payload, dict):
        raise AnswerValidationError("answer is not a JSON object")
    record_map = {str(row.get("evidence_id")): row for row in evidence.get("records") or []}
    sections = []
    used: list[str] = []
    all_text = []
    for section in payload.get("sections") or []:
        if not isinstance(section, dict):
            continue
        claims = []
        for raw in section.get("claims") or []:
            if not isinstance(raw, dict):
                continue
            template = str(raw.get("text_template") or raw.get("text") or "").strip()
            ids = tuple(str(item) for item in raw.get("evidence_ids") or [])
            if not template:
                continue
            unknown = [item for item in ids if item not in record_map]
            if unknown:
                raise AnswerValidationError(f"unknown evidence ids: {unknown}")
            kind = str(raw.get("kind") or "fact")
            if kind == "fact" and not ids:
                raise AnswerValidationError("fact claim has no evidence ids")
            text = _render_bound_text(
                template, cited_ids=ids, calculations=raw.get("calculations"),
                record_map=record_map, evidence=evidence,
            )
            if any(word in text for word in CAUSAL_ASSERTIONS):
                raise AnswerValidationError("unsupported causal assertion")
            if kind == "action_hypothesis" and not any(word in text for word in ("建议", "假设", "待验证", "可以测试")):
                raise AnswerValidationError("action is not framed as a hypothesis")
            claims.append({
                "text": text, "evidence_ids": list(ids), "kind": kind,
                "confidence": str(raw.get("confidence") or "medium"),
            })
            all_text.append(text)
            used.extend(ids)
        if claims:
            sections.append({"title": str(section.get("title") or "关键发现"), "claims": claims})
    direct_template = str(payload.get("direct_answer_template") or payload.get("direct_answer") or "").strip()
    if not direct_template:
        raise AnswerValidationError("direct_answer is missing")
    direct_ids = tuple(str(item) for item in payload.get("direct_evidence_ids") or [])
    unknown_direct_ids = [item for item in direct_ids if item not in record_map]
    if unknown_direct_ids:
        raise AnswerValidationError(f"unknown direct evidence ids: {unknown_direct_ids}")
    direct = _render_bound_text(
        direct_template, cited_ids=direct_ids,
        calculations=payload.get("direct_calculations"),
        record_map=record_map, evidence=evidence,
    )
    all_text.insert(0, direct)
    all_text_value = "\n".join(all_text)
    allowed_years = set(re.findall(r"20\d{2}", json.dumps({
        "scope": evidence.get("scope"),
        "periods": [row.get("period") for row in record_map.values()],
    }, ensure_ascii=False, default=str)))
    mentioned_years = set(re.findall(r"20\d{2}", all_text_value))
    if mentioned_years - allowed_years:
        raise AnswerValidationError(
            f"answer dates exceed evidence scope: {sorted(mentioned_years - allowed_years)}"
        )
    allowed_platforms = {
        str(value).upper() for value in [
            (evidence.get("scope") or {}).get("platform"),
            *(row.get("platform") for row in record_map.values()),
        ] if value
    }
    for platform, terms in PLATFORM_TERMS.items():
        if platform not in allowed_platforms and any(term in all_text_value.upper() for term in terms):
            raise AnswerValidationError(f"answer platform exceeds evidence scope: {platform}")
    allowed_brand_text = "\n".join(
        str(value or "").casefold() for value in [
            (evidence.get("scope") or {}).get("brand"),
            *(row.get("brand") for row in record_map.values()),
            *(row.get("subject") for row in record_map.values()),
        ] if value
    )
    answer_folded = all_text_value.casefold()
    mappings, _ = _load_reference()
    for chinese, english in mappings.items():
        chinese = str(chinese).strip().casefold()
        english = str(english).strip().casefold()
        if len(chinese) < 2 or len(english) < 3:
            continue
        mentioned = chinese in answer_folded or bool(re.search(
            rf"(?<![a-z0-9]){re.escape(english)}(?![a-z0-9])", answer_folded,
        ))
        allowed = chinese in allowed_brand_text or english in allowed_brand_text
        if mentioned and not allowed:
            raise AnswerValidationError(f"answer brand exceeds evidence scope: {chinese}")
    return AnswerEnvelope(
        direct_answer=direct, sections=tuple(sections),
        direct_evidence_ids=direct_ids,
        data_gaps=tuple(str(item) for item in payload.get("data_gaps") or evidence.get("data_gaps") or []),
        coverage_notice=str(payload.get("coverage_notice") or evidence.get("coverage_notice") or "").strip() or None,
        used_evidence_ids=tuple(dict.fromkeys([*direct_ids, *used])),
    )


def _draft_prompt(question: str, plan: dict[str, Any], evidence: dict[str, Any]) -> str:
    return f"""你是品牌生意分析Agent的最终分析师。
用户原问题：{question}
实际执行计划：{json.dumps(plan, ensure_ascii=False, default=str)}
Evidence Envelope：{json.dumps({k: v for k, v in evidence.items() if k != 'source_reports'}, ensure_ascii=False, default=str)}

请深度分析并直接回答用户，找出规模、增减量、结构和跨维度关系。每个事实都要标注可用的evidence_id。
不得编造行业、竞品、公司动作或因果；行动只能写成待验证假设。
用户没要策略时，不强行给策略。先写直接结论，再写关键发现、缺口和口径。"""


def _format_prompt(question: str, evidence: dict[str, Any], draft: str, error: str | None) -> str:
    repair = f"\n上次输出未通过事实校验：{error}。请修正。" if error else ""
    return f"""将下面的深度分析整理为严格JSON，不得增加任何事实或数字。
用户问题：{question}
Evidence：{json.dumps({k: v for k, v in evidence.items() if k != 'source_reports'}, ensure_ascii=False, default=str)}
分析草稿：{draft}

所有金额、比率、排名和日期都不要写成字面数字，必须使用绑定占位符：
- 证据值：{{{{evidence:完整evidence_id}}}}
- 锁定范围：{{{{scope:brand}}}}、{{{{scope:platform}}}}、{{{{scope:period}}}}
- 覆盖日期：{{{{period:current_start}}}}、{{{{period:current_end}}}}、{{{{period:prior_start}}}}、{{{{period:prior_end}}}}
需要新的加减、除法或增速时，定义calculations并在文字中使用{{{{calc:calculation_id}}}}。
可用operator仅有：subtract、add、divide、percent_change、percentage_point_change。

只返回JSON：
{{"direct_answer_template":"2-4句直接结论","direct_evidence_ids":["完整evidence_id"],
"direct_calculations":[{{"calculation_id":"calc_1","operator":"subtract",
"operand_evidence_ids":["evidence_id_1","evidence_id_2"],"unit":"yuan|ratio|pp"}}],
"sections":[{{"title":"","claims":[
{{"text_template":"","evidence_ids":["完整evidence_id"],"calculations":[],
"kind":"fact|action_hypothesis","confidence":"high|medium|low"}}]}}],
"data_gaps":[],"coverage_notice":null}}
占位符中的evidence_id必须同时列在该句的evidence_ids。{repair}"""


def _audit_prompt(question: str, evidence: dict[str, Any], payload: dict[str, Any]) -> str:
    return f"""你是独立的证据核验员。判断Answer Template中的每个事实、数字、日期、比较和归因是否被其引用的Evidence支持。
用户问题：{question}
Evidence：{json.dumps({k: v for k, v in evidence.items() if k != 'source_reports'}, ensure_ascii=False, default=str)}
Answer Template：{json.dumps(payload, ensure_ascii=False, default=str)}

规则：
1. evidence/calc/scope/period占位符是可验证绑定，不是未完成文字。
2. 若出现占位符之外的具体数字或日期，必须确认它可由引用证据直接支持；否则不通过。
3. 不得将相关性写成因果，建议必须是待验证假设。
4. 只核验事实支持，不要因文风或措辞偏好拒绝。

只返回JSON：{{"passed":true,"issues":[]}}。"""


def _audit_passed(payload: dict[str, Any]) -> tuple[bool, str | None]:
    if not isinstance(payload, dict) or not isinstance(payload.get("passed"), bool):
        raise AnswerValidationError("summary audit returned an invalid schema")
    issues = [str(item) for item in payload.get("issues") or [] if str(item).strip()]
    if payload["passed"]:
        return True, None
    return False, "; ".join(issues) or "semantic evidence audit rejected the answer"


def _fallback(evidence: dict[str, Any]) -> dict[str, Any]:
    reports = [str(item.get("markdown") or "") for item in evidence.get("source_reports") or []]
    markdown = "\n\n".join(item for item in reports if item) or "数据已查询，但暂时无法生成通过事实校验的总结。"
    if evidence.get("coverage_notice"):
        markdown = f"{evidence['coverage_notice']}\n\n{markdown}"
    from bot.execution_policy import enabled
    if enabled():
        markdown += "\n\n补充分析尚未完成；以上保留已取得的数据与报告，未将其视为对全部问题的完整回答。"
    return {
        "ok": bool(reports), "markdown": markdown,
        "meta": {
            **dict(evidence.get("base_meta") or {}),
            "evidence_envelope": evidence, "summary_fallback": True,
            "document_ready": bool(reports),
        },
    }


def synthesize_grounded_answer(
    question: str, plan: dict[str, Any], outputs: dict[str, dict], *, platform: str | None = None,
) -> dict[str, Any]:
    evidence = build_evidence_envelope(outputs, platform=platform)
    if not evidence.get("records") or not os.environ.get("DASHSCOPE_API_KEY"):
        return _fallback(evidence)
    client = llm_client(max_retries=0)
    started = time.monotonic()
    try:
        draft_response = client.chat.completions.create(
            model=llm_model("summary"),
            messages=[{"role": "user", "content": _draft_prompt(question, plan, evidence)}],
            max_tokens=int(os.environ.get("SUMMARY_LLM_MAX_TOKENS", "4000")),
            timeout=float(os.environ.get("SUMMARY_LLM_TIMEOUT", "90")),
            extra_body={
                "enable_thinking": True,
                "reasoning_effort": os.environ.get("DASHSCOPE_SUMMARY_REASONING_EFFORT", "high"),
            },
        )
        draft = draft_response.choices[0].message.content or ""
        error = None
        envelope = None
        formatted = None
        audit_response = None
        for _ in range(2):
            formatted = client.chat.completions.create(
                model=llm_model("summary"),
                messages=[{"role": "user", "content": _format_prompt(question, evidence, draft, error)}],
                max_tokens=int(os.environ.get("SUMMARY_FORMATTER_MAX_TOKENS", "4000")),
                timeout=float(os.environ.get("SUMMARY_FORMATTER_TIMEOUT", "30")),
                response_format={"type": "json_object"},
                extra_body={"enable_thinking": False},
            )
            try:
                answer_payload = extract_json_object(formatted.choices[0].message.content or "")
                candidate = _validate_answer(answer_payload, evidence)
                audit_response = client.chat.completions.create(
                    model=llm_model("summary"),
                    messages=[{
                        "role": "user",
                        "content": _audit_prompt(question, evidence, answer_payload),
                    }],
                    max_tokens=int(os.environ.get("SUMMARY_AUDIT_MAX_TOKENS", "1200")),
                    timeout=float(os.environ.get("SUMMARY_AUDIT_TIMEOUT", "30")),
                    response_format={"type": "json_object"},
                    extra_body={"enable_thinking": False},
                )
                passed, audit_error = _audit_passed(extract_json_object(
                    audit_response.choices[0].message.content or "",
                ))
                if not passed:
                    raise AnswerValidationError(audit_error or "summary audit rejected the answer")
                envelope = candidate
                break
            except Exception as exc:
                error = str(exc)
        if envelope is None:
            raise AnswerValidationError(error or "answer validation failed")
        sections = [envelope.coverage_notice, f"先回答你的问题：{envelope.direct_answer}"]
        for section in envelope.sections:
            sections.extend([
                f"## {section['title']}",
                "\n".join(f"- {claim['text']}" for claim in section["claims"]),
            ])
        if envelope.data_gaps:
            sections.extend(["## 数据缺口与边界", "\n".join(f"- {item}" for item in envelope.data_gaps)])
        source_markdown = "\n\n".join(
            str(item.get("markdown") or "") for item in evidence.get("source_reports") or []
        )
        if source_markdown:
            sections.extend(["# 数据明细", source_markdown])
        markdown = "\n\n".join(item for item in sections if item)
        meta = dict(evidence.get("base_meta") or {})
        meta.update({
            "document_ready": True, "evidence_envelope": evidence,
            "answer_envelope": envelope.to_dict(),
            "used_evidence_ids": list(envelope.used_evidence_ids),
            "summary_model": llm_model("summary"),
            "summary_elapsed_ms": int((time.monotonic() - started) * 1000),
            "summary_usage": {
                "draft": _usage(draft_response),
                "formatter": _usage(formatted),
                "audit": _usage(audit_response),
            },
            "evidence_usage_rate": round(
                len(envelope.used_evidence_ids) / max(1, len(evidence.get("records") or [])), 4,
            ),
        })
        log.info(
            "[grounded_summary] model=%s elapsed_ms=%s evidence=%s used=%s usage=%s",
            llm_model("summary"), meta["summary_elapsed_ms"],
            len(evidence.get("records") or []), len(envelope.used_evidence_ids),
            meta["summary_usage"],
        )
        return {"ok": True, "markdown": markdown, "meta": meta}
    except Exception as exc:
        log.warning("[grounded_summary] synthesis failed: %s", exc)
        result = _fallback(evidence)
        result["meta"]["summary_validation_error"] = str(exc)
        return result
