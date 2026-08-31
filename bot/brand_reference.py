from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path
import unicodedata


_REFERENCE_PATH = Path(__file__).resolve().parent / "data" / "brand_cn_en_reference.json"


def _key(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


@lru_cache(maxsize=1)
def _load_reference() -> tuple[dict[str, str], set[str]]:
    if not _REFERENCE_PATH.exists():
        return {}, set()
    try:
        payload = json.loads(_REFERENCE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}, set()
    mappings = payload.get("mappings") if isinstance(payload, dict) else {}
    conflicts = payload.get("conflicts") if isinstance(payload, dict) else {}
    normalized: dict[str, str] = {}
    for chinese, english in (mappings or {}).items():
        key = _key(chinese)
        value = str(english or "").strip()
        if key and value:
            normalized[key] = value
    conflict_keys = {_key(value) for value in (conflicts or {}) if _key(value)}
    return normalized, conflict_keys


def english_brand_for_chinese(value: object) -> str | None:
    """Return a deterministic English fallback only for an unambiguous Chinese name."""
    key = _key(value)
    if not key:
        return None
    mappings, conflicts = _load_reference()
    if key in conflicts:
        return None
    return mappings.get(key)
