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
    brand,
    SUM(live_sales_amount) AS live_sales_amount
  FROM kol_live_sales
  WHERE year = 2026
    AND brand_category IS NOT NULL
    AND brand_category <> ''
    AND brand IS NOT NULL
    AND brand <> ''
  GROUP BY brand_category, brand
),
ranked AS (
  SELECT
    brand_category,
    brand,
    live_sales_amount,
    ROW_NUMBER() OVER (
      PARTITION BY brand_category
      ORDER BY live_sales_amount DESC
    ) AS rank_no
  FROM brand_gmv
)
SELECT
  brand_category AS 品类,
  rank_no AS 排名,
  brand AS 品牌,
  brand AS 品牌中文名,
  ROUND(live_sales_amount, 2) AS KOL直播销售额
FROM ranked
WHERE rank_no <= 10
ORDER BY 品类, 排名;
"""

df = pd.read_sql(text(sql), engine)

out_path = BASE_DIR / "kol_category_2026_top10_brands.xlsx"
df.to_excel(out_path, index=False)

print(f"exported rows: {len(df)}")
print(f"saved to: {out_path}")
