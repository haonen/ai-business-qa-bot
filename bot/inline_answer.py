from __future__ import annotations

"""Evidence-grounded one-sentence answer shown before the report link."""

import logging
import os
import re

from bot.utils import llm_client


log = logging.getLogger(__name__)


def inline_answer_enabled() -> bool:
    return os.environ.get("BOT_INLINE_ANSWER_ENABLED", "1") == "1"


def _one_sentence(value: str, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(r"^(?:一句话回答|直接回答|结论)\s*[:：]\s*", "", text)
    parts = [part.strip(" ；。！？") for part in re.split(r"[。！？]+", text) if part.strip()]
    text = "；".join(parts)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip(" ，；。") + "……"
    return f"{text}。" if text else ""


def _numbers(value: str) -> list[str]:
    return re.findall(r"[+-]?\d+(?:,\d{3})*(?:\.\d+)?(?:%|pp|M|K|万|亿)?", value, re.I)


def _normalize_number(value: str) -> str:
    # Direction is often expressed as a word ("下降40%") instead of the
    # report's sign ("-40%"); the magnitude must still exist verbatim.
    return value.replace(",", "").lstrip("+-").casefold()


def _is_grounded(answer: str, user_text: str, report_markdown: str) -> bool:
    evidence_numbers = {
        _normalize_number(number) for number in _numbers(f"{user_text}\n{report_markdown}")
    }
    return all(_normalize_number(number) in evidence_numbers for number in _numbers(answer))


def _report_excerpt(markdown: str) -> str:
    limit = max(4000, int(os.environ.get("BOT_INLINE_ANSWER_REPORT_CHARS", "24000")))
    if len(markdown) <= limit:
        return markdown
    half = limit // 2
    return f"{markdown[:half]}\n\n[中间表格已省略]\n\n{markdown[-half:]}"


def _fallback_answer(user_text: str, markdown: str, max_chars: int) -> str | None:
    question_tokens = set(re.findall(r"[A-Za-z][A-Za-z0-9_-]+|[\u4e00-\u9fff]{2,8}", user_text.casefold()))
    topic_terms = {
        term for term in (
            "gmv", "同比", "环比", "增长", "下降", "贡献", "拖累", "品类", "类目",
            "李佳琦", "t2", "non-kol", "kol直播", "品牌自营直播", "短视频",
            "媒体花费", "媒体投资", "费比", "bet", "主推", "系列", "sku",
        ) if term in user_text.casefold()
    }
    candidates: list[tuple[int, str]] = []
    for raw in markdown.splitlines():
        line = re.sub(r"^\s*(?:[-*+]|•|\d+[.)])\s*", "", raw).strip()
        line = re.sub(r"[*_#]", "", line).strip()
        if not 10 <= len(line) <= 320:
            continue
        if line.startswith(("数据源", "数据口径", "http", "注：", "说明：")):
            continue
        if line.count("|") >= 2 or re.fullmatch(r"[-:| ]+", line):
            continue
        lowered = line.casefold()
        overlap = sum(1 for token in question_tokens if token in lowered)
        topic = sum(3 for token in topic_terms if token in lowered)
        evidence = 3 if _numbers(line) else 0
        conclusion = 2 if any(token in line for token in ("整体", "增长", "下降", "最大", "最高", "主要", "贡献", "拖累")) else 0
        candidates.append((overlap + topic + evidence + conclusion, line))
    if not candidates:
        return None
    best = max(candidates, key=lambda item: (item[0], _numbers(item[1]) != [], -len(item[1])))[1]
    return _one_sentence(best, max_chars)


def build_inline_answer(user_text: str, report_markdown: str) -> str | None:
    """Answer from report evidence only; safely fall back when the LLM fails."""
    if not inline_answer_enabled() or not report_markdown.strip():
        return None
    max_chars = max(60, int(os.environ.get("BOT_INLINE_ANSWER_MAX_CHARS", "180")))
    fallback = _fallback_answer(user_text, report_markdown, max_chars)
    if not os.environ.get("DASHSCOPE_API_KEY"):
        return fallback
    prompt = f"""
用户原问题：{user_text}

已完成的分析报告：
{_report_excerpt(report_markdown)}

任务：先直接回答用户最想知道的结论。只输出一个中文句子，不要标题、列表或铺垫，不超过{max_chars}字。
只能使用报告中明确存在的事实和数字；不得补造数字、因果、策略或公司动作。原问题有多个要点时，用分号在同一句里概括。报告无法支持的要点要明确说“现有数据无法判断”。
"""
    try:
        response = llm_client(max_retries=0).chat.completions.create(
            model=os.environ.get("DASHSCOPE_SUMMARY_MODEL", "qwen-plus-latest"),
            messages=[
                {"role": "system", "content": "你是严格的业务报告结论编辑，绝不超出证据。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0,
            max_tokens=240,
            timeout=float(os.environ.get("BOT_INLINE_ANSWER_TIMEOUT_SECONDS", "12")),
            extra_body={"enable_thinking": False},
        )
        answer = _one_sentence(response.choices[0].message.content or "", max_chars)
        if answer and _is_grounded(answer, user_text, report_markdown):
            return answer
        log.warning("inline answer rejected because its numbers were not grounded")
    except Exception as exc:
        log.warning("inline answer generation failed: %s", exc)
    return fallback


def document_status(answer: str | None, trailing: str) -> str:
    if not answer:
        return trailing
    return f"先回答你的问题：{answer}\n\n{trailing}"
