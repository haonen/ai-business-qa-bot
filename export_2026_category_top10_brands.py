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
WITH brand_store_gmv AS (
  SELECT
    brand_category,
    brand_name,
    store_cn,
    SUM(gmv) AS store_gmv
  FROM sku_sales
  WHERE year = 2026
    AND brand_category IS NOT NULL
    AND brand_category <> ''
    AND brand_name IS NOT NULL
    AND brand_name <> ''
  GROUP BY brand_category, brand_name, store_cn
),
brand_gmv AS (
  SELECT
    brand_category,
    brand_name,
    SUM(store_gmv) AS gmv
  FROM brand_store_gmv
  GROUP BY brand_category, brand_name
),
brand_cn AS (
  SELECT brand_category, brand_name, store_cn AS brand_cn
  FROM (
    SELECT
      brand_category,
      brand_name,
      store_cn,
      store_gmv,
      ROW_NUMBER() OVER (
        PARTITION BY brand_category, brand_name
        ORDER BY store_gmv DESC
      ) AS rn
    FROM brand_store_gmv
    WHERE store_cn IS NOT NULL
      AND store_cn <> ''
  ) t
  WHERE rn = 1
),
ranked AS (
  SELECT
    b.brand_category,
    b.brand_name,
    COALESCE(c.brand_cn, '') AS brand_cn,
    b.gmv,
    ROW_NUMBER() OVER (
      PARTITION BY b.brand_category
      ORDER BY b.gmv DESC
    ) AS rank_no
  FROM brand_gmv b
  LEFT JOIN brand_cn c
    ON b.brand_category = c.brand_category
   AND b.brand_name = c.brand_name
)
SELECT
  brand_category AS 品类,
  rank_no AS 排名,
  brand_name AS 品牌,
  brand_cn AS 品牌中文名,
  ROUND(gmv, 2) AS GMV
FROM ranked
WHERE rank_no <= 10
ORDER BY 品类, 排名;
"""

df = pd.read_sql(text(sql), engine)

out_path = BASE_DIR / "brand_category_2026_top10.xlsx"
df.to_excel(out_path, index=False)

print(f"exported rows: {len(df)}")
print(f"saved to: {out_path}")
