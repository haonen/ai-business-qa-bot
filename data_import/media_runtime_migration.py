from __future__ import annotations

from pathlib import Path

from bot.db.connection import fetch_one, get_engine


SQL_DIR = Path(__file__).resolve().parent / "sql"


def _execute(sql: str):
    from sqlalchemy import text

    with get_engine().begin() as conn:
        conn.execute(text(sql))


def _ensure_table(filename: str):
    sql = (SQL_DIR / filename).read_text(encoding="utf-8").strip().rstrip(";")
    _execute(sql)
    print(f"table ready: {filename}")


def _index_exists(table_name: str, index_name: str) -> bool:
    row = fetch_one(
        """
        SELECT COUNT(*) AS count_value
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = :table_name
          AND index_name = :index_name
        """,
        {"table_name": table_name, "index_name": index_name},
    )
    return bool(row.get("count_value"))


def _ensure_index(table_name: str, index_name: str, columns: str):
    if _index_exists(table_name, index_name):
        print(f"index exists: {table_name}.{index_name}")
        return
    print(f"creating index: {table_name}.{index_name} ({columns})")
    _execute(f"ALTER TABLE {table_name} ADD INDEX {index_name} ({columns})")
    print(f"index ready: {table_name}.{index_name}")


def _column_exists(table_name: str, column_name: str) -> bool:
    row = fetch_one(
        """
        SELECT COUNT(*) AS count_value
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = :table_name
          AND column_name = :column_name
        """,
        {"table_name": table_name, "column_name": column_name},
    )
    return bool(row.get("count_value"))


def _ensure_column(table_name: str, column_name: str, definition: str):
    if _column_exists(table_name, column_name):
        print(f"column exists: {table_name}.{column_name}")
        return
    print(f"creating column: {table_name}.{column_name}")
    _execute(f"ALTER TABLE {table_name} ADD COLUMN `{column_name}` {definition}")
    print(f"column ready: {table_name}.{column_name}")


def main():
    _ensure_table("brand_dictionary.sql")
    _ensure_table("brand_resolution_cache.sql")
    _ensure_table("source_brand_resolution_cache.sql")
    _ensure_table("tmall_brand_index.sql")
    _ensure_column(
        "ai_bot_brand_resolution_cache",
        "dy_brand",
        "VARCHAR(255) NULL AFTER tmall_brand",
    )
    _ensure_index(
        "ai_bot_brand_dictionary",
        "idx_brand_dictionary_normalized",
        "normalized_brand, source_name",
    )
    _ensure_index(
        "ai_bot_tmall_product_link",
        "idx_tmall_brand_date",
        "brand_name, bus_date",
    )
    _ensure_index(
        "ai_bot_dy_product_link",
        "idx_dy_brand_date",
        "`商品品牌`, `业务日期`",
    )
    _ensure_index(
        "top_brands_total_ec",
        "idx_nso_brand_platform_period",
        "Brand, platform, year, month",
    )
    _ensure_index(
        "tmall_store_ranking_day_jiashicang",
        "idx_ecip_tmall_brand_date_category",
        "brand_name, bus_date, category_EN_level_1",
    )
    _ensure_index(
        "three_platform_store_rank_monthly",
        "idx_ecip_monthly_brand_date_platform",
        "brand_name(100), bus_date, platform(16)",
    )
    _ensure_index(
        "three_platforms_segmented_markets_monthly",
        "idx_market_monthly_scope",
        "global_segment(50), category_EN(50), platform(16), bus_date",
    )
    _ensure_index(
        "three_platforms_segmented_markets_daily",
        "idx_market_daily_scope",
        "global_segment(50), category_EN(50), platform(16), bus_date",
    )
    _ensure_index(
        "three_platform_store_rank_monthly",
        "idx_market_brand_monthly_scope",
        "SELECTIVITY(50), platform(16), bus_date, brand_name(100), category_EN_level_1(50)",
    )
    _ensure_index(
        "tmall_store_ranking_day_jiashicang",
        "idx_market_brand_daily_scope",
        "SELECTIVITY(50), bus_date, brand_name(100), category_EN_level_1(50)",
    )


if __name__ == "__main__":
    main()
