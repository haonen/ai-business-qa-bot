from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(__file__).resolve().parent / "data"


PERIOD_MAP = {
    "618": {
        2025: ("2025-05-13", "2025-06-07"),
        2026: ("2026-05-13", "2026-06-07"),
    },
    "双11": {
        2025: ("2025-10-24", "2025-11-11"),
        2026: ("2026-10-24", "2026-11-11"),
    },
    "双十一": {
        2025: ("2025-10-24", "2025-11-11"),
        2026: ("2026-10-24", "2026-11-11"),
    },
    "520": {
        2025: ("2025-05-10", "2025-05-20"),
        2026: ("2026-05-10", "2026-05-20"),
    },
}


def load_json(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def parse_period(period: str) -> dict[str, tuple[str, str]]:
    period = (period or "618").strip()
    if period in PERIOD_MAP:
        p = PERIOD_MAP[period]
        return {"y2025": p[2025], "y2026": p[2026]}

    match = re.match(r"(\d{4}-\d{2}-\d{2})[~\-至到]+(\d{4}-\d{2}-\d{2})", period)
    if match:
        s26, e26 = match.group(1), match.group(2)
        s25 = _shift_year(s26, -1)
        e25 = _shift_year(e26, -1)
        return {"y2025": (s25, e25), "y2026": (s26, e26)}
    md_match = re.match(r"(?:(20\d{2})年)?(\d{1,2})月(\d{1,2})[日号]?[~\-至到]+(?:(20\d{2})年)?(\d{1,2})月(\d{1,2})[日号]?", period)
    if md_match:
        start_year = int(md_match.group(1) or md_match.group(4) or 2026)
        end_year = int(md_match.group(4) or start_year)
        s26 = f"{start_year}-{int(md_match.group(2)):02d}-{int(md_match.group(3)):02d}"
        e26 = f"{end_year}-{int(md_match.group(5)):02d}-{int(md_match.group(6)):02d}"
        s25 = _shift_year(s26, -1)
        e25 = _shift_year(e26, -1)
        return {"y2025": (s25, e25), "y2026": (s26, e26)}
    raise ValueError(f"无法解析时间段：{period}，支持 618 / 双11 / 520 / 2026-05-01~2026-05-31")


def _shift_year(value: str, years: int) -> str:
    dt = datetime.strptime(value, "%Y-%m-%d")
    try:
        return dt.replace(year=dt.year + years).strftime("%Y-%m-%d")
    except ValueError:
        # Handles Feb 29 when shifting to a non-leap year.
        return dt.replace(year=dt.year + years, day=28).strftime("%Y-%m-%d")


def safe_evol(new_val: float | int | None, old_val: float | int | None) -> float | None:
    if old_val is None or old_val == 0:
        return None
    return round(((new_val or 0) - old_val) / old_val, 4)


def safe_div(num: float | int | None, den: float | int | None) -> float | None:
    if not den:
        return None
    return round((num or 0) / den, 4)


def clean_label(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text or fallback


def llm_client():
    from openai import OpenAI

    return OpenAI(
        api_key=os.environ["DASHSCOPE_API_KEY"],
        base_url=os.environ.get("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    )


def extract_json_object(text: str) -> dict:
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group())
    except Exception:
        return {}


def detect_brand_hint(text: str) -> str | None:
    text = text.strip()
    cleaned = re.sub(r"^\s*(帮我|请|麻烦)?(分析一下|分析|看一下|看看|看|帮忙看一下)?\s*", "", text)

    date_pattern = r"(?:(?:20\d{2})年)?\d{1,2}月\d{1,2}[日号]?[~\-至到]+(?:(?:20\d{2})年)?\d{1,2}月\d{1,2}[日号]?"
    date_match = re.search(date_pattern, cleaned)
    if date_match:
        before = cleaned[:date_match.start()].strip()
        before = re.sub(r"[，,。！？!?\s]+$", "", before)
        if 1 <= len(before) <= 30:
            return before
        after = cleaned[date_match.end():]
        after = re.sub(r"^(期间|的|里|内|，|,|\s)+", "", after).strip()
        candidate = re.split(r"(?:的)?(?:生意|表现|怎么样|如何|分析|情况)", after, maxsplit=1)[0].strip()
        candidate = re.sub(r"[，,。！？!?\s]+$", "", candidate)
        if 1 <= len(candidate) <= 30:
            return candidate

    full_date_match = re.search(r"\d{4}-\d{2}-\d{2}[~\-至到]+\d{4}-\d{2}-\d{2}", cleaned)
    if full_date_match:
        before = cleaned[:full_date_match.start()].strip()
        before = re.sub(r"[，,。！？!?\s]+$", "", before)
        if 1 <= len(before) <= 30:
            return before
        after = cleaned[full_date_match.end():]
        after = re.sub(r"^(期间|的|里|内|，|,|\s)+", "", after).strip()
        candidate = re.split(r"(?:的)?(?:生意|表现|怎么样|如何|分析|情况)", after, maxsplit=1)[0].strip()
        if 1 <= len(candidate) <= 30:
            return candidate

    for marker in ["618", "双11", "双十一", "520"]:
        if marker in cleaned:
            before, after = cleaned.split(marker, 1)
            # Support both "Olay 618怎么样" and "618 Olay怎么样".
            candidates = [before, re.split(r"(?:的)?(?:生意|表现|怎么样|如何|分析|情况)", after, maxsplit=1)[0]]
            for candidate in candidates:
                candidate = re.sub(r"^(期间|的|里|内|，|,|\s)+", "", candidate).strip()
                candidate = re.sub(r"[，,。！？!?\s]+$", "", candidate)
                if 1 <= len(candidate) <= 30:
                    return candidate

    match = re.search(r"([A-Za-z][A-Za-z0-9&.\- ]{0,30}|[\u4e00-\u9fa5][\u4e00-\u9fa5A-Za-z0-9]{0,20})(?:的)?(?:生意|表现|怎么样|如何)", cleaned)
    if match:
        return match.group(1).strip()
    return None


def normalize_period_hint(text: str) -> str | None:
    text = text.strip()
    full = re.search(r"\d{4}-\d{2}-\d{2}[~\-至到]+\d{4}-\d{2}-\d{2}", text)
    if full:
        return full.group(0).replace("到", "~").replace("至", "~")
    md = re.search(r"(?:(?:20\d{2})年)?\d{1,2}月\d{1,2}[日号]?[~\-至到]+(?:(?:20\d{2})年)?\d{1,2}月\d{1,2}[日号]?", text)
    if md:
        return md.group(0)
    if "双十一" in text:
        return "双11"
    for p in ["618", "双11", "520"]:
        if p in text:
            return p
    return None
