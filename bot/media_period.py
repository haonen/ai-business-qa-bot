from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
import calendar
import re

from bot.utils import normalize_period_hint, parse_period


_RANGE_SEP = r"[~～—–\-至到]+"


@dataclass(frozen=True)
class MediaPeriod:
    focus_start: str
    focus_end: str
    prior_start: str
    prior_end: str
    search_start: str
    search_end: str
    display: str
    prior_display: str
    canonical: str

    def to_dict(self) -> dict:
        return asdict(self)


def _month_start(year: int, month: int) -> date:
    return date(year, month, 1)


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _shift_year(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        return value.replace(year=value.year + years, day=28)


def _build(start: date, end: date) -> MediaPeriod:
    start_month = _month_start(start.year, start.month)
    end_month = _month_end(end.year, end.month)
    if start_month.year != 2026 or end_month.year != 2026:
        raise ValueError("BET媒体分析V1仅支持2026年，并使用2025年同期计算Evol%。")
    if end_month < start_month:
        raise ValueError("分析结束月份不能早于开始月份。")

    prior_start = _shift_year(start_month, -1)
    prior_end = _shift_year(end_month, -1)
    search_start = date(start_month.year, 1, 1)
    if start_month.month == end_month.month:
        display = f"{start_month.year}年{start_month.month}月"
        prior_display = f"{prior_start.year}年{prior_start.month}月"
        canonical = display
    else:
        display = f"{start_month.year}年{start_month.month}–{end_month.month}月"
        prior_display = f"{prior_start.year}年{prior_start.month}–{prior_end.month}月"
        canonical = f"{start_month.year}年{start_month.month}-{end_month.month}月"
    return MediaPeriod(
        focus_start=start_month.isoformat(),
        focus_end=end_month.isoformat(),
        prior_start=prior_start.isoformat(),
        prior_end=prior_end.isoformat(),
        search_start=search_start.isoformat(),
        search_end=end_month.isoformat(),
        display=display,
        prior_display=prior_display,
        canonical=canonical,
    )


def normalize_media_period_hint(text: str) -> str | None:
    text = (text or "").strip()
    full_dates = re.search(
        rf"20\d{{2}}-\d{{2}}-\d{{2}}{_RANGE_SEP}20\d{{2}}-\d{{2}}-\d{{2}}",
        text,
    )
    if full_dates:
        return full_dates.group(0)
    full_cn_dates = re.search(
        rf"(?:(?:20\d{{2}})年)?\d{{1,2}}月\d{{1,2}}[日号]?{_RANGE_SEP}"
        rf"(?:(?:20\d{{2}})年)?\d{{1,2}}月\d{{1,2}}[日号]?",
        text,
    )
    if full_cn_dates:
        return full_cn_dates.group(0)
    single_iso_date = re.search(r"20\d{2}-\d{1,2}-\d{1,2}(?!\d)", text)
    if single_iso_date:
        return single_iso_date.group(0)
    single_cn_date = re.search(r"(?:(?:20\d{2})年)?\d{1,2}月\d{1,2}[日号]?", text)
    if single_cn_date:
        return single_cn_date.group(0)
    patterns = [
        rf"20\d{{2}}-\d{{1,2}}{_RANGE_SEP}20\d{{2}}-\d{{1,2}}",
        rf"20\d{{2}}年\d{{1,2}}月?{_RANGE_SEP}(?:20\d{{2}}年)?\d{{1,2}}月",
        rf"\d{{1,2}}月?{_RANGE_SEP}\d{{1,2}}月",
        r"20\d{2}年\d{1,2}月",
        r"20\d{2}-\d{1,2}",
        r"\d{1,2}月",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(0)
    for marker in ("双十一", "双11", "618", "520"):
        if marker in text:
            return "双11" if marker == "双十一" else marker
    return normalize_period_hint(text)


def parse_media_period(period: str) -> MediaPeriod:
    raw = (period or "").strip()
    if not raw:
        raise ValueError("缺少媒体分析时间。")

    month_range = re.fullmatch(
        rf"(20\d{{2}})年(\d{{1,2}})月?{_RANGE_SEP}(?:(20\d{{2}})年)?(\d{{1,2}})月",
        raw,
    )
    if month_range:
        start_year = int(month_range.group(1))
        end_year = int(month_range.group(3) or start_year)
        return _build(
            _month_start(start_year, int(month_range.group(2))),
            _month_end(end_year, int(month_range.group(4))),
        )

    short_month_range = re.fullmatch(
        rf"(\d{{1,2}})月?{_RANGE_SEP}(\d{{1,2}})月",
        raw,
    )
    if short_month_range:
        return _build(
            _month_start(2026, int(short_month_range.group(1))),
            _month_end(2026, int(short_month_range.group(2))),
        )

    iso_month_range = re.fullmatch(
        rf"(20\d{{2}})-(\d{{1,2}}){_RANGE_SEP}(20\d{{2}})-(\d{{1,2}})",
        raw,
    )
    if iso_month_range:
        return _build(
            _month_start(int(iso_month_range.group(1)), int(iso_month_range.group(2))),
            _month_end(int(iso_month_range.group(3)), int(iso_month_range.group(4))),
        )

    single_cn = re.fullmatch(r"(20\d{2})年(\d{1,2})月", raw)
    if single_cn:
        year, month = int(single_cn.group(1)), int(single_cn.group(2))
        return _build(_month_start(year, month), _month_end(year, month))

    short_single_cn = re.fullmatch(r"(\d{1,2})月", raw)
    if short_single_cn:
        month = int(short_single_cn.group(1))
        return _build(_month_start(2026, month), _month_end(2026, month))

    single_iso = re.fullmatch(r"(20\d{2})-(\d{1,2})", raw)
    if single_iso:
        year, month = int(single_iso.group(1)), int(single_iso.group(2))
        return _build(_month_start(year, month), _month_end(year, month))

    try:
        ranges = parse_period(raw)
        start = datetime.strptime(ranges["y2026"][0], "%Y-%m-%d").date()
        end = datetime.strptime(ranges["y2026"][1], "%Y-%m-%d").date()
        return _build(start, end)
    except ValueError:
        pass

    raise ValueError(
        f"无法解析媒体分析时间：{period}。支持“2026年3月”“2026年1-4月”或618等口径。"
    )


def period_from_latest_month(value: str | date | datetime) -> MediaPeriod:
    if isinstance(value, str):
        parsed = datetime.strptime(value[:10], "%Y-%m-%d").date()
    elif isinstance(value, datetime):
        parsed = value.date()
    else:
        parsed = value
    return _build(_month_start(parsed.year, parsed.month), _month_end(parsed.year, parsed.month))
