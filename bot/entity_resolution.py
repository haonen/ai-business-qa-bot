from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from difflib import SequenceMatcher
from functools import lru_cache
import calendar
import json
from pathlib import Path
import re
import unicodedata


DATA_DIR = Path(__file__).resolve().parent / "data"
REFERENCE_PATH = DATA_DIR / "brand_cn_en_reference.json"
MEDIA_ALIAS_PATH = DATA_DIR / "media_brand_aliases.json"


ENGLISH_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2,
    "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

# These are business-confirmed input spellings, not aliases invented by the
# router model.  They resolve to an existing canonical registry record.
CONFIRMED_BRAND_ALIASES = {
    "林清玄": "林清轩",
}


class ResolvedPeriod(str):
    """String-compatible period carrying the already validated date range."""

    def __new__(
        cls,
        canonical: str,
        *,
        start_date: str,
        end_date: str,
        comparison_start: str | None = None,
        comparison_end: str | None = None,
    ):
        value = str.__new__(cls, canonical)
        value.start_date = start_date
        value.end_date = end_date
        value.comparison_start = comparison_start
        value.comparison_end = comparison_end
        return value

    def __deepcopy__(self, memo):
        return self


@dataclass
class TimeMention:
    raw_text: str
    start_offset: int
    end_offset: int
    granularity: str
    start_date: str | None
    end_date: str | None
    role: str = "FOCUS"
    year_source: str = "explicit"
    resolution_status: str = "exact"
    canonical: str | None = None


@dataclass
class TimeScope:
    mentions: list[TimeMention] = field(default_factory=list)
    focus_period: ResolvedPeriod | None = None
    comparison_periods: list[dict] = field(default_factory=list)
    campaign_name: str | None = None
    missing_slots: list[str] = field(default_factory=list)
    status: str = "missing"

    def to_dict(self) -> dict:
        data = {
            "mentions": [asdict(item) for item in self.mentions],
            "focus_period": str(self.focus_period) if self.focus_period else None,
            "comparison_periods": list(self.comparison_periods),
            "campaign_name": self.campaign_name,
            "missing_slots": list(self.missing_slots),
            "status": self.status,
        }
        if self.focus_period:
            data["focus_range"] = {
                "start_date": self.focus_period.start_date,
                "end_date": self.focus_period.end_date,
                "comparison_start": self.focus_period.comparison_start,
                "comparison_end": self.focus_period.comparison_end,
            }
        return data


@dataclass
class BrandCandidate:
    canonical_brand_key: str
    canonical_display_name: str
    aliases: list[str]
    score: float = 1.0


@dataclass
class BrandResolution:
    surface: str | None = None
    start_offset: int | None = None
    end_offset: int | None = None
    normalized_surface: str | None = None
    canonical_brand_key: str | None = None
    canonical_display_name: str | None = None
    aliases: list[str] = field(default_factory=list)
    source_mappings: dict[str, str] = field(default_factory=dict)
    status: str = "missing"
    candidates: list[BrandCandidate] = field(default_factory=list)
    match_method: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EntityResolution:
    text: str
    time_scope: TimeScope
    brand: BrandResolution
    reason_codes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "time_scope": self.time_scope.to_dict(),
            "brand": self.brand.to_dict(),
            "reason_codes": list(self.reason_codes),
        }


def _norm(value: object) -> str:
    return re.sub(
        r"[\s\-_'’‘`\.·&]+", "",
        unicodedata.normalize("NFKC", str(value or "")).casefold(),
    )


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _prior_year(value: date) -> date:
    try:
        return value.replace(year=value.year - 1)
    except ValueError:
        return value.replace(year=value.year - 1, day=28)


def _valid_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _mention(
    match: re.Match,
    granularity: str,
    start: date | None,
    end: date | None,
    *,
    year_source: str,
    status: str,
    canonical: str | None,
) -> TimeMention:
    return TimeMention(
        raw_text=match.group(0), start_offset=match.start(), end_offset=match.end(),
        granularity=granularity,
        start_date=start.isoformat() if start else None,
        end_date=end.isoformat() if end else None,
        year_source=year_source, resolution_status=status, canonical=canonical,
    )


def _extract_time_candidates(
    text: str,
    current_year: int,
    confirmed_year: int | None,
    as_of_date: date | None = None,
) -> list[TimeMention]:
    candidates: list[tuple[int, TimeMention]] = []

    def add(pattern: str, priority: int, builder):
        for match in re.finditer(pattern, text, re.I):
            try:
                mention = builder(match)
            except (TypeError, ValueError):
                mention = None
            if mention:
                candidates.append((priority, mention))

    add(
        r"(?:(今年|去年)|(20\d{2})年?)?\s*(YTD|MTD|年初至今|本月至今|今年以来)",
        95,
        lambda m: _build_to_date(m, current_year, confirmed_year, as_of_date),
    )
    add(
        r"今天|今日|昨天|昨日|前天",
        96,
        lambda m: _build_relative_day(m, as_of_date),
    )

    # Full/same-month date ranges.
    add(
        r"(?:(今年|去年)|(20\d{2})年)?(\d{1,2})月(\d{1,2})[日号]?\s*[~～—–\-至到]+\s*"
        r"(?:(20\d{2})年)?(?:(\d{1,2})月)?(\d{1,2})[日号]?",
        100,
        lambda m: _build_date_range(m, current_year, confirmed_year),
    )
    add(
        r"(20\d{2})-(\d{1,2})-(\d{1,2})\s*(?:~|～|—|–|至|到|\s-\s)\s*"
        r"(20\d{2})-(\d{1,2})-(\d{1,2})",
        100,
        _build_iso_date_range,
    )
    add(
        r"(?<!\d)(20\d{2})(\d{2})(\d{2})\s*[~～—–\-至到]+\s*(20\d{2})(\d{2})(\d{2})(?!\d)",
        100,
        _build_iso_date_range,
    )
    add(
        r"(20\d{2})/(\d{1,2})/(\d{1,2})\s*[~～—–\-至到]+\s*"
        r"(20\d{2})/(\d{1,2})/(\d{1,2})",
        100,
        _build_iso_date_range,
    )
    english_month = "|".join(sorted(ENGLISH_MONTHS, key=len, reverse=True))
    add(
        rf"\b({english_month})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s*(20\d{{2}}))?"
        rf"\s*(?:to|through|until|[-–—])\s*"
        rf"({english_month})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s*(20\d{{2}}))?\b",
        100,
        lambda m: _build_english_date_range(m, current_year),
    )
    # Month ranges before single months.
    add(
        r"(?:(今年|去年)|((?:20\d{2}|\d{2}))年)?(\d{1,2})月?\s*[~～—–\-至到]+\s*"
        r"(?:(20\d{2})年)?(\d{1,2})月",
        90,
        lambda m: _build_month_range(m, current_year, confirmed_year),
    )
    add(
        r"(20\d{2})-(\d{1,2})\s*[~～—–\-至到]+\s*(20\d{2})-(\d{1,2})(?!-\d)",
        90,
        _build_iso_month_range,
    )
    add(
        r"(?<!\d)(20\d{2})\s+(\d{1,2})\s*[~～—–\-至到]+\s*(\d{1,2})(?:\s*月)?(?!\d)",
        90,
        _build_spaced_month_range,
    )
    add(
        r"(?:(今年|去年)|(20\d{2})年?)?[Qq]([1-4])",
        80,
        lambda m: _build_quarter(m, current_year, confirmed_year),
    )
    add(
        r"(?:(今年|去年)|(20\d{2})年)?(\d{1,2})月(?!\d)",
        70,
        lambda m: _build_month(m, current_year, confirmed_year),
    )
    add(r"(20\d{2})-(\d{1,2})-(\d{1,2})(?!\d)", 75, _build_iso_single_date)
    add(r"(20\d{2})-(\d{1,2})(?!-\d)", 70, _build_iso_single_month)
    add(
        r"(?:(今年|去年)|(20\d{2})年)?(\d{1,2})月(\d{1,2})[日号]?",
        75,
        lambda m: _build_single_date(m, current_year, confirmed_year),
    )
    add(r"本月|上月", 70, lambda m: _build_relative_month(m, current_year))
    add(
        r"(?:(20\d{2})年?)?(618|520|双11|双十一)",
        60,
        lambda m: _build_campaign(m, current_year, confirmed_year),
    )
    # A standalone year is a valid full-year business period.  This rule has
    # lower priority than quarters/months, so their longer spans win.
    add(
        r"(?<!\d)(20\d{2})\s*年(?!\s*(?:[Qq][1-4]|\d{1,2}月))",
        50,
        _build_year,
    )
    add(
        r"(?<![\d.-])(20\d{2})(?![\d.-])(?=\s*(?:的)?(?:生意|经营|表现|报告|分析|BET|媒体|大盘))",
        45,
        _build_year,
    )

    # Longest/highest-priority candidates win and spans may not overlap.
    selected: list[TimeMention] = []
    occupied: list[tuple[int, int]] = []
    for _, item in sorted(
        candidates,
        key=lambda pair: (-pair[0], -(pair[1].end_offset - pair[1].start_offset), pair[1].start_offset),
    ):
        if any(item.start_offset < end and item.end_offset > start for start, end in occupied):
            continue
        selected.append(item)
        occupied.append((item.start_offset, item.end_offset))
    return sorted(selected, key=lambda item: item.start_offset)


def _resolve_year(relative: str | None, explicit: str | None, current_year: int, confirmed: int | None):
    if explicit:
        value = int(explicit)
        return (2000 + value if len(explicit) == 2 else value), "explicit", "exact"
    if relative == "今年":
        return current_year, "relative", "exact"
    if relative == "去年":
        return current_year - 1, "relative", "exact"
    if confirmed:
        return confirmed, "confirmed", "exact"
    # Business users overwhelmingly mean the current data year when they only
    # write a month or quarter. Keep the assumption visible in the trace, but
    # do not interrupt every request with a confirmation turn.
    return current_year, "default_current_year", "exact"


def _build_date_range(match: re.Match, current_year: int, confirmed: int | None):
    relative, explicit, sm, sd, end_year, em, ed = match.groups()
    year, source, status = _resolve_year(relative, explicit, current_year, confirmed)
    end_year_value = int(end_year or year)
    start = _valid_date(year, int(sm), int(sd))
    end = _valid_date(end_year_value, int(em or sm), int(ed))
    if not start or not end or end < start:
        return _mention(match, "DATE_RANGE", start, end, year_source=source, status="invalid", canonical=None)
    canonical = f"{start.year}年{start.month}月{start.day}日至{end.month}月{end.day}日"
    return _mention(match, "DATE_RANGE", start, end, year_source=source, status=status, canonical=canonical)


def _build_iso_date_range(match: re.Match):
    sy, sm, sd, ey, em, ed = map(int, match.groups())
    start, end = _valid_date(sy, sm, sd), _valid_date(ey, em, ed)
    status = "exact" if start and end and end >= start else "invalid"
    canonical = f"{start.isoformat()}~{end.isoformat()}" if status == "exact" else None
    return _mention(match, "DATE_RANGE", start, end, year_source="explicit", status=status, canonical=canonical)


def _build_english_date_range(match: re.Match, current_year: int):
    start_month, start_day, start_year, end_month, end_day, end_year = match.groups()
    explicit_year = start_year or end_year
    sy = int(start_year or explicit_year or current_year)
    ey = int(end_year or explicit_year or sy)
    start = _valid_date(sy, ENGLISH_MONTHS[start_month.casefold()], int(start_day))
    end = _valid_date(ey, ENGLISH_MONTHS[end_month.casefold()], int(end_day))
    status = "exact" if start and end and end >= start else "invalid"
    canonical = (
        f"{start.year}年{start.month}月{start.day}日至{end.year}年{end.month}月{end.day}日"
        if status == "exact" else None
    )
    return _mention(
        match, "DATE_RANGE", start, end,
        year_source="explicit" if explicit_year else "default_current_year",
        status=status, canonical=canonical,
    )


def _build_year(match: re.Match):
    year = int(match.group(1))
    return _mention(
        match, "YEAR", date(year, 1, 1), date(year, 12, 31),
        year_source="explicit", status="exact", canonical=f"{year}年",
    )


def _build_to_date(
    match: re.Match,
    current_year: int,
    confirmed: int | None,
    as_of_date: date | None,
):
    relative, explicit, marker = match.groups()
    year, source, status = _resolve_year(relative, explicit, current_year, confirmed)
    as_of = as_of_date or date.today()
    try:
        end = as_of.replace(year=year)
    except ValueError:
        end = as_of.replace(year=year, day=28)
    normalized = marker.upper()
    if normalized in {"YTD", "年初至今", "今年以来"}:
        start = date(year, 1, 1)
        granularity = "YEAR_TO_DATE"
        canonical = f"{year}年YTD"
    else:
        start = date(year, end.month, 1)
        granularity = "MONTH_TO_DATE"
        canonical = f"{year}年{end.month}月MTD"
    return _mention(
        match, granularity, start, end,
        year_source=source, status=status, canonical=canonical,
    )


def _build_relative_day(match: re.Match, as_of_date: date | None):
    offsets = {"今天": 0, "今日": 0, "昨天": -1, "昨日": -1, "前天": -2}
    selected = (as_of_date or date.today()) + timedelta(days=offsets[match.group(0)])
    return _mention(
        match, "DATE", selected, selected,
        year_source="relative", status="exact",
        canonical=f"{selected.year}年{selected.month}月{selected.day}日",
    )


def _build_month_range(match: re.Match, current_year: int, confirmed: int | None):
    relative, explicit, sm, end_year, em = match.groups()
    year, source, status = _resolve_year(relative, explicit, current_year, confirmed)
    ey = int(end_year or year)
    smi, emi = int(sm), int(em)
    if not (1 <= smi <= 12 and 1 <= emi <= 12):
        return _mention(match, "MONTH_RANGE", None, None, year_source=source, status="invalid", canonical=None)
    start, end = date(year, smi, 1), _month_end(ey, emi)
    if end < start:
        status = "invalid"
    canonical = f"{year}年{smi}-{emi}月" if status != "invalid" else None
    return _mention(match, "MONTH_RANGE", start, end, year_source=source, status=status, canonical=canonical)


def _build_iso_month_range(match: re.Match):
    sy, sm, ey, em = map(int, match.groups())
    if not (1 <= sm <= 12 and 1 <= em <= 12):
        return _mention(match, "MONTH_RANGE", None, None, year_source="explicit", status="invalid", canonical=None)
    start, end = date(sy, sm, 1), _month_end(ey, em)
    status = "exact" if end >= start else "invalid"
    return _mention(
        match, "MONTH_RANGE", start, end, year_source="explicit", status=status,
        canonical=f"{sy}年{sm}-{em}月" if status == "exact" else None,
    )


def _build_spaced_month_range(match: re.Match):
    year, sm, em = map(int, match.groups())
    if not (1 <= sm <= 12 and 1 <= em <= 12):
        return _mention(match, "MONTH_RANGE", None, None, year_source="explicit", status="invalid", canonical=None)
    start, end = date(year, sm, 1), _month_end(year, em)
    status = "exact" if end >= start else "invalid"
    return _mention(
        match, "MONTH_RANGE", start, end, year_source="explicit", status=status,
        canonical=f"{year}年{sm}-{em}月" if status == "exact" else None,
    )


def _build_quarter(match: re.Match, current_year: int, confirmed: int | None):
    relative, explicit, quarter = match.groups()
    year, source, status = _resolve_year(relative, explicit, current_year, confirmed)
    q = int(quarter)
    sm, em = (q - 1) * 3 + 1, q * 3
    return _mention(
        match, "QUARTER", date(year, sm, 1), _month_end(year, em),
        year_source=source, status=status, canonical=f"{year}年Q{q}",
    )


def _build_month(match: re.Match, current_year: int, confirmed: int | None):
    relative, explicit, month = match.groups()
    year, source, status = _resolve_year(relative, explicit, current_year, confirmed)
    value = int(month)
    if not 1 <= value <= 12:
        return _mention(match, "MONTH", None, None, year_source=source, status="invalid", canonical=None)
    return _mention(
        match, "MONTH", date(year, value, 1), _month_end(year, value),
        year_source=source, status=status, canonical=f"{year}年{value}月",
    )


def _build_single_date(match: re.Match, current_year: int, confirmed: int | None):
    relative, explicit, month, day = match.groups()
    year, source, status = _resolve_year(relative, explicit, current_year, confirmed)
    value = _valid_date(year, int(month), int(day))
    canonical = f"{year}年{int(month)}月{int(day)}日" if value else None
    return _mention(match, "DATE", value, value, year_source=source, status=status if value else "invalid", canonical=canonical)


def _build_iso_single_date(match: re.Match):
    year, month, day = map(int, match.groups())
    value = _valid_date(year, month, day)
    return _mention(
        match, "DATE", value, value, year_source="explicit",
        status="exact" if value else "invalid",
        canonical=f"{year}-{month:02d}-{day:02d}" if value else None,
    )


def _build_iso_single_month(match: re.Match):
    year, month = map(int, match.groups())
    if not 1 <= month <= 12:
        return _mention(match, "MONTH", None, None, year_source="explicit", status="invalid", canonical=None)
    return _mention(
        match, "MONTH", date(year, month, 1), _month_end(year, month),
        year_source="explicit", status="exact", canonical=f"{year}年{month}月",
    )


def _build_relative_month(match: re.Match, current_year: int):
    today = date.today()
    if match.group(0) == "上月":
        first = today.replace(day=1)
        target = first - timedelta(days=1)
    else:
        target = today
    return _mention(
        match, "MONTH", date(target.year, target.month, 1), _month_end(target.year, target.month),
        year_source="relative", status="exact", canonical=f"{target.year}年{target.month}月",
    )


def _build_campaign(match: re.Match, current_year: int, confirmed: int | None):
    explicit, name = match.groups()
    normalized = "双11" if name == "双十一" else name
    _, source, year_status = _resolve_year(None, explicit, current_year, confirmed)
    status = "needs_campaign_window"
    if year_status == "needs_year_confirmation":
        status = "needs_year_and_campaign_window"
    return _mention(
        match, "CAMPAIGN", None, None, year_source=source, status=status,
        canonical=f"{int(explicit or confirmed or current_year)}年{normalized}",
    )


def resolve_time_scope(
    text: str,
    *,
    current_year: int | None = None,
    confirmed_year: int | None = None,
    as_of_date: date | None = None,
) -> TimeScope:
    year = int(current_year or date.today().year)
    mentions = _extract_time_candidates(text, year, confirmed_year, as_of_date)
    if not mentions:
        return TimeScope()

    campaign = next((item for item in mentions if item.granularity == "CAMPAIGN"), None)
    supplied_window = next(
        (
            item for item in mentions
            if item.granularity == "DATE_RANGE" and item.resolution_status == "exact"
        ),
        None,
    )
    if campaign and supplied_window:
        campaign.role = "CAMPAIGN_LABEL"
        campaign.start_date = supplied_window.start_date
        campaign.end_date = supplied_window.end_date
        campaign.year_source = supplied_window.year_source
        campaign.resolution_status = "exact"
        campaign.canonical = supplied_window.canonical
    role_mentions = [item for item in mentions if item.role != "CAMPAIGN_LABEL"] or mentions

    if len(role_mentions) > 1:
        for index, mention in enumerate(role_mentions):
            mention.role = "FOCUS" if index == 0 else "UNRESOLVED"
        for index in range(len(role_mentions) - 1):
            between = text[role_mentions[index].end_offset:role_mentions[index + 1].start_offset].casefold()
            if (
                re.search(r"同比|对比|比较|(?<![A-Za-z0-9_])vs(?![A-Za-z0-9_])|去年同期", between)
                or re.fullmatch(r"\s*(?:环比|比)\s*", between)
            ):
                role_mentions[index].role = "FOCUS"
                role_mentions[index + 1].role = "COMPARISON"
    comparison_mentions = [item for item in role_mentions if item.role == "COMPARISON"]
    focus_candidates = [item for item in role_mentions if item.role == "FOCUS"]
    focus = focus_candidates[0] if focus_candidates else role_mentions[0]

    missing = []
    statuses = {item.resolution_status for item in mentions}
    if "invalid" in statuses:
        missing.append("time_scope.valid_period")
    if "needs_year_confirmation" in statuses or "needs_year_and_campaign_window" in statuses:
        missing.append("time_scope.year_confirmation")
    if "needs_campaign_window" in statuses or "needs_year_and_campaign_window" in statuses:
        missing.append("time_scope.campaign_window")
    if any(item.role == "UNRESOLVED" for item in mentions):
        missing.append("time_scope.period_roles")

    focus_period = None
    if focus.start_date and focus.end_date and focus.resolution_status == "exact":
        comparison = comparison_mentions[0] if comparison_mentions else None
        comparison_start = comparison.start_date if comparison else _prior_year(date.fromisoformat(focus.start_date)).isoformat()
        comparison_end = comparison.end_date if comparison else _prior_year(date.fromisoformat(focus.end_date)).isoformat()
        focus_period = ResolvedPeriod(
            focus.canonical or focus.raw_text,
            start_date=focus.start_date, end_date=focus.end_date,
            comparison_start=comparison_start, comparison_end=comparison_end,
        )
    status = "exact" if not missing and focus_period else "needs_clarification"
    return TimeScope(
        mentions=mentions, focus_period=focus_period,
        comparison_periods=[asdict(item) for item in comparison_mentions],
        campaign_name=(campaign.raw_text if campaign else None),
        missing_slots=list(dict.fromkeys(missing)), status=status,
    )


def _comparison_period_payload(mention: TimeMention) -> dict:
    return {
        "label": mention.canonical or mention.raw_text,
        "start_date": mention.start_date,
        "end_date": mention.end_date,
    }


def _prior_year_period_payload(mention: TimeMention) -> dict:
    start = _prior_year(date.fromisoformat(str(mention.start_date)))
    end = _prior_year(date.fromisoformat(str(mention.end_date)))
    return {
        "label": f"{start.isoformat()}~{end.isoformat()}",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }


def _is_aligned_prior_year(focus: TimeMention, baseline: TimeMention) -> bool:
    if not all((focus.start_date, focus.end_date, baseline.start_date, baseline.end_date)):
        return False
    focus_start, focus_end = date.fromisoformat(focus.start_date), date.fromisoformat(focus.end_date)
    baseline_start, baseline_end = date.fromisoformat(baseline.start_date), date.fromisoformat(baseline.end_date)
    return _prior_year(focus_start) == baseline_start and _prior_year(focus_end) == baseline_end


def build_comparison_spec(scope: TimeScope, text: str = "") -> dict:
    """Compile extracted time mentions into a business comparison contract."""
    mentions = [
        item for item in scope.mentions
        if item.role != "CAMPAIGN_LABEL"
        and item.resolution_status == "exact"
        and item.start_date and item.end_date
    ]
    if not mentions:
        return {}
    lowered = str(text or "").casefold()
    focus_mentions = [item for item in mentions if item.role == "FOCUS"]
    comparison_mentions = [item for item in mentions if item.role == "COMPARISON"]

    if "环比" in lowered and len(mentions) >= 2:
        return {
            "mode": "MOM",
            "analysis_periods": [_comparison_period_payload(mentions[0])],
            "baseline_periods": [_comparison_period_payload(mentions[1])],
            "comparison_target": "ACTUAL",
            "source": "explicit",
        }
    direct_actual = bool(re.search(
        r"绝对值|哪个.{0,4}(?:高|大|多)|(?:多|少)多少|(?:gmv|销售额).{0,6}(?:高|大|多)",
        lowered, re.I,
    ))
    if direct_actual and len(mentions) >= 2:
        focus = focus_mentions[0] if focus_mentions else mentions[0]
        baseline = comparison_mentions[0] if comparison_mentions else mentions[1]
        return {
            "mode": "DIRECT_ACTUAL",
            "analysis_periods": [_comparison_period_payload(focus)],
            "baseline_periods": [_comparison_period_payload(baseline)],
            "comparison_target": "ACTUAL",
            "source": "explicit",
        }
    if len(mentions) == 2 and _is_aligned_prior_year(mentions[0], mentions[1]):
        return {
            "mode": "YOY_ALIGNED",
            "analysis_periods": [_comparison_period_payload(mentions[0])],
            "baseline_periods": [_comparison_period_payload(mentions[1])],
            "comparison_target": "EVOLUTION",
            "source": "inferred_aligned_pair",
        }
    if comparison_mentions:
        focus = focus_mentions[0] if focus_mentions else mentions[0]
        baseline = comparison_mentions[0]
        if (
            focus.start_date and baseline.start_date
            and focus.start_date[:4] == baseline.start_date[:4]
        ):
            return {
                "mode": "YOY_ALIGNED",
                "analysis_periods": [_comparison_period_payload(item) for item in mentions],
                "baseline_periods": [_prior_year_period_payload(item) for item in mentions],
                "comparison_target": "EVOLUTION",
                "source": "business_default",
            }
        aligned = _is_aligned_prior_year(focus, baseline)
        return {
            "mode": "YOY_ALIGNED" if aligned else "EXPLICIT_BASELINE",
            "analysis_periods": [_comparison_period_payload(focus)],
            "baseline_periods": [_comparison_period_payload(baseline)],
            "comparison_target": "EVOLUTION" if aligned else "ACTUAL",
            "source": "explicit",
        }
    return {
        "mode": "YOY_ALIGNED",
        "analysis_periods": [_comparison_period_payload(item) for item in mentions],
        "baseline_periods": [_prior_year_period_payload(item) for item in mentions],
        "comparison_target": "EVOLUTION",
        "source": "business_default" if len(mentions) > 1 else "implicit_yoy",
    }


@lru_cache(maxsize=1)
def _brand_registry():
    payload = json.loads(REFERENCE_PATH.read_text(encoding="utf-8")) if REFERENCE_PATH.exists() else {}
    mappings = payload.get("mappings") or {}
    conflicts = payload.get("conflicts") or {}
    records: dict[str, dict] = {}
    alias_to_keys: dict[str, set[str]] = {}

    def add_record(chinese: str, english: str):
        key = _norm(english) or _norm(chinese)
        record = records.setdefault(key, {
            "key": key, "display": english or chinese, "aliases": [], "source_mappings": {},
        })
        for alias in (chinese, english):
            if alias and alias not in record["aliases"]:
                record["aliases"].append(alias)
            normalized = _norm(alias)
            if normalized:
                alias_to_keys.setdefault(normalized, set()).add(key)

    for chinese, english in mappings.items():
        add_record(str(chinese), str(english))
    for chinese, english_values in conflicts.items():
        for english in english_values:
            add_record(str(chinese), str(english))

    if MEDIA_ALIAS_PATH.exists():
        aliases_payload = json.loads(MEDIA_ALIAS_PATH.read_text(encoding="utf-8"))
        for alias, source_values in (aliases_payload.get("aliases") or {}).items():
            normalized = _norm(alias)
            # The alias key is often the Chinese/canonical name while a source
            # uses a different official English value (谷雨 -> GRAIN RAIN).
            matching_keys = set(alias_to_keys.get(normalized, set()))
            for source_value in (source_values or {}).values():
                matching_keys.update(alias_to_keys.get(_norm(source_value), set()))
            for key in matching_keys:
                if alias not in records[key]["aliases"]:
                    records[key]["aliases"].append(alias)
                alias_to_keys.setdefault(normalized, set()).add(key)
                records[key]["source_mappings"].update(source_values or {})
    for alias, canonical in CONFIRMED_BRAND_ALIASES.items():
        canonical_keys = alias_to_keys.get(_norm(canonical), set())
        for key in canonical_keys:
            if alias not in records[key]["aliases"]:
                records[key]["aliases"].append(alias)
            alias_to_keys.setdefault(_norm(alias), set()).add(key)
    return records, alias_to_keys


NON_BRAND_TERMS = (
    "请重新分析", "重新分析", "分析一下", "帮我", "请", "麻烦", "看一下", "看看", "看下", "分析", "生成", "做",
    "天猫", "抖音", "京东", "三平台", "全平台", "tmall", "douyin", "jingdong",
    "品牌生意", "生意表现", "经营表现", "媒体投资", "媒体花费", "投资情况", "投资表现",
    "主推商品", "商品表现", "产品表现", "表现", "生意", "经营", "gmv", "bet", "投放",
    "重点看", "同比去年同期", "同比", "对比", "怎么样", "如何", "是什么样的", "情况", "报告", "平台",
    "mass top品牌", "mass beauty", "pure mass", "beauty market", "selective", "professional",
    "top品牌", "top 3", "top3", "top 5", "top5", "品牌", "品牌排名", "哪些品牌", "大盘", "市场",
    "segment", "category", "女士护肤", "彩妆", "男士护肤", "选品", "生意节奏", "价格", "促销",
    "key driver", "keydriver", "driver contribution", "李佳琦", "佳琦", "t2", "non-kol",
    "kol直播", "达人推广直播", "品牌自营直播", "短视频及其他", "短视频",
    "三平台生意分析模板", "生意分析模板", "分析模板", "模板", "是哪些",
    # Quantity-style asks ("生意是多少"/"卖了多少钱") close with a different verb
    # than the descriptive "怎么样/如何/情况" phrasings above; without these the
    # trailing clause survives inside the extracted brand span.
    "是多少钱", "是多少", "有多少", "多少钱", "多少",
    "卖了多少钱", "花了多少钱", "赚了多少钱", "卖了多少", "花了多少", "赚了多少",
    "销售额", "销量",
    "官方旗舰店", "海外旗舰店", "全球旗舰店", "旗舰店", "专卖店", "自营店", "官方店",
)


def _is_brandless_market_question(text: str) -> bool:
    value = unicodedata.normalize("NFKC", text).casefold()
    explicit_market = any(term in value for term in (
        "大盘", "市场整体", "整体市场", "pure mass", "beauty market",
        "mass beauty", "selective", "professional",
    )) or bool(re.search(r"\bmass\b", value))
    ranking = bool(re.search(r"\btop\s*\d*|排名|哪些品牌|品牌.*(?:最好|最高|最快)", value))
    return explicit_market and ranking


def _span_overlaps(start: int, end: int, blocked: list[tuple[int, int]]) -> bool:
    return any(start < blocked_end and end > blocked_start for blocked_start, blocked_end in blocked)


@lru_cache(maxsize=1)
def _brand_alias_entries():
    records, alias_to_keys = _brand_registry()
    aliases = {alias for record in records.values() for alias in record["aliases"] if len(alias.strip()) >= 2}
    return [
        (
            unicodedata.normalize("NFKC", alias).casefold(),
            alias,
            alias_to_keys.get(_norm(alias), set()),
            bool(re.fullmatch(r"[a-z0-9 .&'\-]+", alias.casefold())),
        )
        for alias in sorted(aliases, key=len, reverse=True)
    ]


def _exact_brand_mentions(text: str, blocked: list[tuple[int, int]]):
    matches = []
    lowered = unicodedata.normalize("NFKC", text).casefold()
    for needle, alias, keys, needs_boundary in _brand_alias_entries():
        start = lowered.find(needle)
        while start >= 0:
            end = start + len(needle)
            boundary_ok = not needs_boundary or (
                (start == 0 or not lowered[start - 1].isalnum())
                and (end == len(lowered) or not lowered[end].isalnum())
            )
            if boundary_ok and not _span_overlaps(start, end, blocked):
                matches.append((start, end, alias, keys))
            start = lowered.find(needle, start + 1)
    selected = []
    for item in sorted(matches, key=lambda row: (-(row[1] - row[0]), row[0])):
        if not any(_span_overlaps(item[0], item[1], [(other[0], other[1])]) for other in selected):
            selected.append(item)
    return sorted(selected, key=lambda row: row[0])


def _fallback_brand_span(text: str, blocked: list[tuple[int, int]]):
    quote = re.search(r"[“\"'‘]([^”\"'’]{1,40})[”\"'’]", text)
    if quote and not _span_overlaps(quote.start(1), quote.end(1), blocked):
        return quote.group(1).strip(), quote.start(1), quote.end(1)
    chars = list(text)
    for start, end in blocked:
        chars[start:end] = " " * (end - start)
    masked = "".join(chars)
    for term in sorted(NON_BRAND_TERMS, key=len, reverse=True):
        for match in re.finditer(re.escape(term), masked, re.I):
            chars[match.start():match.end()] = " " * (match.end() - match.start())
        masked = "".join(chars)
    for match in re.finditer(r"[A-Za-z][A-Za-z0-9&.'’\- ]{0,35}|[\u4e00-\u9fffA-Za-z0-9&.'’\-]{2,30}", masked):
        surface = re.sub(r"^(?:的|在|于|和)+|(?:的|在|于|和|里面)+$", "", match.group(0).strip())
        surface = re.split(r"[,，。；;：:]", surface, maxsplit=1)[0].strip()
        if surface and surface not in {"这个品牌", "该品牌", "哪些品牌", "今年", "去年", "本月", "上月"}:
            offset = text.find(surface, match.start(), match.end())
            return surface, offset, offset + len(surface)
    return None, None, None


def _candidate_payload(keys: set[str], score: float = 1.0):
    records, _ = _brand_registry()
    return [
        BrandCandidate(
            canonical_brand_key=key,
            canonical_display_name=records[key]["display"],
            aliases=list(records[key]["aliases"]), score=score,
        )
        for key in sorted(keys)
    ]


def resolve_brand(
    text: str,
    time_scope: TimeScope,
    *,
    selected_brand_key: str | None = None,
) -> BrandResolution:
    if _is_brandless_market_question(text):
        return BrandResolution(match_method="market_brand_not_required")
    records, alias_to_keys = _brand_registry()
    blocked = [(item.start_offset, item.end_offset) for item in time_scope.mentions]
    for term in sorted(NON_BRAND_TERMS, key=len, reverse=True):
        blocked.extend((match.start(), match.end()) for match in re.finditer(re.escape(term), text, re.I))
    exact = _exact_brand_mentions(text, blocked)
    keys = set().union(*(item[3] for item in exact)) if exact else set()
    if selected_brand_key and selected_brand_key in records:
        keys = {selected_brand_key}
    if len(keys) > 1:
        surface = " / ".join(item[2] for item in exact)
        return BrandResolution(
            surface=surface, normalized_surface=_norm(surface), status="ambiguous",
            candidates=_candidate_payload(keys), match_method="multiple_exact_brands",
        )
    if len(keys) == 1:
        key = next(iter(keys))
        record = records[key]
        matched = next((item for item in exact if key in item[3]), None)
        surface = text[matched[0]:matched[1]] if matched else record["display"]
        return BrandResolution(
            surface=surface, start_offset=matched[0] if matched else None,
            end_offset=matched[1] if matched else None,
            normalized_surface=_norm(surface), canonical_brand_key=key,
            canonical_display_name=record["display"], aliases=list(record["aliases"]),
            source_mappings=dict(record["source_mappings"]), status="resolved",
            match_method="registry_exact",
        )

    surface, start, end = _fallback_brand_span(text, blocked)
    if not surface:
        return BrandResolution()
    normalized = _norm(surface)
    exact_keys = alias_to_keys.get(normalized, set())
    if len(exact_keys) == 1:
        key = next(iter(exact_keys))
        record = records[key]
        return BrandResolution(
            surface=surface, start_offset=start, end_offset=end,
            normalized_surface=normalized, canonical_brand_key=key,
            canonical_display_name=record["display"], aliases=list(record["aliases"]),
            source_mappings=dict(record["source_mappings"]), status="resolved",
            match_method="registry_normalized_exact",
        )
    scored = []
    for alias_norm, candidate_keys in alias_to_keys.items():
        score = SequenceMatcher(None, normalized, alias_norm).ratio()
        one_cjk_edit = bool(
            re.fullmatch(r"[\u4e00-\u9fff]{3,6}", normalized)
            and len(normalized) == len(alias_norm)
            and sum(left != right for left, right in zip(normalized, alias_norm)) == 1
        )
        if score >= 0.78 or one_cjk_edit:
            for key in candidate_keys:
                scored.append((max(score, 0.75), key))
    best = []
    seen = set()
    for score, key in sorted(scored, reverse=True):
        if key not in seen:
            best.append(BrandCandidate(
                canonical_brand_key=key, canonical_display_name=records[key]["display"],
                aliases=list(records[key]["aliases"]), score=round(score, 3),
            ))
            seen.add(key)
        if len(best) == 3:
            break
    return BrandResolution(
        surface=surface, start_offset=start, end_offset=end,
        normalized_surface=normalized, status="ambiguous" if best else "not_found",
        candidates=best, match_method="fuzzy_candidates" if best else "not_found",
    )


def resolve_entities(
    text: str,
    *,
    current_year: int | None = None,
    confirmed_year: int | None = None,
    selected_brand_key: str | None = None,
) -> EntityResolution:
    time_scope = resolve_time_scope(
        text, current_year=current_year, confirmed_year=confirmed_year,
    )
    brand = resolve_brand(text, time_scope, selected_brand_key=selected_brand_key)
    reasons = []
    if time_scope.missing_slots:
        reasons.append("TIME_SCOPE_NEEDS_CLARIFICATION")
    if brand.status in {"ambiguous", "not_found"}:
        reasons.append("BRAND_NEEDS_CONFIRMATION")
    return EntityResolution(text=text, time_scope=time_scope, brand=brand, reason_codes=reasons)
