from __future__ import annotations

import argparse
from pathlib import Path

from bot.db.connection import fetch_df, get_engine
from bot.media_brand import normalize_brand


SOURCE_COLUMNS = {
    "search": ("ai_bot_media_search_index", "brand"),
    "topline": ("ai_bot_media_topline_investment", "brand_r"),
    "ksi": ("ai_bot_media_ksi_performance", "brand"),
    "tmall": ("ai_bot_tmall_product_link", "brand_name"),
    "dy": ("ai_bot_dy_product_link", "`商品品牌`"),
    "nso": ("top_brands_total_ec", "Brand"),
}
SCHEMA_PATH = Path(__file__).resolve().parent / "sql" / "brand_dictionary.sql"


def ensure_table():
    from sqlalchemy import text

    ddl = SCHEMA_PATH.read_text(encoding="utf-8").strip().rstrip(";")
    with get_engine().begin() as conn:
        conn.execute(text(ddl))


def refresh_source(source_name: str, chunk_size: int = 500) -> int:
    from sqlalchemy import text

    table, column = SOURCE_COLUMNS[source_name]
    df = fetch_df(
        f"""
        SELECT DISTINCT TRIM({column}) AS source_brand
        FROM {table}
        WHERE {column} IS NOT NULL AND TRIM({column}) <> ''
        """
    )
    rows = []
    for value in df.get("source_brand", []):
        source_brand = str(value).strip()
        normalized = normalize_brand(source_brand)
        if source_brand and normalized:
            rows.append({
                "source_name": source_name,
                "source_brand": source_brand,
                "normalized_brand": normalized,
            })
    statement = text(
        """
        INSERT INTO ai_bot_brand_dictionary (
            source_name, source_brand, normalized_brand
        ) VALUES (
            :source_name, :source_brand, :normalized_brand
        )
        ON DUPLICATE KEY UPDATE
            normalized_brand = VALUES(normalized_brand),
            last_seen_at = CURRENT_TIMESTAMP
        """
    )
    with get_engine().begin() as conn:
        for start in range(0, len(rows), chunk_size):
            conn.execute(statement, rows[start:start + chunk_size])
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description="Refresh BET brand lookup dictionary.")
    parser.add_argument(
        "--source",
        choices=["all", *SOURCE_COLUMNS],
        default="all",
        help="Refresh one source or all sources.",
    )
    args = parser.parse_args()
    ensure_table()
    sources = list(SOURCE_COLUMNS) if args.source == "all" else [args.source]
    for source in sources:
        count = refresh_source(source)
        print(f"{source}: refreshed {count} distinct brands")


if __name__ == "__main__":
    main()
