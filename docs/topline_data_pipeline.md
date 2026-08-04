# Topline 媒体投资清洗、BKFS 标签与 MySQL 入库

## 1. 数据口径

- 数据源：`Topline.xlsx`。
- 一行代表某年某月、某组业务维度和媒体形式的一笔媒体花费记录。
- `Total(M)` 是媒体花费真实值，标准字段为 `spend_million`，单位是 million。
- `Total wo VAT` 仅作为源字段保留，标准字段为 `spend_wo_vat_million`。
- 2025 和 2026 数据使用完全相同的标签规则。
- Evol 必须在查询层按相同月份、相同维度聚合后计算，不能在明细表中预先固化。

## 2. 三个标签字段

标签规则来自 `BKFS标签说明.xlsx`，已整理到
`data_import/config/bkfs_rules.json`。

| 字段 | 适用数据 | 可取值 |
|---|---|---|
| `bkfs_overall` | 全部记录 | B/K/F/S/T/NULL |
| `bkfs_xiaohongshu` | `Standard APP Name` 包含“小红书” | B/K/F/S/NULL |
| `bkfs_douyin` | `Standard APP Name` 包含“抖音” | B/K/F/S/T/NULL |

整体规则：

- K：`A I T (ROE) = Influencer`
- T：`A I T (ROE) = Transaction`
- B：剩余记录的 `Standard Ad Format` 包含 Brandzone、Opening 或 Banner
- F：剩余记录的 `Standard Ad Format` 包含 Feeds
- S：剩余记录的 `Standard Ad Format` 包含 Search

小红书规则：

- 仅对 APP 名包含“小红书”的记录打标。
- K：Influencer。
- B：Brandzone 或 Opening。
- F：Feeds 相关。
- S：Search。
- 小红书没有 T 规则；Transaction 若同时是 Feeds，会回落为 F。

抖音规则：

- 对 APP 名包含“抖音”的记录打标，包括“抖音/微信”等组合名称。
- K：Influencer。
- T：Transaction。
- B：Brandzone 或 Banner。
- F：Feeds 相关。
- S：Search。

判断优先级为 AIT 标签优先、广告形式其次，避免 Transaction+Feeds 同时命中 T 和 F。
未命中规则的记录保留，标签为 NULL。

## 3. 标准字段

MySQL 表名：`ai_bot_media_topline_investment`。

除三个标签和审计字段外，源字段映射如下：

| 源字段 | 标准字段 |
|---|---|
| Category | `category` |
| Brand-R | `brand_r` |
| Universe | `universe` |
| Year / Month | `year` / `month` |
| 月份标准日期 | `period_month`，统一为当月 1 日 |
| Luxe_Brands | `luxe_brands` |
| Division for LOREAL | `division_for_loreal` |
| Cate / Cate-1 / Cate-2 | `cate` / `cate_1` / `cate_2` |
| top 15 group | `top_15_group` |
| Group type | `group_type` |
| Media / Submedia | `media` / `submedia` |
| Key Competitors | `key_competitors` |
| Sponsor Type | `sponsor_type` |
| Total(M) | `spend_million` |
| Total wo VAT | `spend_wo_vat_million` |
| Standard APP Name | `standard_app_name` |
| Standard Ad Format | `standard_ad_format` |
| A I T (ROE) | `ait_roe` |

审计字段包括 `record_key`、来源文件、sheet、原始行号和导入批次。

## 4. 数据清洗规则

1. 校验 21 个源字段完整存在。
2. 清理字符串前后空格、连续空格和不间断空格。
3. Year、Month、Total(M)、Total wo VAT 转换为数值。
4. 月份必须为 1–12；年份、月份、品牌和 Total(M) 不得为空。
5. 不对相同明细行去重。源数据没有投放唯一键，重复行可能代表真实重复投放。
6. 保留负数 Total(M)，用于 Rebate 等净额调整。
7. 标签按外部 JSON 配置执行，便于后续调整。
8. 生成 Parquet 全量文件、2,000 行 CSV 抽样和 JSON 质量报告。

首批全量质量检查发现：

- 510,067 行全部通过必填字段校验。
- 18,341 行与其他源记录完全相同；由于源文件没有投放唯一键，全部保留。
- 990 行 Total(M) 为负数，合计约 -738.31 million，按 Rebate 等净额调整保留。
- 小红书 `Keywords`、抖音 `Topview`/`Opening` 等未出现在附件对应平台规则中，因此标签为 NULL。
  质量报告的 `top_untagged` 会列出这些记录，不做未经确认的自动归类。

## 5. 本地清洗

```bash
cd "/Users/shuoyang/北极星/ai-business-qa-bot"

source "/Users/shuoyang/北极星/venv/bin/activate"

python -m data_import.topline_pipeline \
  --input "/Users/shuoyang/北极星/BET data/OneDrive_1_2026-7-30/Topline.xlsx" \
  --output-dir "data_import/output/topline" \
  --batch-id "topline_initial_20260730"
```

输出：

- `media_topline_investment.parquet`
- `media_topline_investment_sample.csv`
- `media_topline_quality_report.json`

## 6. 上传服务器

本地执行：

```bash
cd "/Users/shuoyang/北极星/ai-business-qa-bot"

ssh root@115.190.197.231 \
  "mkdir -p /root/ai-business-qa-bot/data_import/config \
             /root/ai-business-qa-bot/data_import/sql \
             /root/ai-business-qa-bot/data_import/data \
             /root/ai-business-qa-bot/data_import/output/topline"
```

上传代码、配置和建表 SQL：

```bash
scp \
  data_import/__init__.py \
  data_import/topline_pipeline.py \
  data_import/requirements.txt \
  root@115.190.197.231:/root/ai-business-qa-bot/data_import/

scp \
  data_import/config/bkfs_rules.json \
  root@115.190.197.231:/root/ai-business-qa-bot/data_import/config/

scp \
  data_import/sql/media_topline_investment.sql \
  root@115.190.197.231:/root/ai-business-qa-bot/data_import/sql/
```

上传源文件：

```bash
scp \
  "/Users/shuoyang/北极星/BET data/OneDrive_1_2026-7-30/Topline.xlsx" \
  root@115.190.197.231:/root/ai-business-qa-bot/data_import/data/
```

## 7. 服务器清洗与 MySQL 写入

登录服务器：

```bash
ssh root@115.190.197.231
cd /root/ai-business-qa-bot
source .venv/bin/activate
python -m pip install -r data_import/requirements.txt
```

先只清洗：

```bash
python -m data_import.topline_pipeline \
  --input "data_import/data/Topline.xlsx" \
  --output-dir "data_import/output/topline" \
  --batch-id "topline_initial_20260730"
```

检查质量报告：

```bash
python -m json.tool \
  data_import/output/topline/media_topline_quality_report.json
```

确认后建表并写入：

```bash
python -m data_import.topline_pipeline \
  --input "data_import/data/Topline.xlsx" \
  --output-dir "data_import/output/topline" \
  --batch-id "topline_initial_20260730" \
  --load-mysql \
  --chunk-size 1000
```

程序会自动执行建表 SQL，然后在同一事务内替换本次文件覆盖的 Year×Month 分区。
重复运行不会重复插入；失败时整个事务回滚。

## 8. 入库校验

进入 MySQL：

```bash
set -a
source .env
set +a

mysql \
  -h "$MYSQL_HOST" \
  -P "$MYSQL_PORT" \
  -u "$MYSQL_USER" \
  -p \
  "$MYSQL_DATABASE"
```

检查总行数与总花费：

```sql
SELECT
    COUNT(*) AS row_count,
    ROUND(SUM(spend_million), 6) AS spend_million
FROM ai_bot_media_topline_investment;
```

首批数据预期约为 510,067 行、139,759.024 million。MySQL DECIMAL 对每行保留 6 位小数，
汇总值可能与 Excel 浮点结果有极小舍入差异。

检查年月：

```sql
SELECT
    year,
    month,
    COUNT(*) AS row_count,
    ROUND(SUM(spend_million), 2) AS spend_million
FROM ai_bot_media_topline_investment
GROUP BY year, month
ORDER BY year, month;
```

检查三个标签：

```sql
SELECT bkfs_overall, COUNT(*) AS rows,
       ROUND(SUM(spend_million), 2) AS spend_million
FROM ai_bot_media_topline_investment
GROUP BY bkfs_overall
ORDER BY bkfs_overall;
```

```sql
SELECT bkfs_xiaohongshu, COUNT(*) AS rows,
       ROUND(SUM(spend_million), 2) AS spend_million
FROM ai_bot_media_topline_investment
WHERE standard_app_name LIKE '%小红书%'
GROUP BY bkfs_xiaohongshu
ORDER BY bkfs_xiaohongshu;
```

```sql
SELECT bkfs_douyin, COUNT(*) AS rows,
       ROUND(SUM(spend_million), 2) AS spend_million
FROM ai_bot_media_topline_investment
WHERE standard_app_name LIKE '%抖音%'
GROUP BY bkfs_douyin
ORDER BY bkfs_douyin;
```

## 9. Tool 查询与 Evol

真实花费使用：

```sql
SUM(spend_million)
```

比较 2025 和 2026 时必须锁定相同月份。例如 2026 当前只有 1–5 月，就只能与 2025 年
1–5 月比较。

品牌×整体 BKFS 的示例：

```sql
WITH yearly AS (
    SELECT
        brand_r,
        bkfs_overall,
        year,
        SUM(spend_million) AS spend_million
    FROM ai_bot_media_topline_investment
    WHERE year IN (2025, 2026)
      AND month BETWEEN 1 AND 5
      AND bkfs_overall IS NOT NULL
    GROUP BY brand_r, bkfs_overall, year
),
pivoted AS (
    SELECT
        brand_r,
        bkfs_overall,
        SUM(CASE WHEN year = 2025 THEN spend_million ELSE 0 END) AS spend_2025,
        SUM(CASE WHEN year = 2026 THEN spend_million ELSE 0 END) AS spend_2026
    FROM yearly
    GROUP BY brand_r, bkfs_overall
)
SELECT
    brand_r,
    bkfs_overall,
    spend_2025,
    spend_2026,
    CASE
        WHEN spend_2025 = 0 THEN NULL
        ELSE (spend_2026 - spend_2025) / spend_2025
    END AS evol
FROM pivoted;
```

平台查询分别使用 `bkfs_xiaohongshu` 和 `bkfs_douyin`，不要拿整体标签代替平台标签。
