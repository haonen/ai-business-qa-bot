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

from bot.utils import extract_json_object, llm_client


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
                    model=os.environ.get("DASHSCOPE_SUMMARY_MODEL", "qwen-plus-latest"),
                    messages=[{"role": "user", "content": base_prompt + extra}],
                    temperature=0.3, max_tokens=600,
                    timeout=float(os.environ.get("LLM_REQUEST_TIMEOUT", "30")),
                    response_format={"type": "json_object"},
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
