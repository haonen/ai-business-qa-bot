from __future__ import annotations

import json
import os
import re
import calendar
import math
from datetime import date, datetime
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

    iso_month_range = re.fullmatch(
        r"(20\d{2})-(\d{1,2})[~～—–\-至到]+(20\d{2})-(\d{1,2})",
        period,
    )
    if iso_month_range:
        return _month_period_ranges(
            int(iso_month_range.group(1)),
            int(iso_month_range.group(2)),
            int(iso_month_range.group(3)),
            int(iso_month_range.group(4)),
        )

    cn_month_range = re.fullmatch(
        r"(?:(20\d{2})年)?(\d{1,2})月?[~～—–\-至到]+(?:(20\d{2})年)?(\d{1,2})月",
        period,
    )
    if cn_month_range:
        start_year = int(cn_month_range.group(1) or cn_month_range.group(3) or 2026)
        end_year = int(cn_month_range.group(3) or start_year)
        return _month_period_ranges(
            start_year,
            int(cn_month_range.group(2)),
            end_year,
            int(cn_month_range.group(4)),
        )

    single_cn_month = re.fullmatch(r"(?:(20\d{2})年)?(\d{1,2})月", period)
    if single_cn_month:
        year = int(single_cn_month.group(1) or 2026)
        month = int(single_cn_month.group(2))
        return _month_period_ranges(year, month, year, month)

    single_iso_month = re.fullmatch(r"(20\d{2})-(\d{1,2})", period)
    if single_iso_month:
        year = int(single_iso_month.group(1))
        month = int(single_iso_month.group(2))
        return _month_period_ranges(year, month, year, month)

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
    raise ValueError(
        f"无法解析时间段：{period}，支持 5月 / 2026年5月 / 1-4月 / "
        "618 / 双11 / 520 / 2026-05-01~2026-05-31"
    )


def parse_ec_period(period: str, default_year: int) -> dict[str, Any]:
    """Parse an EC analysis period without assuming a fixed analysis year."""
    raw = str(period or "").strip()
    if not raw:
        raise ValueError("缺少生意分析时间，请明确指定月份或日期区间。")

    campaign = raw.replace("双十一", "双11")
    campaign_year_match = re.fullmatch(r"(20\d{2})年?(618|520|双11)", campaign)
    campaign_year = int(campaign_year_match.group(1)) if campaign_year_match else default_year
    campaign_name = campaign_year_match.group(2) if campaign_year_match else campaign
    campaign_windows = {
        "618": ((5, 13), (6, 7)),
        "520": ((5, 10), (5, 20)),
        "双11": ((10, 24), (11, 11)),
    }
    if campaign_name in campaign_windows:
        (sm, sd), (em, ed) = campaign_windows[campaign_name]
        current_start = date(campaign_year, sm, sd)
        current_end = date(campaign_year, em, ed)
    else:
        current_start, current_end = _parse_ec_date_range(raw, default_year)

    if current_end < current_start:
        raise ValueError("结束日期不能早于开始日期。")
    prior_start = _shift_date_year(current_start, -1)
    prior_end = _shift_date_year(current_end, -1)
    return {
        "current_start": current_start.isoformat(),
        "current_end": current_end.isoformat(),
        "prior_start": prior_start.isoformat(),
        "prior_end": prior_end.isoformat(),
        "current_year": current_start.year,
        "prior_year": prior_start.year,
        "current_label": _period_label(current_start, current_end),
        "prior_label": _period_label(prior_start, prior_end),
        "raw": raw,
    }


def _parse_ec_date_range(value: str, default_year: int) -> tuple[date, date]:
    iso_dates = re.fullmatch(
        r"(20\d{2})-(\d{1,2})-(\d{1,2})\s*[~～—–至到]+\s*"
        r"(20\d{2})-(\d{1,2})-(\d{1,2})",
        value,
    )
    if iso_dates:
        return (
            date(int(iso_dates.group(1)), int(iso_dates.group(2)), int(iso_dates.group(3))),
            date(int(iso_dates.group(4)), int(iso_dates.group(5)), int(iso_dates.group(6))),
        )

    cn_dates = re.fullmatch(
        r"(?:(20\d{2})年)?(\d{1,2})月(\d{1,2})[日号]?\s*[~～—–\-至到]+\s*"
        r"(?:(20\d{2})年)?(\d{1,2})月(\d{1,2})[日号]?",
        value,
    )
    if cn_dates:
        start_year = int(cn_dates.group(1) or cn_dates.group(4) or default_year)
        end_year = int(cn_dates.group(4) or start_year)
        return (
            date(start_year, int(cn_dates.group(2)), int(cn_dates.group(3))),
            date(end_year, int(cn_dates.group(5)), int(cn_dates.group(6))),
        )

    iso_months = re.fullmatch(
        r"(20\d{2})-(\d{1,2})\s*[~～—–至到]+\s*(20\d{2})-(\d{1,2})",
        value,
    )
    if iso_months:
        sy, sm, ey, em = map(int, iso_months.groups())
        return date(sy, sm, 1), date(ey, em, calendar.monthrange(ey, em)[1])

    cn_months = re.fullmatch(
        r"(?:(20\d{2})年)?(\d{1,2})月?\s*[~～—–\-至到]+\s*"
        r"(?:(20\d{2})年)?(\d{1,2})月",
        value,
    )
    if cn_months:
        sy = int(cn_months.group(1) or cn_months.group(3) or default_year)
        ey = int(cn_months.group(3) or sy)
        sm, em = int(cn_months.group(2)), int(cn_months.group(4))
        return date(sy, sm, 1), date(ey, em, calendar.monthrange(ey, em)[1])

    single_cn = re.fullmatch(r"(?:(20\d{2})年)?(\d{1,2})月", value)
    if single_cn:
        year = int(single_cn.group(1) or default_year)
        month = int(single_cn.group(2))
        return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])

    single_iso = re.fullmatch(r"(20\d{2})-(\d{1,2})", value)
    if single_iso:
        year, month = map(int, single_iso.groups())
        return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])

    raise ValueError(
        f"无法解析时间段：{value}。支持单月、月份区间、精确日期区间、618、520或双11。"
    )


def _shift_date_year(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        return value.replace(year=value.year + years, day=28)


def _period_label(start: date, end: date) -> str:
    if start.year == end.year:
        return f"{start.year}年{start.month}月{start.day}日—{end.month}月{end.day}日"
    return (
        f"{start.year}年{start.month}月{start.day}日—"
        f"{end.year}年{end.month}月{end.day}日"
    )


def _month_period_ranges(
    start_year: int,
    start_month: int,
    end_year: int,
    end_month: int,
) -> dict[str, tuple[str, str]]:
    if start_year != 2026 or end_year != 2026:
        raise ValueError("当前分析版本仅支持2026年，并使用2025年同期计算同比。")
    if not 1 <= start_month <= 12 or not 1 <= end_month <= 12:
        raise ValueError("月份必须在1到12之间。")
    if (end_year, end_month) < (start_year, start_month):
        raise ValueError("结束月份不能早于开始月份。")
    start_26 = f"{start_year}-{start_month:02d}-01"
    end_26 = f"{end_year}-{end_month:02d}-{calendar.monthrange(end_year, end_month)[1]:02d}"
    start_25 = f"{start_year - 1}-{start_month:02d}-01"
    end_25_year = end_year - 1
    end_25 = f"{end_25_year}-{end_month:02d}-{calendar.monthrange(end_25_year, end_month)[1]:02d}"
    return {"y2025": (start_25, end_25), "y2026": (start_26, end_26)}


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
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return fallback
    text = str(value).strip()
    return text or fallback


def llm_client(*, max_retries: int = 2):
    from openai import OpenAI

    return OpenAI(
        api_key=os.environ["DASHSCOPE_API_KEY"],
        base_url=os.environ.get("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        max_retries=max_retries,
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

    month_hint = normalize_period_hint(cleaned)
    if month_hint and ("月" in month_hint or re.fullmatch(r"20\d{2}-\d{1,2}", month_hint)):
        without_period = cleaned.replace(month_hint, " ")
        candidate = re.split(
            r"(?:的)?(?:生意|媒体投资|媒体表现|BET|bet|表现|怎么样|如何|分析|情况)",
            without_period,
            maxsplit=1,
        )[0]
        candidate = re.sub(r"[，,。！？!?\s]+", "", candidate)
        if 1 <= len(candidate) <= 30:
            return candidate

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
    iso_month_range = re.search(
        r"20\d{2}-\d{1,2}[~～—–\-至到]+20\d{2}-\d{1,2}",
        text,
    )
    if iso_month_range:
        return iso_month_range.group(0)
    cn_month_range = re.search(
        r"(?:(?:20\d{2})年)?\d{1,2}月?[~～—–\-至到]+(?:(?:20\d{2})年)?\d{1,2}月",
        text,
    )
    if cn_month_range:
        return cn_month_range.group(0)
    single_cn_month = re.search(r"(?:(?:20\d{2})年)?\d{1,2}月", text)
    if single_cn_month:
        return single_cn_month.group(0)
    single_iso_month = re.search(r"20\d{2}-\d{1,2}(?!-\d)", text)
    if single_iso_month:
        return single_iso_month.group(0)
    campaign_with_year = re.search(r"20\d{2}年?(?:618|520|双11|双十一)", text)
    if campaign_with_year:
        return campaign_with_year.group(0).replace("双十一", "双11")
    if "双十一" in text:
        return "双11"
    for p in ["618", "双11", "520"]:
        if p in text:
            return p
    return None
