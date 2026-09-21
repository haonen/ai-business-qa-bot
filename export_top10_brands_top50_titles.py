from pathlib import Path
import os

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

BASE_DIR = Path("/root/ai-business-qa-bot")
load_dotenv(BASE_DIR / ".env")

url = URL.create(
    "mysql+pymysql",
    username=os.environ["MYSQL_USER"],
    password=os.environ["MYSQL_PASSWORD"],
    host=os.environ["MYSQL_HOST"],
    port=int(os.environ.get("MYSQL_PORT", "3306")),
    database=os.environ["MYSQL_DATABASE"],
    query={"charset": "utf8mb4"},
)

engine = create_engine(url, pool_pre_ping=True)

sql = """
WITH brand_gmv AS (
    SELECT
        brand_category,
        brand_name,
        SUM(gmv) AS total_gmv
    FROM sku_sales
    WHERE year = 2026
      AND brand_category IS NOT NULL AND brand_category <> ''
      AND brand_name IS NOT NULL AND brand_name <> ''
    GROUP BY brand_category, brand_name
),
top10_brands AS (
    SELECT brand_category, brand_name, total_gmv
    FROM (
        SELECT
            brand_category,
            brand_name,
            total_gmv,
            ROW_NUMBER() OVER (
                PARTITION BY brand_category
                ORDER BY total_gmv DESC
            ) AS brand_rank
        FROM brand_gmv
    ) ranked
    WHERE brand_rank <= 10
),
title_gmv AS (
    SELECT
        s.brand_category,
        s.brand_name,
        s.product_title,
        SUM(s.gmv) AS title_gmv
    FROM sku_sales s
    JOIN top10_brands t
      ON s.brand_category = t.brand_category
     AND s.brand_name = t.brand_name
    WHERE s.year = 2026
      AND s.product_title IS NOT NULL AND s.product_title <> ''
    GROUP BY s.brand_category, s.brand_name, s.product_title
)
SELECT
    brand_category,
    brand_name,
    product_title,
    title_gmv,
    title_rank
FROM (
    SELECT
        brand_category,
        brand_name,
        product_title,
        title_gmv,
        ROW_NUMBER() OVER (
            PARTITION BY brand_category, brand_name
            ORDER BY title_gmv DESC
        ) AS title_rank
    FROM title_gmv
) ranked
WHERE title_rank <= 50
ORDER BY brand_category, brand_name, title_rank
"""

df = pd.read_sql(text(sql), engine)
print(f"exported rows: {len(df)}")
print(f"brands covered: {df.groupby('brand_category')['brand_name'].nunique().to_dict()}")

out_path = BASE_DIR / "top10_brands_top50_titles.xlsx"
df.to_excel(out_path, index=False)
print(f"saved to: {out_path}")
