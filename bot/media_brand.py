from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import lru_cache
import json
import logging
import os
from pathlib import Path
import re
import unicodedata

from bot.db.connection import fetch_df, fetch_one, get_engine
from bot.utils import extract_json_object, llm_client


SOURCES = ("search", "topline", "ksi", "tmall", "dy")
LOOKUP_SOURCES = (*SOURCES, "nso")
_ALIAS_PATH = Path(__file__).resolve().parent / "data" / "media_brand_aliases.json"
log = logging.getLogger(__name__)
_SOURCE_RESOLUTION_CACHE: dict[tuple[str, str], dict] = {}


@dataclass(frozen=True)
class ResolvedBrands:
    search_brand: str
    topline_brand: str
    ksi_brand: str
    tmall_brand: str
    dy_brand: str | None = None

    def by_source(self) -> dict[str, str]:
        return {
            "search": self.search_brand,
            "topline": self.topline_brand,
            "ksi": self.ksi_brand,
            "tmall": self.tmall_brand,
            "dy": self.dy_brand,
        }


def normalize_brand(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(
        r"(官方旗舰店|品牌旗舰店|海外旗舰店|旗舰店|官方店|专卖店|专营店)$",
        "",
        text,
    )
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


def _load_aliases() -> dict:
    if not _ALIAS_PATH.exists():
        return {}
    data = json.loads(_ALIAS_PATH.read_text(encoding="utf-8"))
    return data.get("aliases", data) if isinstance(data, dict) else {}


def _complete_mapping(mapping: dict[str, str | None]) -> ResolvedBrands | None:
    if not all(mapping.get(source) for source in SOURCES):
        return None
    return ResolvedBrands(
        search_brand=str(mapping["search"]),
        topline_brand=str(mapping["topline"]),
        ksi_brand=str(mapping["ksi"]),
        tmall_brand=str(mapping["tmall"]),
        dy_brand=str(mapping["dy"]),
    )


def _read_resolution_cache(normalized_input: str) -> ResolvedBrands | None:
    try:
        row = fetch_one(
            """
            SELECT search_brand, topline_brand, ksi_brand, tmall_brand, dy_brand
            FROM ai_bot_brand_resolution_cache
            WHERE normalized_input = :normalized_input
            """,
            {"normalized_input": normalized_input},
        )
    except Exception as exc:
        log.warning("[media_brand] resolution cache unavailable: %s", exc)
        return None
    return _complete_mapping({
        "search": row.get("search_brand"),
        "topline": row.get("topline_brand"),
        "ksi": row.get("ksi_brand"),
        "tmall": row.get("tmall_brand"),
        "dy": row.get("dy_brand"),
    }) if row else None


def _write_resolution_cache(
    normalized_input: str,
    user_brand: str,
    resolved: ResolvedBrands,
    confidence: float,
):
    try:
        from sqlalchemy import text

        with get_engine().begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO ai_bot_brand_resolution_cache (
                        normalized_input, user_brand,
                        search_brand, topline_brand, ksi_brand, tmall_brand, dy_brand,
                        confidence
                    ) VALUES (
                        :normalized_input, :user_brand,
                        :search_brand, :topline_brand, :ksi_brand, :tmall_brand, :dy_brand,
                        :confidence
                    )
                    ON DUPLICATE KEY UPDATE
                        user_brand = VALUES(user_brand),
                        search_brand = VALUES(search_brand),
                        topline_brand = VALUES(topline_brand),
                        ksi_brand = VALUES(ksi_brand),
                        tmall_brand = VALUES(tmall_brand),
                        dy_brand = VALUES(dy_brand),
                        confidence = VALUES(confidence),
                        updated_at = CURRENT_TIMESTAMP
                    """
                ),
                {
                    "normalized_input": normalized_input,
                    "user_brand": user_brand,
                    **asdict(resolved),
                    "confidence": round(confidence, 4),
                },
            )
    except Exception as exc:
        # Cache writes must never break an otherwise valid report.
        log.warning("[media_brand] failed to persist resolution cache: %s", exc)


def _read_source_resolution_cache(normalized_input: str, source: str) -> dict:
    try:
        return fetch_one(
            """
            SELECT source_brand, match_method, confidence
            FROM ai_bot_source_brand_resolution_cache
            WHERE normalized_input = :normalized_input
              AND source_name = :source_name
            """,
            {"normalized_input": normalized_input, "source_name": source},
        )
    except Exception as exc:
        # Keep compatibility while the migration is being rolled out.
        log.warning("[media_brand] source resolution cache unavailable: %s", exc)
        return {}


def _write_source_resolution_cache(
    normalized_input: str,
    user_brand: str,
    source: str,
    source_brand: str,
    match_method: str,
    confidence: float | None = None,
):
    try:
        from sqlalchemy import text

        with get_engine().begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO ai_bot_source_brand_resolution_cache (
                        normalized_input, source_name, user_brand, source_brand,
                        match_method, confidence
                    ) VALUES (
                        :normalized_input, :source_name, :user_brand, :source_brand,
                        :match_method, :confidence
                    )
                    ON DUPLICATE KEY UPDATE
                        user_brand = VALUES(user_brand),
                        source_brand = VALUES(source_brand),
                        match_method = VALUES(match_method),
                        confidence = VALUES(confidence),
                        updated_at = CURRENT_TIMESTAMP
                    """
                ),
                {
                    "normalized_input": normalized_input,
                    "source_name": source,
                    "user_brand": user_brand,
                    "source_brand": source_brand,
                    "match_method": match_method,
                    "confidence": confidence,
                },
            )
    except Exception as exc:
        log.warning("[media_brand] failed to persist source resolution cache: %s", exc)


def _source_resolution_success(
    *,
    user_brand: str,
    normalized_input: str,
    source: str,
    source_brand: str,
    match_method: str,
    confidence: float | None = None,
) -> dict:
    result = {
        "input": user_brand,
        "source": source,
        "brand": str(source_brand),
        "match_method": match_method,
    }
    cache_key = (source, normalized_input)
    _SOURCE_RESOLUTION_CACHE[cache_key] = result
    _write_source_resolution_cache(
        normalized_input,
        user_brand,
        source,
        str(source_brand),
        match_method,
        confidence,
    )
    return dict(result)


def _dictionary_rows(
    normalized_values: tuple[str, ...],
    sources: tuple[str, ...] = SOURCES,
    *,
    include_prefix: bool = False,
) -> list[dict]:
    values = tuple(dict.fromkeys(value for value in normalized_values if value))
    if not values or not sources:
        return []
    params: dict[str, str] = {}
    source_clauses = []
    value_clauses = []
    for index, source in enumerate(sources):
        key = f"source_{index}"
        source_clauses.append(f":{key}")
        params[key] = source
    for index, value in enumerate(values):
        key = f"value_{index}"
        value_clauses.append(f"normalized_brand = :{key}")
        params[key] = value
        if include_prefix and len(value) >= 3:
            prefix_key = f"prefix_{index}"
            value_clauses.append(f"normalized_brand LIKE :{prefix_key}")
            params[prefix_key] = f"{value}%"
    limit = max(20, min(int(os.environ.get("MEDIA_BRAND_LOOKUP_LIMIT", "80")), 200))
    df = fetch_df(
        f"""
        SELECT source_name, source_brand, normalized_brand
        FROM ai_bot_brand_dictionary
        WHERE source_name IN ({", ".join(source_clauses)})
          AND ({" OR ".join(value_clauses)})
        ORDER BY source_name, CHAR_LENGTH(source_brand), source_brand
        LIMIT {limit}
        """,
        params,
    )
    return df.to_dict(orient="records") if not df.empty else []


def _group_candidates(rows: list[dict]) -> dict[str, tuple[str, ...]]:
    grouped = {source: [] for source in LOOKUP_SOURCES}
    for row in rows:
        source = str(row.get("source_name") or "")
        brand = str(row.get("source_brand") or "").strip()
        if source in grouped and brand and brand not in grouped[source]:
            grouped[source].append(brand)
    return {source: tuple(values) for source, values in grouped.items()}


def _tmall_brand_index_matches(normalized_values: tuple[str, ...]) -> tuple[dict, ...]:
    """Read the small store/brand alias index; never scan the Tmall fact table."""
    values = tuple(dict.fromkeys(value for value in normalized_values if value))
    if not values:
        return ()
    params = {f"alias_{index}": value for index, value in enumerate(values)}
    placeholders = ", ".join(f":alias_{index}" for index in range(len(values)))
    try:
        df = fetch_df(
            f"""
            SELECT
              source_brand,
              SUM(observation_count) AS observation_count,
              GROUP_CONCAT(DISTINCT alias_source ORDER BY alias_source) AS alias_sources
            FROM ai_bot_tmall_brand_index
            WHERE normalized_alias IN ({placeholders})
            GROUP BY source_brand
            ORDER BY observation_count DESC, source_brand
            LIMIT 20
            """,
            params,
        )
    except Exception as exc:
        # Keep rolling deployments compatible until the index table is built.
        log.warning("[media_brand] tmall brand index unavailable: %s", exc)
        return ()
    return tuple(df.to_dict(orient="records")) if not df.empty else ()


def _match_brands(rows: tuple[dict, ...]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            str(row.get("source_brand") or "").strip()
            for row in rows
            if str(row.get("source_brand") or "").strip()
        )
    )


def _tmall_index_method(rows: tuple[dict, ...], *, direct: bool) -> str:
    sources = {
        source
        for row in rows
        for source in str(row.get("alias_sources") or "").split(",")
        if source
    }
    if direct and ("store_cn" in sources or "store_en" in sources):
        return "tmall_store_exact"
    return "tmall_brand_index_exact" if direct else "tmall_alias_index_exact"


def _tmall_exact_fact_candidates(values: tuple[str, ...]) -> tuple[str, ...]:
    """Index-backed fallback when the dictionary refresh is temporarily unavailable."""
    names = tuple(dict.fromkeys(str(value or "").strip() for value in values if str(value or "").strip()))
    if not names:
        return ()
    params = {f"name_{index}": value for index, value in enumerate(names)}
    placeholders = ", ".join(f":name_{index}" for index in range(len(names)))
    df = fetch_df(
        f"""
        SELECT DISTINCT brand_name AS source_brand
        FROM ai_bot_tmall_product_link FORCE INDEX (idx_tmall_brand_date)
        WHERE brand_name IN ({placeholders})
        ORDER BY brand_name
        LIMIT 10
        """,
        params,
    )
    if df.empty:
        return ()
    return tuple(
        dict.fromkeys(
            str(value).strip()
            for value in df["source_brand"].dropna().tolist()
            if str(value).strip()
        )
    )


@lru_cache(maxsize=256)
def _generate_brand_variants(user_brand: str) -> tuple[str, ...]:
    variants = [user_brand]
    if not os.environ.get("DASHSCOPE_API_KEY"):
        return tuple(variants)
    prompt = f"""
为品牌“{user_brand}”生成数据库检索名称，最多8个。
必须尽量同时给出该品牌真实的官方中文名与官方英文/罗马字品牌名：
用户输入中文时补充英文名，用户输入英文时补充中文名。
也可以包含常见音译及去掉店铺后缀后的名称。
不要加入母公司、集团名或其他子品牌。
只返回JSON：{{"variants":["名称1","名称2"]}}
"""
    try:
        response = llm_client(max_retries=0).chat.completions.create(
            model=os.environ.get("DASHSCOPE_ROUTER_MODEL", "qwen3.7-plus"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=160,
            timeout=float(os.environ.get("MEDIA_BRAND_LLM_TIMEOUT", "12")),
        )
        parsed = extract_json_object(response.choices[0].message.content or "")
        for value in parsed.get("variants") or []:
            value = str(value or "").strip()
            if value and len(value) <= 80:
                variants.append(value)
    except Exception as exc:
        log.warning("[media_brand] variant generation failed for %s: %s", user_brand, exc)
    unique = []
    seen = set()
    for value in variants:
        key = normalize_brand(value)
        if key and key not in seen:
            unique.append(value)
            seen.add(key)
    return tuple(unique[:8])


def _validate_selected_brand(value: str, candidates: tuple[str, ...]) -> str | None:
    if value in candidates:
        return value
    normalized = normalize_brand(value)
    matches = [candidate for candidate in candidates if normalize_brand(candidate) == normalized]
    return matches[0] if len(matches) == 1 else None


def _select_source_mappings(
    user_brand: str,
    candidates: dict[str, tuple[str, ...]],
) -> tuple[dict[str, str], dict[str, list[str]], float]:
    unresolved = {source: values for source, values in candidates.items() if values}
    if not unresolved:
        return {}, {}, 0.0
    if not os.environ.get("DASHSCOPE_API_KEY"):
        return {}, {source: list(values[:5]) for source, values in unresolved.items()}, 0.0
    prompt = f"""
用户输入品牌：{user_brand}
各数据源真实候选：
{json.dumps({k: list(v) for k, v in unresolved.items()}, ensure_ascii=False)}

请分别选择与用户品牌相同的品牌。只能返回每个数据源候选列表里的原值，
不能混淆集团、母公司、子品牌或相似名称。
只返回JSON：
{{"mappings":{{"数据源名":{{"brand":"候选列表中的原值","confidence":0.98}}}}}}
"""
    try:
        response = llm_client(max_retries=0).chat.completions.create(
            model=os.environ.get("DASHSCOPE_ROUTER_MODEL", "qwen3.7-plus"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=260,
            timeout=float(os.environ.get("MEDIA_BRAND_LLM_TIMEOUT", "12")),
        )
        parsed = extract_json_object(response.choices[0].message.content or "")
    except Exception as exc:
        log.warning("[media_brand] candidate selection failed for %s: %s", user_brand, exc)
        return {}, {source: list(values[:5]) for source, values in unresolved.items()}, 0.0

    threshold = float(os.environ.get("MEDIA_BRAND_LLM_CONFIDENCE", "0.90"))
    selected: dict[str, str] = {}
    ambiguous: dict[str, list[str]] = {}
    confidences = []
    output = parsed.get("mappings") or {}
    for source, source_candidates in unresolved.items():
        item = output.get(source) or {}
        candidate = _validate_selected_brand(str(item.get("brand") or ""), source_candidates)
        try:
            confidence = float(item.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.0
        if candidate and confidence >= threshold:
            selected[source] = candidate
            confidences.append(confidence)
        else:
            ambiguous[source] = list(source_candidates[:5])
    return selected, ambiguous, min(confidences) if confidences else 0.0


def resolve_media_brand(
    user_brand: str,
    brand_aliases: list[str] | tuple[str, ...] | None = None,
) -> dict:
    user_brand = str(user_brand or "").strip()
    normalized_input = normalize_brand(user_brand)
    if not normalized_input:
        return {"error": "missing_brand", "message": "请提供需要分析的品牌。"}

    cached = _read_resolution_cache(normalized_input)
    if cached:
        return {
            "input": user_brand,
            "resolved": cached.by_source(),
            "match_methods": {source: "cache" for source in SOURCES},
        }

    aliases = _load_aliases()
    alias_entry = aliases.get(user_brand) or aliases.get(normalized_input) or {}
    resolved: dict[str, str | None] = {
        source: str(alias_entry[source]).strip()
        if isinstance(alias_entry, dict) and alias_entry.get(source)
        else None
        for source in SOURCES
    }
    methods = {source: "alias" for source, value in resolved.items() if value}

    supplied_names = [user_brand]
    for value in brand_aliases or ():
        value = str(value or "").strip()
        if value and value not in supplied_names:
            supplied_names.append(value)
    supplied_normalized = tuple(
        dict.fromkeys(normalize_brand(value) for value in supplied_names if normalize_brand(value))
    )

    missing_sources = tuple(source for source in SOURCES if not resolved[source])
    if "tmall" in missing_sources:
        direct_rows = _tmall_brand_index_matches((normalized_input,))
        direct_candidates = _match_brands(direct_rows)
        alias_rows = _tmall_brand_index_matches(supplied_normalized[1:])
        alias_candidates = _match_brands(alias_rows)
        tmall_candidates = direct_candidates
        if len(direct_candidates) > 1 and alias_candidates:
            narrowed = tuple(value for value in direct_candidates if value in alias_candidates)
            if narrowed:
                tmall_candidates = narrowed
        elif not direct_candidates:
            tmall_candidates = alias_candidates
        if len(tmall_candidates) == 1:
            resolved["tmall"] = tmall_candidates[0]
            methods["tmall"] = _tmall_index_method(
                direct_rows if direct_candidates else alias_rows,
                direct=bool(direct_candidates),
            )
            missing_sources = tuple(source for source in SOURCES if not resolved[source])

    exact_rows = _dictionary_rows(supplied_normalized, missing_sources)
    exact_candidates = _group_candidates(exact_rows)
    for source in missing_sources:
        values = exact_candidates.get(source) or ()
        if len(values) == 1:
            resolved[source] = values[0]
            methods[source] = "dictionary_exact"

    missing_sources = tuple(source for source in SOURCES if not resolved[source])
    ambiguous: dict[str, list[str]] = {}
    confidence = 1.0
    if missing_sources:
        variants = (
            tuple(supplied_names)
            if len(supplied_names) > 1
            else _generate_brand_variants(user_brand)
        )
        normalized_variants = tuple(normalize_brand(value) for value in variants)
        candidate_rows = _dictionary_rows(
            normalized_variants,
            missing_sources,
            include_prefix=True,
        )
        grouped = _group_candidates(candidate_rows)
        for source in missing_sources:
            values = grouped.get(source) or ()
            if len(values) == 1:
                resolved[source] = values[0]
                methods[source] = "dictionary_unique_alias"
        missing_sources = tuple(source for source in SOURCES if not resolved[source])
        no_candidates = [source for source in missing_sources if not grouped.get(source)]
        for source in no_candidates:
            methods[source] = "not_found"
        selectable_sources = tuple(
            source for source in missing_sources if source not in no_candidates
        )
        if selectable_sources:
            selected, ambiguous, confidence = _select_source_mappings(
                user_brand,
                {source: grouped[source] for source in selectable_sources},
            )
            for source, value in selected.items():
                resolved[source] = value
                methods[source] = f"llm:{confidence:.2f}"
            for source in ambiguous:
                methods[source] = "ambiguous"

    complete = _complete_mapping(resolved)
    missing_sources = [source for source in SOURCES if not resolved[source]]
    if not any(resolved.values()):
        return {
            "error": "brand_not_found",
            "message": (
                f"品牌“{user_brand}”在BET数据源中均没有可验证品牌值，报告未生成。"
            ),
            "candidates": ambiguous,
            "missing_sources": list(SOURCES),
        }

    # 完整映射才写长期缓存；缺失来源可能在以后月度数据更新时出现，不能负缓存。
    if complete:
        _write_resolution_cache(
            normalized_input,
            user_brand,
            complete,
            confidence=confidence or 1.0,
        )
    return {
        "input": user_brand,
        "resolved": complete.by_source() if complete else dict(resolved),
        "match_methods": {
            source: methods.get(source, "not_found") for source in SOURCES
        },
        "missing_sources": missing_sources,
        "candidates": ambiguous,
    }


def resolve_source_brand(
    user_brand: str,
    source: str,
    brand_aliases: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """Resolve one source independently without requiring a complete BET mapping."""
    user_brand = str(user_brand or "").strip()
    if source not in LOOKUP_SOURCES:
        return {"error": "invalid_source", "message": f"不支持品牌源：{source}"}
    normalized_input = normalize_brand(user_brand)
    if not normalized_input:
        return {"error": "missing_brand", "message": "请提供需要分析的品牌。"}

    supplied = [user_brand]
    for value in brand_aliases or ():
        value = str(value or "").strip()
        if value and value not in supplied:
            supplied.append(value)

    cache_key = (source, normalized_input)
    source_cached = _read_source_resolution_cache(normalized_input, source)

    if source == "tmall":
        direct_rows = _tmall_brand_index_matches((normalized_input,))
        direct_candidates = _match_brands(direct_rows)
        alias_values = tuple(
            dict.fromkeys(
                normalize_brand(value)
                for value in supplied[1:]
                if normalize_brand(value)
            )
        )
        alias_rows = _tmall_brand_index_matches(alias_values) if alias_values else ()
        alias_candidates = _match_brands(alias_rows)

        candidates = direct_candidates
        matching_rows = direct_rows
        direct_match = True
        if len(direct_candidates) > 1 and alias_candidates:
            narrowed = tuple(value for value in direct_candidates if value in alias_candidates)
            if narrowed:
                candidates = narrowed
        elif not direct_candidates and alias_candidates:
            candidates = alias_candidates
            matching_rows = alias_rows
            direct_match = False

        if len(candidates) == 1:
            in_memory = _SOURCE_RESOLUTION_CACHE.get(cache_key) or {}
            if in_memory.get("brand") == candidates[0]:
                return dict(in_memory)
            return _source_resolution_success(
                user_brand=user_brand,
                normalized_input=normalized_input,
                source=source,
                source_brand=candidates[0],
                match_method=_tmall_index_method(matching_rows, direct=direct_match),
            )

        if candidates:
            cached_brand = str(source_cached.get("source_brand") or "")
            if cached_brand in candidates:
                result = {
                    "input": user_brand,
                    "source": source,
                    "brand": cached_brand,
                    "match_method": "source_cache_validated",
                }
                _SOURCE_RESOLUTION_CACHE[cache_key] = result
                return dict(result)
            selected, ambiguous, confidence = _select_source_mappings(
                user_brand,
                {source: candidates},
            )
            if selected.get(source):
                return _source_resolution_success(
                    user_brand=user_brand,
                    normalized_input=normalized_input,
                    source=source,
                    source_brand=selected[source],
                    match_method=f"tmall_index_llm:{confidence:.2f}",
                    confidence=confidence,
                )
            return {
                "error": "ambiguous_brand",
                "message": f"品牌“{user_brand}”对应多个天猫品牌值，请确认具体品牌。",
                "source": source,
                "candidates": ambiguous.get(source, list(candidates[:5])),
            }

    if cache_key in _SOURCE_RESOLUTION_CACHE:
        return dict(_SOURCE_RESOLUTION_CACHE[cache_key])

    if source_cached.get("source_brand"):
        result = {
            "input": user_brand,
            "source": source,
            "brand": str(source_cached["source_brand"]),
            "match_method": "source_cache",
        }
        _SOURCE_RESOLUTION_CACHE[cache_key] = result
        return dict(result)

    try:
        cached_row = fetch_one(
            f"SELECT {source}_brand AS source_brand "
            "FROM ai_bot_brand_resolution_cache "
            "WHERE normalized_input = :normalized_input",
            {"normalized_input": normalized_input},
        )
    except Exception:
        cached_row = {}
    if cached_row.get("source_brand"):
        return _source_resolution_success(
            user_brand=user_brand,
            normalized_input=normalized_input,
            source=source,
            source_brand=str(cached_row["source_brand"]),
            match_method="cache",
        )

    aliases = _load_aliases()
    alias_entry = aliases.get(user_brand) or aliases.get(normalized_input) or {}
    if isinstance(alias_entry, dict) and alias_entry.get(source):
        return _source_resolution_success(
            user_brand=user_brand,
            normalized_input=normalized_input,
            source=source,
            source_brand=str(alias_entry[source]).strip(),
            match_method="alias",
        )

    normalized_values = tuple(
        dict.fromkeys(normalize_brand(value) for value in supplied if normalize_brand(value))
    )
    exact = _group_candidates(_dictionary_rows(normalized_values, (source,))).get(source) or ()
    if len(exact) == 1:
        return _source_resolution_success(
            user_brand=user_brand,
            normalized_input=normalized_input,
            source=source,
            source_brand=exact[0],
            match_method="dictionary_exact",
        )
    if len(exact) > 1:
        return {
            "error": "ambiguous_brand",
            "message": f"品牌“{user_brand}”有多个可用候选，请确认具体品牌。",
            "source": source,
            "candidates": list(exact[:5]),
        }

    variants = tuple(supplied) if len(supplied) > 1 else _generate_brand_variants(user_brand)
    variant_values = tuple(normalize_brand(value) for value in variants if normalize_brand(value))
    candidates = _group_candidates(
        _dictionary_rows(variant_values, (source,), include_prefix=True)
    ).get(source) or ()
    match_method = "dictionary_unique_alias"
    if not candidates and source == "tmall":
        try:
            candidates = _tmall_exact_fact_candidates(variants)
            match_method = "fact_exact_alias"
        except Exception as exc:
            log.warning("[media_brand] tmall exact fallback failed for %s: %s", user_brand, exc)
    if len(candidates) == 1:
        return _source_resolution_success(
            user_brand=user_brand,
            normalized_input=normalized_input,
            source=source,
            source_brand=candidates[0],
            match_method=match_method,
        )
    if candidates:
        selected, ambiguous, confidence = _select_source_mappings(
            user_brand,
            {source: candidates},
        )
        if selected.get(source):
            return _source_resolution_success(
                user_brand=user_brand,
                normalized_input=normalized_input,
                source=source,
                source_brand=selected[source],
                match_method=f"llm:{confidence:.2f}",
                confidence=confidence,
            )
        return {
            "error": "ambiguous_brand",
            "message": f"品牌“{user_brand}”有多个可用候选，请确认具体品牌。",
            "source": source,
            "candidates": ambiguous.get(source, list(candidates[:5])),
        }
    source_labels = {
        "search": "Social Search",
        "topline": "Topline",
        "ksi": "KSI",
        "tmall": "天猫商品链接",
        "dy": "抖音商品链接",
        "nso": "EC Consolidation",
    }
    return {
        "error": "brand_not_found",
        "message": (
            f"品牌“{user_brand}”在{source_labels.get(source, source)}数据中"
            "没有对应品牌值。"
        ),
        "source": source,
        "candidates": [],
    }


def latest_common_month(resolved: dict[str, str | None]) -> str | None:
    queries = {
        "search": """
            SELECT DISTINCT DATE_FORMAT(report_month, '%%Y-%%m-01') AS month_value
            FROM ai_bot_media_search_index
            WHERE report_year = 2026 AND brand = :brand
        """,
        "topline": """
            SELECT DISTINCT DATE_FORMAT(period_month, '%%Y-%%m-01') AS month_value
            FROM ai_bot_media_topline_investment
            WHERE year = 2026 AND brand_r = :brand
        """,
        "ksi": """
            SELECT DISTINCT DATE_FORMAT(period_month, '%%Y-%%m-01') AS month_value
            FROM ai_bot_media_ksi_performance
            WHERE year = 2026 AND brand = :brand
        """,
        "nso": """
            SELECT DISTINCT CONCAT(year, '-', LPAD(month, 2, '0'), '-01') AS month_value
            FROM top_brands_total_ec
            WHERE year = 2026 AND platform = 'TTL' AND Brand = :brand
        """,
    }
    common: set[str] | None = None
    for source, sql in queries.items():
        brand = resolved.get(source)
        if not brand:
            continue
        df = fetch_df(sql, {"brand": brand})
        values = {
            str(value)[:10]
            for value in (df["month_value"].dropna().tolist() if not df.empty else [])
        }
        common = values if common is None else common & values
        if not common:
            return None
    return max(common) if common else None
