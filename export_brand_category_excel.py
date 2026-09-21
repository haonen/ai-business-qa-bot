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
SELECT DISTINCT
  brand_name AS 品牌,
  brand_category AS 品类
FROM sku_sales
WHERE brand_name IS NOT NULL
  AND brand_name <> ''
  AND brand_category IS NOT NULL
  AND brand_category <> ''
ORDER BY 品类, 品牌;
"""

df = pd.read_sql(text(sql), engine)

out_path = BASE_DIR / "brand_category_list.xlsx"
df.to_excel(out_path, index=False)

print(f"exported rows: {len(df)}")
print(f"saved to: {out_path}")
