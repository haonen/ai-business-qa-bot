from pathlib import Path
import os

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL


BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = Path(__file__).resolve().parent / "data"

load_dotenv(BASE_DIR / ".env")


def get_engine():
    url = URL.create(
        "mysql+pymysql",
        username=os.environ["MYSQL_USER"],
        password=os.environ["MYSQL_PASSWORD"],
        host=os.environ["MYSQL_HOST"],
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        database=os.environ["MYSQL_DATABASE"],
        query={"charset": "utf8mb4"},
    )
    return create_engine(url, pool_pre_ping=True)


SKU_COLUMN_MAP = {
    "bus_date": "bus_date",
    "year": "year",
    "brand_category": "brand_category",
    "brand_name": "brand_name",
    "store_CN": "store_cn",
    "category_CN": "category_cn",
    "category_level_1": "category_level_1",
    "category_level_2": "category_level_2",
    "category_level_3": "category_level_3",
    "item_id": "item_id",
    "product_title": "product_title",
    "gmv": "gmv",
    "unit": "unit",
    "kol_driver": "kol_driver",
    "link_type": "link_type",
}

KOL_COLUMN_MAP = {
    "直播开始日期": "live_start_date",
    "year": "year",
    "brand_category": "brand_category",
    "店铺": "shop_name",
    "关联主播": "host_name",
    "主播类型": "host_type",
    "主播等级": "host_level",
    "品牌": "brand",
    "宝贝标题": "product_title",
    "直播销售额": "live_sales_amount",
    "直播销量": "live_sales_unit",
    "kol_type": "kol_type",
}


def read_parquet(path, column_map, date_cols, numeric_cols):
    df = pd.read_parquet(path)
    keep = [col for col in column_map if col in df.columns]
    missing = [col for col in column_map if col not in df.columns]
    if missing:
        print(f"warning: {path.name} missing columns: {missing}")

    df = df[keep].rename(columns=column_map)

    for col in date_cols:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce").dt.date

    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df.where(pd.notnull(df), None)


def create_tables(engine):
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS sku_sales"))
        conn.execute(text("DROP TABLE IF EXISTS kol_live_sales"))

        conn.execute(text("""
            CREATE TABLE sku_sales (
                bus_date DATE,
                year INT,
                brand_category VARCHAR(50),
                brand_name VARCHAR(255),
                store_cn VARCHAR(255),
                category_cn VARCHAR(500),
                category_level_1 VARCHAR(255),
                category_level_2 VARCHAR(255),
                category_level_3 VARCHAR(255),
                item_id VARCHAR(100),
                product_title TEXT,
                gmv DECIMAL(18,2),
                unit DECIMAL(18,2),
                kol_driver VARCHAR(50),
                link_type VARCHAR(100)
            ) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))

        conn.execute(text("""
            CREATE TABLE kol_live_sales (
                live_start_date DATE,
                year INT,
                brand_category VARCHAR(50),
                shop_name VARCHAR(255),
                host_name VARCHAR(255),
                host_type VARCHAR(100),
                host_level VARCHAR(100),
                brand VARCHAR(255),
                product_title TEXT,
                live_sales_amount DECIMAL(18,2),
                live_sales_unit DECIMAL(18,2),
                kol_type VARCHAR(50)
            ) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))


def insert_in_chunks(df, table_name, engine, chunk_size=2000):
    total = len(df)
    for start in range(0, total, chunk_size):
        end = min(start + chunk_size, total)
        df.iloc[start:end].to_sql(
            table_name,
            engine,
            if_exists="append",
            index=False,
        )
        print(f"{table_name}: inserted {end:,}/{total:,}")


def create_indexes(engine):
    statements = [
        "CREATE INDEX idx_sku_year ON sku_sales(year)",
        "CREATE INDEX idx_sku_date ON sku_sales(bus_date)",
        "CREATE INDEX idx_sku_brand ON sku_sales(brand_name)",
        "CREATE INDEX idx_sku_store ON sku_sales(store_cn)",
        "CREATE INDEX idx_sku_category ON sku_sales(category_cn(191))",
        "CREATE INDEX idx_sku_driver ON sku_sales(kol_driver)",
        "CREATE INDEX idx_sku_link_type ON sku_sales(link_type)",
        "CREATE INDEX idx_sku_item ON sku_sales(item_id)",
        "CREATE INDEX idx_kol_year ON kol_live_sales(year)",
        "CREATE INDEX idx_kol_date ON kol_live_sales(live_start_date)",
        "CREATE INDEX idx_kol_brand ON kol_live_sales(brand)",
        "CREATE INDEX idx_kol_shop ON kol_live_sales(shop_name)",
        "CREATE INDEX idx_kol_type ON kol_live_sales(kol_type)",
    ]
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


def print_validation(engine):
    queries = {
        "sku_rows": "SELECT COUNT(*) AS value FROM sku_sales",
        "kol_rows": "SELECT COUNT(*) AS value FROM kol_live_sales",
        "sku_gmv_by_year": """
            SELECT year, ROUND(SUM(gmv), 2) AS gmv
            FROM sku_sales
            GROUP BY year
            ORDER BY year
        """,
        "kol_sales_by_year": """
            SELECT year, ROUND(SUM(live_sales_amount), 2) AS live_sales_amount
            FROM kol_live_sales
            GROUP BY year
            ORDER BY year
        """,
    }

    with engine.connect() as conn:
        for name, sql in queries.items():
            print(f"\n[{name}]")
            rows = conn.execute(text(sql)).mappings().all()
            for row in rows:
                print(dict(row))


def main():
    sku_path = DATA_DIR / "sku_all.parquet"
    kol_path = DATA_DIR / "kol_all.parquet"

    if not sku_path.exists() or not kol_path.exists():
        raise FileNotFoundError(
            "Expected data/sku_all.parquet and data/kol_all.parquet under data_import."
        )

    engine = get_engine()

    sku = read_parquet(
        sku_path,
        SKU_COLUMN_MAP,
        date_cols=["bus_date"],
        numeric_cols=["year", "gmv", "unit"],
    )
    kol = read_parquet(
        kol_path,
        KOL_COLUMN_MAP,
        date_cols=["live_start_date"],
        numeric_cols=["year", "live_sales_amount", "live_sales_unit"],
    )

    print(f"sku rows ready: {len(sku):,}")
    print(f"kol rows ready: {len(kol):,}")

    create_tables(engine)
    insert_in_chunks(sku, "sku_sales", engine)
    insert_in_chunks(kol, "kol_live_sales", engine)
    create_indexes(engine)
    print_validation(engine)
    print("\nimport completed")


if __name__ == "__main__":
    main()

