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

# ── Step 1: kol_live_sales 各品类KOL销售额Top10品牌 ──
kol_rank_sql = """
SELECT brand_category, brand, kol_gmv
FROM (
    SELECT
        brand_category,
        brand,
        SUM(live_sales_amount) AS kol_gmv,
        ROW_NUMBER() OVER (
            PARTITION BY brand_category
            ORDER BY SUM(live_sales_amount) DESC
        ) AS brand_rank
    FROM kol_live_sales
    WHERE year = 2026
      AND brand_category IS NOT NULL AND brand_category <> ''
      AND brand IS NOT NULL AND brand <> ''
    GROUP BY brand_category, brand
) ranked
WHERE brand_rank <= 10
ORDER BY brand_category, kol_gmv DESC
"""

kol_top = pd.read_sql(text(kol_rank_sql), engine)
print("=== KOL Top10 brands per category (kol_live_sales) ===")
for cat, grp in kol_top.groupby("brand_category"):
    print(f"\n[{cat}]")
    for _, row in grp.iterrows():
        print(f"  {row['brand']}  ({row['kol_gmv']/10000:.0f}万)")

kol_brand_names = kol_top["brand"].unique().tolist()
forced = ["韩束", "林清轩", "自然堂"]

# ── Step 2: 品牌名模糊匹配回 sku_sales ──
with engine.connect() as conn:
    sku_brands = pd.read_sql(
        text("""
            SELECT DISTINCT brand_name
            FROM sku_sales
            WHERE year = 2026
              AND brand_name IS NOT NULL AND brand_name <> ''
        """),
        conn,
    )["brand_name"].tolist()

def match_sku_brand(target: str, candidates: list[str]) -> list[str]:
    t = target.strip().lower()
    hits = []
    for c in candidates:
        cl = c.strip().lower()
        if t in cl or cl in t:
            hits.append(c)
            continue
        for part in t.replace("／", "/").split("/"):
            p = part.strip()
            if p and (p in cl or cl in p):
                hits.append(c)
                break
    return list(dict.fromkeys(hits))

matched: dict[str, list[str]] = {}
unmatched: list[str] = []
for b in kol_brand_names + forced:
    hits = match_sku_brand(b, sku_brands)
    if hits:
        matched[b] = hits
    else:
        unmatched.append(b)

print("\n=== brand mapping: kol_live_sales.brand -> sku_sales.brand_name ===")
for k, v in matched.items():
    print(f"  {k}  ->  {v}")
if unmatched:
    print(f"\n⚠️ NOT matched in sku_sales: {unmatched}")

all_sku_brand_names = sorted({name for hits in matched.values() for name in hits})

# ── Step 3: 每品牌 GMV Top50 链接标题 ──
placeholder = ", ".join([f":b{i}" for i in range(len(all_sku_brand_names))])
params = {f"b{i}": n for i, n in enumerate(all_sku_brand_names)}

title_sql = f"""
SELECT brand_category, brand_name, product_title, title_gmv, title_rank
FROM (
    SELECT
        brand_category,
        brand_name,
        product_title,
        SUM(gmv) AS title_gmv,
        ROW_NUMBER() OVER (
            PARTITION BY brand_name
            ORDER BY SUM(gmv) DESC
        ) AS title_rank
    FROM sku_sales
    WHERE year = 2026
      AND brand_name IN ({placeholder})
      AND product_title IS NOT NULL AND product_title <> ''
    GROUP BY brand_category, brand_name, product_title
) ranked
WHERE title_rank <= 50
ORDER BY brand_name, title_rank
"""

df = pd.read_sql(text(title_sql), engine, params=params)
print(f"\nexported rows: {len(df)}")
print(f"brands in output: {df['brand_name'].nunique()}")

out_path = BASE_DIR / "kol_top10_brands_top50_titles.xlsx"
df.to_excel(out_path, index=False)
print(f"saved to: {out_path}")
