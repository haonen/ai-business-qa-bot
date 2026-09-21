from __future__ import annotations

"""Business-owned Key Driver vocabulary and platform semantics.

Key Drivers describe commerce GMV channels.  Their names must not be reused as
BET/media intent keywords merely because strings such as ``Non-KOL`` contain
``KOL``.
"""

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class KeyDriverMatch:
    platform: str | None
    drivers: tuple[str, ...]
    ambiguous: bool = False


TMALL_PATTERNS = (
    (r"李佳琦|佳琦", "李佳琦"),
    (r"(?<![A-Za-z0-9])T2(?![A-Za-z0-9])", "T2"),
    (r"(?<![A-Za-z0-9])Non[\s_-]*KOL(?![A-Za-z0-9])", "Non-KOL"),
)

DOUYIN_PATTERNS = (
    (r"KOL\s*直播|达人推广直播|达人直播", "KOL直播"),
    (r"品牌自营直播|自营直播|品牌自播", "品牌自营直播"),
    (r"短视频(?:及其他)?", "短视频及其他"),
)


def _matches(text: str, patterns: tuple[tuple[str, str], ...]) -> list[str]:
    return [canonical for pattern, canonical in patterns if re.search(pattern, text, re.I)]


def detect_key_drivers(text: str) -> KeyDriverMatch:
    tmall = _matches(str(text or ""), TMALL_PATTERNS)
    douyin = _matches(str(text or ""), DOUYIN_PATTERNS)
    drivers = tuple(dict.fromkeys([*tmall, *douyin]))
    if tmall and douyin:
        return KeyDriverMatch(None, drivers, True)
    if tmall:
        return KeyDriverMatch("TM", drivers)
    if douyin:
        return KeyDriverMatch("DY", drivers)
    return KeyDriverMatch(None, ())


def inferred_key_driver_platform(text: str) -> str | None:
    match = detect_key_drivers(text)
    return None if match.ambiguous else match.platform


def has_explicit_media_intent(text: str) -> bool:
    """Return BET/media intent without treating commerce driver names as media."""
    value = str(text or "").casefold()
    # Remove the complete commerce driver before looking for contextual KOL
    # media phrases; otherwise "Non-KOL表现" contains the substring "KOL表现".
    media_value = re.sub(r"non[\s_-]*kol", "", value, flags=re.I)
    explicit = (
        "bet", "媒体投资", "媒体花费", "媒体费用", "媒体费比",
        "站外投放", "站外投资", "bkfs", "bkfst", "ksi",
        "social search", "socialsearch", "search report", "社交搜索", "搜索指数",
        "take rate", "take-rate", "take_rate", "cpe", "engage",
        "抖音投放", "小红书投放", "达人投放",
    )
    if any(token in media_value for token in explicit):
        return True
    if re.search(r"(?:kol|达人).{0,8}(?:投放|花费|成本|投资|媒体|表现)", media_value, re.I):
        return not bool(re.search(r"kol\s*直播|达人(?:推广)?直播", media_value, re.I))
    return False
