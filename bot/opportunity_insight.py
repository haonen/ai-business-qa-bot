from __future__ import annotations

"""opportunity_insight Skill：候选筛选、LLM 综合与证据约束校验。

设计原则：LLM 从不生成任何数字。候选行先由 Python 按 contract.json 的量化
门槛筛选（份额是否是噪音、是否份额提升或同比正增长），LLM 只被允许从这份
已过滤的候选里挑选、排序并写一句不含数字的"建议/机会点"措辞；具体的份额、
同比、金额永远由 Python 从候选数据里渲染进最终文案。这样校验只需要确认
LLM 返回的 candidate_id 都确实存在、措辞里没有因果强断言词，不需要对自由文本
做脆弱的数字模糊匹配。
"""

import json
import logging
import os
import re
from functools import lru_cache
from pathlib import Path

from bot.utils import extract_json_object, llm_client, llm_model


log = logging.getLogger(__name__)

_SKILL_DIR = Path(__file__).resolve().parent / "skills" / "followup" / "opportunity-insight"

OPPORTUNITY_HINTS = (
    "机会点", "增长机会", "增长点", "行动方案", "怎么提升", "如何提升", "如何增长",
    "有什么建议", "给点建议", "给出建议", "下一步怎么做", "怎么优化", "如何优化",
    "值得关注", "哪里有潜力", "还能怎么做",
)


def asks_for_opportunity(text: str) -> bool:
    lowered = str(text or "").casefold()
    return any(hint in lowered for hint in OPPORTUNITY_HINTS)


@lru_cache(maxsize=1)
def load_contract() -> dict:
    return json.loads((_SKILL_DIR / "contract.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def load_skill_guidance() -> str:
    return (_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


def _num(value):
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return None if value != value else value  # filters out NaN


def _fmt_pct(value, digits: int = 1) -> str:
    return "—" if value is None else f"{value * 100:.{digits}f}%"


def _fmt_pp(value) -> str:
    if value is None:
        return "—"
    pp = value * 100
    return f"+{pp:.1f}pp" if pp >= 0 else f"{pp:.1f}pp"


def _fmt_evol(value) -> str:
    if value is None:
        return "—"
    pct = value * 100
    return f"+{pct:.0f}%" if pct >= 0 else f"{pct:.0f}%"


def _fmt_gmv(value) -> str:
    if value is None:
        return "—"
    value = float(value)
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:,.1f}M"
    return f"{value / 1_000:,.1f}K"


def _share_and_delta(rows: list[dict], name_key: str, fallback_keys: tuple[str, ...] = ()) -> list[dict]:
    """Share of the given row set, computed from raw GMV rather than trusting
    inconsistently-present precomputed "weight" fields across category /
    key_driver / series rows (series rows in particular carry no weight)."""
    total_current = sum(_num(row.get("gmv_current")) or 0 for row in rows)
    total_prior = sum(_num(row.get("gmv_prior")) or 0 for row in rows)
    out = []
    for row in rows:
        name = row.get(name_key)
        if not name:
            for key in fallback_keys:
                name = row.get(key)
                if name:
                    break
        gmv_current = _num(row.get("gmv_current"))
        gmv_prior = _num(row.get("gmv_prior"))
        share = (gmv_current / total_current) if gmv_current is not None and total_current else None
        share_prior = (gmv_prior / total_prior) if gmv_prior is not None and total_prior else None
        share_delta = (share - share_prior) if share is not None and share_prior is not None else None
        evol = _num(row.get("evol"))
        if evol is None and gmv_current is not None and gmv_prior:
            evol = (gmv_current - gmv_prior) / gmv_prior
        out.append({
            "name": str(name or "").strip(), "gmv_current": gmv_current,
            "share": share, "share_delta": share_delta, "evol": evol,
        })
    return out


def select_opportunity_candidates(evidence: dict) -> list[dict]:
    """Deterministic shortlist. Nothing past this function may add a
    candidate that didn't already clear the contract's thresholds."""
    rules = load_contract()["candidate_rules"]
    min_share = float(rules["min_share_of_total"])
    min_delta = float(rules["min_positive_share_delta_pp"]) / 100.0
    min_evol = float(rules["min_positive_evol"])
    max_per_dim = int(rules["max_candidates_per_dimension"])

    def shortlist(rows: list[dict], dimension: str, name_key: str, fallback_keys: tuple[str, ...] = ()) -> list[dict]:
        scored = _share_and_delta(rows, name_key, fallback_keys)
        picked = [
            row for row in scored
            if row["name"]
            and row["share"] is not None and row["share"] >= min_share
            and (
                (row["share_delta"] is not None and row["share_delta"] >= min_delta)
                or (row["evol"] is not None and row["evol"] >= min_evol)
            )
        ]
        picked.sort(key=lambda row: (
            row["share_delta"] if row["share_delta"] is not None else -1,
            row["evol"] if row["evol"] is not None else -1,
        ), reverse=True)
        for row in picked:
            row["dimension"] = dimension
        return picked[:max_per_dim]

    candidates: list[dict] = []
    candidates += shortlist(evidence.get("categories") or [], "category", "category_cn", ("category",))
    candidates += shortlist(evidence.get("key_drivers") or [], "key_driver", "key_driver")
    for series_rows in (evidence.get("series_scopes") or {}).values():
        candidates += shortlist(series_rows, "series", "product_line")
    for index, candidate in enumerate(candidates, 1):
        candidate["candidate_id"] = f"cand_{index}"
    return candidates


def _render_candidate_fact(candidate: dict) -> str:
    dimension_label = {"category": "品类", "key_driver": "Key Driver", "series": "系列"}.get(
        candidate["dimension"], candidate["dimension"],
    )
    parts = [f"{candidate['name']}（{dimension_label}）份额{_fmt_pct(candidate['share'])}"]
    if candidate.get("share_delta") is not None:
        parts.append(f"份额变化{_fmt_pp(candidate['share_delta'])}")
    if candidate.get("evol") is not None:
        parts.append(f"同比{_fmt_evol(candidate['evol'])}")
    if candidate.get("gmv_current") is not None:
        parts.append(f"GMV {_fmt_gmv(candidate['gmv_current'])}")
    return "，".join(parts)


def _forbidden_words() -> list[str]:
    contract = load_contract()
    words = list(contract.get("forbidden_causal_words") or [])
    try:
        from bot.skills.loader import load_narrative_config
        words += list(load_narrative_config().get("wording_blacklist") or [])
    except Exception:
        pass
    return words


def _validate_picks(raw: dict, candidate_by_id: dict) -> tuple[list[dict], str | None]:
    picks = raw.get("picks") if isinstance(raw, dict) else None
    if not isinstance(picks, list) or not picks:
        return [], "返回结果没有picks数组"
    forbidden = _forbidden_words()
    max_bullets = int(load_contract()["output"]["max_bullets"])
    validated = []
    for item in picks[:max_bullets]:
        if not isinstance(item, dict):
            continue
        candidate_id = str(item.get("candidate_id") or "")
        reason = str(item.get("reason") or "").strip()
        if candidate_id not in candidate_by_id:
            return [], f"candidate_id={candidate_id!r}不在候选列表中"
        if not reason:
            return [], f"candidate_id={candidate_id!r}没有给出理由"
        if any(word in reason for word in forbidden):
            hit = next(word for word in forbidden if word in reason)
            return [], f"理由包含禁用因果断言词{hit!r}"
        if re.search(r"\d", reason):
            return [], f"理由里不应包含具体数字（数字由系统自动附加）：{reason!r}"
        validated.append({"candidate_id": candidate_id, "reason": reason})
    if not validated:
        return [], "没有通过校验的picks"
    return validated, None


def _fallback_bullets(candidates: list[dict]) -> list[dict]:
    """Deterministic, LLM-free fallback so a repeated validation failure
    never returns an empty answer."""
    rules = load_contract()["candidate_rules"]
    dominant_share = float(rules.get("min_share_of_total", 0.03)) * 3
    picks = []
    for candidate in candidates[: int(load_contract()["output"]["max_bullets"])]:
        if candidate.get("share_delta") is not None and candidate["share_delta"] > 0:
            reason = "份额持续提升，值得考虑加大资源投入"
        elif candidate.get("share") is not None and candidate["share"] >= dominant_share:
            reason = "现有占比已经不小，值得优先巩固"
        else:
            reason = "同比增速较快，值得关注是否可以复制打法"
        picks.append({"candidate_id": candidate["candidate_id"], "reason": reason})
    return picks


def generate_opportunity_bullets(
    user_question: str, candidates: list[dict], *, brand: str, period: str,
) -> tuple[list[str], bool]:
    """Returns (rendered bullet strings, used_llm). Every number in the
    rendered bullets is produced by `_render_candidate_fact`, never by the
    LLM — the LLM only supplies the ordering and the non-numeric "why this
    might be worth doing" clause, which is what `_validate_picks` enforces."""
    candidate_by_id = {candidate["candidate_id"]: candidate for candidate in candidates}
    picks: list[dict] = []
    used_llm = False
    if candidates and os.environ.get("DASHSCOPE_API_KEY"):
        prompt_candidates = [
            {
                "candidate_id": candidate["candidate_id"], "dimension": candidate["dimension"],
                "name": candidate["name"], "share_rank_hint": index,
            }
            for index, candidate in enumerate(candidates, 1)
        ]
        base_prompt = f"""{load_skill_guidance()}

品牌：{brand}，期间：{period}
用户问题：{user_question}

候选列表（已经过份额和增速门槛筛选，你只能从中挑选，不能新增候选）：
{json.dumps(prompt_candidates, ensure_ascii=False)}

请从候选里选出最多{load_contract()['output']['max_bullets']}个最值得作为增长机会点的候选，按重要性从高到低排序。
每个候选写一句不超过40字的理由，说明"为什么值得关注/可以考虑往这个方向投入"，
理由里禁止出现任何数字（具体份额和同比会由系统自动附加，你写了也会被丢弃并要求重写），
必须使用"值得关注/可以考虑/建议"等假设性措辞，禁止使用"会带来增长/证明了/一定能"等断言词。
只返回JSON：{{"picks":[{{"candidate_id":"...","reason":"..."}}]}}"""
        client = llm_client(max_retries=0)
        extra = ""
        for attempt in range(3):
            try:
                response = client.chat.completions.create(
                    model=llm_model("summary"),
                    messages=[{"role": "user", "content": base_prompt + extra}],
                    max_tokens=600,
                    timeout=float(os.environ.get("LLM_REQUEST_TIMEOUT", "30")),
                    response_format={"type": "json_object"},
                    extra_body={"enable_thinking": False},
                )
                raw = extract_json_object(response.choices[0].message.content or "")
                validated, error = _validate_picks(raw, candidate_by_id)
                if not error:
                    picks = validated
                    used_llm = True
                    break
                extra = f"\n\n上次结果有问题：{error}。请重新生成，严格遵守以上规则。"
            except Exception as exc:
                log.warning("[opportunity_insight] llm attempt %s failed: %s", attempt, exc)
    if not picks:
        picks = _fallback_bullets(candidates)
    bullets = []
    for pick in picks:
        candidate = candidate_by_id.get(pick["candidate_id"])
        if not candidate:
            continue
        bullets.append(f"• {_render_candidate_fact(candidate)}。{pick['reason']}。")
    return bullets, used_llm


def format_opportunity_result(
    brand: str, period: str, platform: str | None, candidates: list[dict], bullets: list[str],
) -> dict:
    platform_label = {"TM": "天猫", "DY": "抖音", "JD": "京东", "TTL": "三平台"}.get(str(platform or "").upper(), "")
    scope = f"范围：{brand}，{period}" + (f"，{platform_label}。" if platform_label else "。")
    if not candidates:
        markdown = "\n\n".join([
            scope,
            "当前证据里没有品类、Key Driver或系列同时满足份额和增速的最低门槛，"
            "暂时找不到有把握的增长机会点建议。",
        ])
    else:
        markdown = "\n\n".join([
            "**增长机会点（基于现有证据的建议，非因果结论）**", scope, *bullets,
            "_以上建议基于内部GMV份额和同比趋势推导，不代表媒体投入、竞品或行业因素的因果验证。_",
        ])
    return {
        "markdown": markdown,
        "meta": {
            "brand": brand, "period": period, "report_type": "opportunity_insight",
            "skill": "opportunity_insight", "domain": "ec",
            "document_ready": False, "candidate_count": len(candidates),
        },
    }


_V3_SKILL_FILES = {
    "analysis-drill": Path(__file__).resolve().parent / "skills" / "followup" / "analysis-drill" / "SKILL.md",
    "opportunity-insight": _SKILL_DIR / "SKILL.md",
    "category-drill": Path(__file__).resolve().parent / "skills" / "category_drill.md",
    "key-driver": Path(__file__).resolve().parent / "skills" / "key_driver.md",
    "sku-investigation": Path(__file__).resolve().parent / "skills" / "sku_investigation.md",
}

_INSIGHT_TYPES = {"growth_opportunity", "recovery_opportunity", "risk", "action_hypothesis"}
_REQUEST_CAPABILITIES = {
    "tm_brand_business", "dy_brand_business", "jd_brand_business",
    "ttl_brand_business", "opportunity_insight",
}
_REQUEST_METRICS = {
    "gmv_actual", "gmv_prior", "gmv_growth", "evol", "share",
    "share_delta", "growth_contribution", "concentration", "rank",
}


def _record_score(record: dict, mode: str) -> tuple[float, float]:
    actual = abs(_num(record.get("gmv_actual")) or 0)
    growth = _num(record.get("gmv_growth")) or 0
    evol = _num(record.get("evol")) or 0
    contribution = _num(record.get("growth_contribution")) or 0
    if mode == "scale":
        return actual, abs(growth)
    if mode == "growth":
        return growth, actual
    if mode == "decline":
        return -growth, actual
    return abs(contribution), actual + abs(evol)


def build_reasoning_pack(evidence: dict, *, max_records: int = 60) -> dict:
    """Build a broad, bounded view without filtering away decline/recovery signals."""
    source = [dict(row) for row in evidence.get("records") or [] if isinstance(row, dict)]
    valid = []
    for row in source:
        if not row.get("evidence_id") or not row.get("subject"):
            continue
        if str(row.get("subject")) in {"未分类", "NULL", "None"}:
            continue
        if all(_num(row.get(key)) is None for key in ("gmv_actual", "gmv_growth", "evol", "share")):
            continue
        valid.append(row)
    selected: list[dict] = []
    dimensions = list(dict.fromkeys(str(row.get("dimension") or "unknown") for row in valid))
    for dimension in dimensions:
        rows = [row for row in valid if str(row.get("dimension") or "unknown") == dimension]
        if dimension == "overall":
            selected.extend(rows)
            continue
        for mode in ("scale", "growth", "decline", "contribution"):
            selected.extend(sorted(rows, key=lambda row: _record_score(row, mode), reverse=True)[:5])
    deduped = []
    seen = set()
    for row in selected:
        evidence_id = str(row.get("evidence_id"))
        if evidence_id in seen:
            continue
        seen.add(evidence_id)
        deduped.append(row)
    deduped = deduped[:max_records]
    included = {str(row.get("evidence_id")) for row in deduped}
    omitted = [
        {"evidence_id": row.get("evidence_id"), "dimension": row.get("dimension"), "subject": row.get("subject")}
        for row in valid if str(row.get("evidence_id")) not in included
    ]
    manifest = dict(evidence.get("manifest") or {})
    manifest.update({
        "eligible_record_count": len(valid),
        "included_record_count": len(deduped),
        "omitted_record_count": len(omitted),
    })
    return {
        "evidence_id": evidence.get("evidence_id"),
        "scope": {
            "brand": evidence.get("brand_surface") or evidence.get("brand"),
            "canonical_brand": evidence.get("canonical_brand") or evidence.get("brand"),
            "period": evidence.get("period_range") or evidence.get("period"),
            "platform": evidence.get("platform"),
        },
        "manifest": manifest,
        "records": deduped,
        "omitted_slices": list(evidence.get("omitted_slices") or []) + omitted,
        "quality": dict(evidence.get("quality") or {}),
    }


def _selected_skill_guidance(selected_skills: list[str]) -> str:
    sections = []
    for name in selected_skills:
        path = _V3_SKILL_FILES.get(name)
        if not path or not path.exists():
            continue
        sections.append(f"## Skill: {name}\n{path.read_text(encoding='utf-8')}")
    return "\n\n".join(sections)


def _validated_evidence_requests(
    requests: object, available_records: list[dict], *, locked_platform: str | None = None,
) -> tuple[list[dict], str | None]:
    if not requests:
        return [], None
    if not isinstance(requests, list):
        return [], "evidence_requests must be a list"
    dimensions = {str(row.get("dimension") or "") for row in available_records}
    subjects = {str(row.get("subject") or "") for row in available_records}
    expected_capability = {
        "TM": "tm_brand_business", "DY": "dy_brand_business",
        "JD": "jd_brand_business", "TTL": "ttl_brand_business",
    }.get(str(locked_platform or "").upper())
    validated = []
    for request in requests[:2]:
        if not isinstance(request, dict):
            return [], "evidence request must be an object"
        capability_id = str(request.get("capability_id") or "opportunity_insight")
        dimension = str(request.get("dimension") or "")
        subject_refs = [str(value) for value in request.get("subject_refs") or []][:5]
        metrics = [str(value) for value in request.get("required_metrics") or []][:8]
        filters = dict(request.get("filters") or {})
        if capability_id not in _REQUEST_CAPABILITIES:
            return [], f"unsupported evidence capability={capability_id!r}"
        if expected_capability and capability_id not in {expected_capability, "opportunity_insight"}:
            return [], "evidence request targets a different platform capability"
        if not dimension or dimension not in dimensions:
            return [], f"unknown evidence dimension={dimension!r}"
        if subject_refs and any(value not in subjects for value in subject_refs):
            return [], "evidence request references an unknown subject"
        if any(value not in _REQUEST_METRICS for value in metrics):
            return [], "evidence request contains an unknown metric"
        if set(filters) - {"subject", "category", "key_driver", "series", "limit"}:
            return [], "evidence request attempts to modify locked scope"
        validated.append({
            "capability_id": capability_id, "dimension": dimension,
            "subject_refs": subject_refs, "filters": filters,
            "required_metrics": metrics,
            "reason": str(request.get("reason") or "").strip(),
        })
    return validated, None


def _validate_v3_result(
    raw: dict, records: list[dict], *, available_records: list[dict] | None = None,
    locked_platform: str | None = None,
) -> tuple[dict | None, str | None]:
    if not isinstance(raw, dict):
        return None, "result is not an object"
    direct = str(raw.get("direct_answer") or "").strip()
    insights = raw.get("insights")
    if not direct or not isinstance(insights, list) or not insights:
        return None, "direct_answer and insights are required"
    known = {str(row.get("evidence_id")) for row in records}
    forbidden = _forbidden_words()
    validated = []
    for item in insights[:5]:
        if not isinstance(item, dict):
            continue
        insight_type = str(item.get("insight_type") or "")
        evidence_ids = [str(value) for value in item.get("evidence_ids") or []]
        strings = [
            str(item.get(key) or "").strip()
            for key in ("thesis", "reasoning", "action_hypothesis", "validation_metric")
        ]
        if insight_type not in _INSIGHT_TYPES:
            return None, f"unsupported insight_type={insight_type!r}"
        if not 1 <= len(evidence_ids) <= 4 or any(value not in known for value in evidence_ids):
            return None, "insight references unknown or invalid evidence_ids"
        if not strings[0] or not strings[1] or not strings[2] or not strings[3]:
            return None, "insight text and validation_metric are required"
        combined = " ".join(strings)
        if re.search(r"\d", combined):
            return None, "LLM narrative must not create numbers"
        if any(word in combined for word in forbidden):
            return None, "insight contains unsupported causal language"
        validated.append({
            "insight_type": insight_type,
            "thesis": strings[0],
            "evidence_ids": evidence_ids,
            "reasoning": strings[1],
            "action_hypothesis": strings[2],
            "validation_metric": strings[3],
            "confidence": str(item.get("confidence") or "medium"),
            "assumptions": [str(value) for value in item.get("assumptions") or []][:3],
        })
    if not validated:
        return None, "no valid insights"
    requests, request_error = _validated_evidence_requests(
        raw.get("evidence_requests"), available_records or records,
        locked_platform=locked_platform,
    )
    if request_error:
        return None, request_error
    return {
        "direct_answer": direct,
        "insights": validated,
        "data_gaps": [str(value) for value in raw.get("data_gaps") or []][:5],
        "evidence_requests": requests,
    }, None


def _supplement_pack(pack: dict, available_records: list[dict], requests: list[dict]) -> int:
    included = {str(row.get("evidence_id")) for row in pack.get("records") or []}
    additions = []
    for request in requests:
        wanted_subjects = set(request.get("subject_refs") or [])
        for row in available_records:
            if str(row.get("evidence_id")) in included:
                continue
            if str(row.get("dimension") or "") != request.get("dimension"):
                continue
            if wanted_subjects and str(row.get("subject") or "") not in wanted_subjects:
                continue
            additions.append(dict(row))
            included.add(str(row.get("evidence_id")))
            if len(additions) >= 30:
                break
    if not additions:
        return 0
    pack.setdefault("records", []).extend(additions)
    manifest = pack.setdefault("manifest", {})
    manifest["included_record_count"] = len(pack["records"])
    manifest["supplemented_record_count"] = int(manifest.get("supplemented_record_count") or 0) + len(additions)
    omitted_ids = {str(row.get("evidence_id")) for row in additions}
    pack["omitted_slices"] = [
        row for row in pack.get("omitted_slices") or []
        if str(row.get("evidence_id")) not in omitted_ids
    ]
    manifest["omitted_record_count"] = len(pack["omitted_slices"])
    return len(additions)


def _fallback_v3(records: list[dict], brand: str) -> dict:
    candidates = [row for row in records if row.get("dimension") != "overall"]
    growing = sorted(candidates, key=lambda row: _record_score(row, "growth"), reverse=True)
    declining = sorted(candidates, key=lambda row: _record_score(row, "decline"), reverse=True)
    picks = []
    if growing and (_num(growing[0].get("gmv_growth")) or 0) > 0:
        picks.append({
            "insight_type": "growth_opportunity", "thesis": f"{growing[0]['subject']}是当前较明确的增长方向",
            "evidence_ids": [growing[0]["evidence_id"]],
            "reasoning": "该对象同时具备可观察的增长信号和业务规模",
            "action_hypothesis": "建议测试是否可以复制当前有效组合并逐步扩大覆盖",
            "validation_metric": "持续观察GMV增长额、份额变化和后续留存表现",
            "confidence": "medium", "assumptions": [],
        })
    if declining and (_num(declining[0].get("gmv_growth")) or 0) < 0:
        picks.append({
            "insight_type": "recovery_opportunity", "thesis": f"{declining[0]['subject']}是优先修复对象",
            "evidence_ids": [declining[0]["evidence_id"]],
            "reasoning": "该对象的生意体量与下降表现使其具备较高修复价值",
            "action_hypothesis": "建议拆查商品和渠道结构，并用小范围调整验证修复空间",
            "validation_metric": "观察GMV降幅、份额和关键商品贡献是否改善",
            "confidence": "medium", "assumptions": [],
        })
    if len(picks) >= 2:
        direct_answer = f"{brand}当前既有可继续放大的增长方向，也有需要优先修复的下降项。"
    elif picks and picks[0]["insight_type"] == "growth_opportunity":
        direct_answer = f"{brand}当前有一个可以小范围验证并考虑放大的增长方向。"
    elif picks:
        direct_answer = f"{brand}当前更明确的机会来自主要下降项的修复。"
    else:
        direct_answer = f"{brand}当前证据不足以形成可靠的增长机会判断。"
    return {
        "direct_answer": direct_answer,
        "insights": picks,
        "data_gaps": [] if picks else ["当前证据不足以形成可靠的机会判断"],
        "evidence_requests": [],
    }


def _render_record_fact(record: dict) -> str:
    parts = [f"{record.get('subject')}（{record.get('dimension')}）"]
    if _num(record.get("gmv_actual")) is not None:
        parts.append(f"GMV {_fmt_gmv(record.get('gmv_actual'))}")
    if _num(record.get("gmv_growth")) is not None:
        parts.append(f"增长额{_fmt_gmv(record.get('gmv_growth'))}")
    if _num(record.get("evol")) is not None:
        parts.append(f"同比{_fmt_evol(record.get('evol'))}")
    if _num(record.get("share")) is not None:
        parts.append(f"份额{_fmt_pct(record.get('share'))}")
    if _num(record.get("share_delta")) is not None:
        parts.append(f"份额变化{_fmt_pp(record.get('share_delta'))}")
    return "，".join(parts)


def run_opportunity_reasoning_v3(
    user_question: str, evidence: dict, *, selected_skills: list[str] | None = None,
) -> dict:
    """Reason over real metrics while keeping every final fact evidence-addressable."""
    pack = build_reasoning_pack(evidence)
    records = list(pack.get("records") or [])
    brand = str(pack.get("scope", {}).get("brand") or evidence.get("brand") or "该品牌")
    period = str((pack.get("scope", {}).get("period") or {}).get("raw") or "")
    platform = str(pack.get("scope", {}).get("platform") or "")
    skills = list(selected_skills or ["analysis-drill", "opportunity-insight"])
    available_records = [
        dict(row) for row in evidence.get("records") or [] if isinstance(row, dict)
    ]
    validated = None
    validation_error = None
    used_llm = False
    evidence_rounds = 0
    if records and os.environ.get("DASHSCOPE_API_KEY"):
        client = llm_client(max_retries=0)
        validation_failures = 0
        for attempt in range(4):
            records = list(pack.get("records") or [])
            prompt = f"""你是品牌生意分析Agent的证据推理器。

完整用户原文：{user_question}
锁定范围：品牌={brand}，期间={period}，平台={platform}。这些实体不可修改。

本次选中的完整分析方法：
{_selected_skill_guidance(skills)}

Evidence Envelope（真实指标，可用于比较和推理；只能引用其中的evidence_id）：
{json.dumps(pack, ensure_ascii=False, default=str)}

请直接回答问题，并识别增长机会、修复机会或风险。行动只能写成待验证假设，不能声称因果。
文字字段禁止自行填写数字；系统会根据evidence_id附加真实数字。
如果Manifest显示缺少关键证据，可在evidence_requests中提出最多两个受控请求。
只返回JSON：
{{"direct_answer":"", "insights":[{{"insight_type":"growth_opportunity|recovery_opportunity|risk|action_hypothesis",
"thesis":"","evidence_ids":["..."],"reasoning":"","action_hypothesis":"建议测试...",
"validation_metric":"观察...","confidence":"high|medium|low","assumptions":[]}}],
"data_gaps":[],"evidence_requests":[]}}"""
            try:
                response = client.chat.completions.create(
                    model=llm_model("summary"),
                    messages=[{"role": "user", "content": prompt + (
                        f"\n上次输出未通过校验：{validation_error}。请修正。" if validation_error else ""
                    )}],
                    max_tokens=1800,
                    timeout=float(os.environ.get("LLM_REQUEST_TIMEOUT", "30")),
                    response_format={"type": "json_object"},
                    extra_body={"enable_thinking": False},
                )
                raw = extract_json_object(response.choices[0].message.content or "")
                validated, validation_error = _validate_v3_result(
                    raw, records, available_records=available_records,
                    locked_platform=platform,
                )
                if not validated:
                    validation_failures += 1
                    if validation_failures > 1:
                        break
                    continue
                requests = list(validated.get("evidence_requests") or [])
                if requests and evidence_rounds < 2:
                    added = _supplement_pack(pack, available_records, requests)
                    if added:
                        evidence_rounds += 1
                        validated = None
                        validation_error = None
                        continue
                    validated.setdefault("data_gaps", []).append(
                        "请求的补充证据在当前已注册数据能力中不可用"
                    )
                used_llm = True
                break
            except Exception as exc:
                validation_error = str(exc)
                log.warning("[opportunity_v3] reasoning attempt %s failed: %s", attempt + 1, exc)
    if not validated:
        validated = _fallback_v3(records, brand)
    record_by_id = {str(row.get("evidence_id")): row for row in records}
    sections = [f"先回答你的问题：{validated['direct_answer']}", "## 增长机会、风险与行动假设"]
    used_evidence = []
    for index, insight in enumerate(validated.get("insights") or [], 1):
        facts = [record_by_id[value] for value in insight["evidence_ids"] if value in record_by_id]
        used_evidence.extend(insight["evidence_ids"])
        sections.extend([
            f"### {index}. {insight['thesis']}",
            "证据：" + "；".join(_render_record_fact(row) for row in facts) + "。",
            f"判断：{insight['reasoning']}。",
            f"行动假设：{insight['action_hypothesis']}。",
            f"验证方式：{insight['validation_metric']}。",
        ])
    if validated.get("data_gaps"):
        sections.extend(["## 数据缺口", *[f"- {value}" for value in validated["data_gaps"]]])
    sections.extend([
        "## 分析边界",
        "以上行动为基于内部生意证据和已审核分析方法的待验证假设，不代表因果结论或公司既定动作。",
    ])
    return {
        "markdown": "\n\n".join(sections),
        "reasoning_pack": pack,
        "analysis": validated,
        "used_llm": used_llm,
        "validation_error": validation_error,
        "used_evidence_ids": list(dict.fromkeys(used_evidence)),
        "evidence_requests": validated.get("evidence_requests") or [],
        "evidence_rounds": evidence_rounds,
    }
