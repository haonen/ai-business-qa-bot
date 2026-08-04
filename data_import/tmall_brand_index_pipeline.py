from __future__ import annotations

import argparse
from pathlib import Path
import re

from bot.db.connection import fetch_df, get_engine
from bot.media_brand import normalize_brand
from data_import.brand_dictionary_pipeline import (
    ensure_table as ensure_brand_dictionary_table,
    refresh_source as refresh_brand_dictionary_source,
)


SCHEMA_PATH = Path(__file__).resolve().parent / "sql" / "tmall_brand_index.sql"
_ENGLISH_STORE_SUFFIX = re.compile(
    r"\b(?:official\s+)?(?:flagship\s+)?(?:online\s+)?store\b$",
    flags=re.IGNORECASE,
)
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_LATIN = re.compile(r"[A-Za-z0-9]+")


def ensure_table():
    from sqlalchemy import text

    ddl = SCHEMA_PATH.read_text(encoding="utf-8").strip().rstrip(";")
    with get_engine().begin() as conn:
        conn.execute(text(ddl))


def _clean_store_value(value: str) -> str:
    text = str(value or "").strip()
    return _ENGLISH_STORE_SUFFIX.sub("", text).strip(" -_()（）")


def alias_variants(value: str, *, store_name: bool = False) -> tuple[str, ...]:
    """Return normalized full/CJK/Latin variants for one real database value."""
    raw = _clean_store_value(value) if store_name else str(value or "").strip()
    candidates = [raw]
    if store_name:
        candidates.extend(("".join(_CJK.findall(raw)), "".join(_LATIN.findall(raw))))
    output = []
    seen = set()
    for candidate in candidates:
        normalized = normalize_brand(candidate)
        if normalized and len(normalized) <= 191 and normalized not in seen:
            output.append(normalized)
            seen.add(normalized)
    return tuple(output)


def _index_rows_for_fact_rows(fact_rows: list[dict]) -> list[dict]:
    aggregated: dict[tuple[str, str, str], dict] = {}
    for row in fact_rows:
        source_brand = str(row.get("brand_name") or "").strip()
        if not source_brand:
            continue
        try:
            count = max(int(row.get("observation_count") or 0), 0)
        except (TypeError, ValueError):
            count = 0
        values = (
            ("brand_name", source_brand, False),
            ("store_cn", str(row.get("store_cn") or "").strip(), True),
            ("store_en", str(row.get("store_en") or "").strip(), True),
        )
        for alias_source, alias_value, store_name in values:
            if not alias_value:
                continue
            for normalized in alias_variants(alias_value, store_name=store_name):
                key = (normalized, source_brand, alias_source)
                item = aggregated.setdefault(key, {
                    "normalized_alias": normalized,
                    "source_brand": source_brand,
                    "alias_value": alias_value[:255],
                    "alias_source": alias_source,
                    "observation_count": 0,
                })
                item["observation_count"] += count
    return list(aggregated.values())


def _tmall_brands(start_after: str | None = None, limit: int | None = None) -> list[str]:
    params = {}
    clause = ""
    if start_after:
        clause = "AND source_brand > :start_after"
        params["start_after"] = start_after
    limit_clause = f"LIMIT {max(int(limit), 1)}" if limit else ""
    df = fetch_df(
        f"""
        SELECT source_brand
        FROM ai_bot_brand_dictionary
        WHERE source_name = 'tmall'
          {clause}
        ORDER BY source_brand
        {limit_clause}
        """,
        params,
    )
    return [str(value).strip() for value in df.get("source_brand", []) if str(value).strip()]


def _fact_rows(brands: list[str]) -> list[dict]:
    if not brands:
        return []
    params = {f"brand_{index}": brand for index, brand in enumerate(brands)}
    placeholders = ", ".join(f":brand_{index}" for index in range(len(brands)))
    df = fetch_df(
        f"""
        SELECT
          brand_name,
          store_CN AS store_cn,
          store_EN AS store_en,
          COUNT(*) AS observation_count
        FROM ai_bot_tmall_product_link FORCE INDEX (idx_tmall_brand_date)
        WHERE brand_name IN ({placeholders})
        GROUP BY brand_name, store_CN, store_EN
        """,
        params,
    )
    return df.to_dict(orient="records") if not df.empty else []


def _replace_brand_rows(brands: list[str], rows: list[dict]):
    from sqlalchemy import bindparam, text

    delete_statement = text(
        "DELETE FROM ai_bot_tmall_brand_index WHERE source_brand IN :brands"
    ).bindparams(bindparam("brands", expanding=True))
    insert_statement = text(
        """
        INSERT INTO ai_bot_tmall_brand_index (
            normalized_alias, source_brand, alias_value, alias_source,
            observation_count
        ) VALUES (
            :normalized_alias, :source_brand, :alias_value, :alias_source,
            :observation_count
        )
        ON DUPLICATE KEY UPDATE
            alias_value = VALUES(alias_value),
            observation_count = VALUES(observation_count),
            last_seen_at = CURRENT_TIMESTAMP
        """
    )
    with get_engine().begin() as conn:
        conn.execute(delete_statement, {"brands": brands})
        if rows:
            conn.execute(insert_statement, rows)


def _prune_invalid_tmall_caches():
    """Remove only cache rows that conflict with the rebuilt real-value index."""
    from sqlalchemy import text

    statements = (
        """
        DELETE cache
        FROM ai_bot_source_brand_resolution_cache AS cache
        LEFT JOIN ai_bot_tmall_brand_index AS idx
          ON idx.normalized_alias = cache.normalized_input
         AND idx.source_brand = cache.source_brand
        WHERE cache.source_name = 'tmall'
          AND idx.source_brand IS NULL
        """,
        """
        DELETE cache
        FROM ai_bot_brand_resolution_cache AS cache
        LEFT JOIN ai_bot_tmall_brand_index AS idx
          ON idx.normalized_alias = cache.normalized_input
         AND idx.source_brand = cache.tmall_brand
        WHERE idx.source_brand IS NULL
        """,
    )
    with get_engine().begin() as conn:
        for statement in statements:
            try:
                conn.execute(text(statement))
            except Exception:
                # Cache tables may not exist during a partial first deployment.
                pass


def refresh_index(
    *,
    brand_batch_size: int = 25,
    start_after: str | None = None,
    limit_brands: int | None = None,
) -> tuple[int, int]:
    brands = _tmall_brands(start_after=start_after, limit=limit_brands)
    total_alias_rows = 0
    for start in range(0, len(brands), brand_batch_size):
        batch = brands[start:start + brand_batch_size]
        rows = _index_rows_for_fact_rows(_fact_rows(batch))
        _replace_brand_rows(batch, rows)
        total_alias_rows += len(rows)
        print(
            f"tmall brand index: processed {min(start + len(batch), len(brands)):,}/"
            f"{len(brands):,} brands; aliases={total_alias_rows:,}; last={batch[-1]}"
        )
    _prune_invalid_tmall_caches()
    return len(brands), total_alias_rows


def main():
    parser = argparse.ArgumentParser(
        description="Build the Tmall store/brand alias index used by EC brand resolution."
    )
    parser.add_argument("--batch-size", type=int, default=25)
    parser.add_argument("--start-after", help="Resume after this source_brand value.")
    parser.add_argument("--limit-brands", type=int, help="Only process N brands (smoke test).")
    parser.add_argument(
        "--skip-brand-refresh",
        action="store_true",
        help="Use the current tmall entries in ai_bot_brand_dictionary.",
    )
    args = parser.parse_args()
    if args.batch_size < 1 or args.batch_size > 200:
        parser.error("--batch-size must be between 1 and 200")

    ensure_table()
    ensure_brand_dictionary_table()
    if not args.skip_brand_refresh:
        count = refresh_brand_dictionary_source("tmall")
        print(f"tmall dictionary: refreshed {count:,} brands")
    brands, aliases = refresh_index(
        brand_batch_size=args.batch_size,
        start_after=args.start_after,
        limit_brands=args.limit_brands,
    )
    print(f"tmall brand index ready: brands={brands:,}, aliases={aliases:,}")


if __name__ == "__main__":
    main()
