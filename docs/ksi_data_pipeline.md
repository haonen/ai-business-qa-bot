# KSI 数据清洗、轻量传输与 MySQL 入库

## 1. 设计结论

KSI 2025 和 KSI 2026 统一写入一张明细表：

```text
ai_bot_media_ksi_performance
```

每行保留一条 KSI 投放/内容表现记录。`Big V` 是花费金额，标准字段为
`big_v_cost`。

CPE 不作为固定字段写入：

```text
CPE = SUM(big_v_cost) / SUM(ttl_engagement)
```

必须先按照用户选择的时间、品牌、Tier、KOL type、平台或 KOL 聚合，再计算
CPE。禁止计算逐行 CPE 后再取平均。

## 2. 两年字段兼容

KSI 2025：

- 391,959 行。
- 30 个源字段。
- 包含 BET、MONTH、Selectivity 和第二个 Tier。

KSI 2026：

- 211,461 行。
- 26 个源字段。
- 不包含 BET、MONTH、Selectivity 和第二个 Tier，对应标准字段存 NULL。

两个 Tier 同时存在时，第一个作为标准 `tier`，第二个保留为
`tier_secondary`。首批 2025 数据中两个 Tier 没有不一致记录。

## 3. 轻量分析表字段

保留：

- 时间：`published_at`、`period_month`、`year`、`month`。
- 业务维度：平台、品牌、Tier、KOL type、大小类目、SKU、Selectivity。
- KOL 标识：NickName、前台/后台 KOL ID。
- 花费：`big_v_cost`、BET。
- 表现：粉丝、播放、评论、点赞、转发、收藏、TTL engagement。
- 内容元数据：内容形式、内容 ID、标题、跳转平台。
- 审计字段：来源文件、sheet、原始行号、导入批次。

不写入分析表：

- Content 正文。
- 原始 URL。
- 跳转链接。

这些长文本不参与当前的 Weight、Evol、Top 10 或 CPE 分析。排除后，两年标准化
Parquet 从约 350MB 原始 Excel 压缩到约 68MB。原始 Excel 保留在本地作为归档；
未来如需内容文本分析，应建立单独的内容详情表或对象存储。

## 4. 首批质量结果

| 年份 | 行数 | Big V 合计 | TTL engagement 合计 | Parquet |
|---|---:|---:|---:|---:|
| 2025 | 391,959 | 5,715,107,887.47 | 1,769,831,875 | 约 45MB |
| 2026 | 211,461 | 2,733,319,024.00 | 1,067,970,116 | 约 23MB |
| 合计 | 603,420 | 8,448,426,911.47 | 2,837,801,991 | 约 68MB |

两年均无必填字段无效、负数 Big V 或空 TTL engagement。

## 5. 本地分别清洗两个年度

超大 Excel 必须在两个独立命令中处理，避免同一 Python 进程连续加载两套共享字符串
导致峰值内存过高。

```bash
cd "/Users/shuoyang/北极星/ai-business-qa-bot"
source "/Users/shuoyang/北极星/venv/bin/activate"
```

准备 2025：

```bash
python -m data_import.ksi_pipeline prepare \
  --input "/Users/shuoyang/北极星/BET data/OneDrive_1_2026-7-30/KSI 2025.xlsx" \
  --output-dir "data_import/output/ksi" \
  --batch-id "ksi_initial_20260730"
```

准备 2026：

```bash
python -m data_import.ksi_pipeline prepare \
  --input "/Users/shuoyang/北极星/BET data/OneDrive_1_2026-7-30/KSI 2026.xlsx" \
  --output-dir "data_import/output/ksi" \
  --batch-id "ksi_initial_20260730"
```

最终需要上传的两个数据文件：

```text
data_import/output/ksi/media_ksi_ksi_2025.parquet
data_import/output/ksi/media_ksi_ksi_2026.parquet
```

## 6. 上传代码和压缩数据

本地执行：

```bash
cd "/Users/shuoyang/北极星/ai-business-qa-bot"

ssh root@115.190.197.231 \
  "mkdir -p /root/ai-business-qa-bot/data_import/sql \
             /root/ai-business-qa-bot/data_import/data/ksi"
```

上传代码和建表 SQL：

```bash
scp \
  data_import/__init__.py \
  data_import/ksi_pipeline.py \
  data_import/requirements.txt \
  root@115.190.197.231:/root/ai-business-qa-bot/data_import/

scp \
  data_import/sql/media_ksi_performance.sql \
  root@115.190.197.231:/root/ai-business-qa-bot/data_import/sql/
```

只上传约 68MB 的 Parquet，不上传两个大 Excel：

```bash
scp \
  data_import/output/ksi/media_ksi_ksi_2025.parquet \
  data_import/output/ksi/media_ksi_ksi_2026.parquet \
  root@115.190.197.231:/root/ai-business-qa-bot/data_import/data/ksi/
```

## 7. 服务器写入 MySQL

```bash
ssh root@115.190.197.231
cd /root/ai-business-qa-bot
source .venv/bin/activate
python -m pip install -r data_import/requirements.txt
```

确认文件：

```bash
ls -lh data_import/data/ksi/
```

创建表并分块写入两个年度：

```bash
python -m data_import.ksi_pipeline load \
  --parquet \
    data_import/data/ksi/media_ksi_ksi_2025.parquet \
    data_import/data/ksi/media_ksi_ksi_2026.parquet \
  --chunk-size 500
```

程序会：

1. 自动执行 `media_ksi_performance.sql`。
2. 读取 Parquet 中涉及的年份×月份。
3. 在同一事务中删除对应月份的旧数据。
4. 每批写入 500 行。
5. 失败时整体回滚。

成功提示：

```text
MySQL partition replace completed: 603,420 rows
```

## 8. 每月更新

每月收到新的 KSI Excel 后：

1. 在本地单独运行 `prepare`，生成当月 Parquet。
2. 将当月 Parquet 上传到服务器 `data_import/data/ksi/`。
3. 运行 `load --parquet 当月文件.parquet`。
4. 验证当月行数、Big V 和 TTL engagement。

月度 Parquet 必须包含该月份的完整数据，因为 load 会替换文件覆盖的年月分区。

## 9. MySQL 入库校验

```sql
SELECT
    COUNT(*) AS row_count,
    MIN(period_month) AS min_month,
    MAX(period_month) AS max_month,
    ROUND(SUM(big_v_cost), 2) AS big_v_cost,
    SUM(ttl_engagement) AS ttl_engagement
FROM ai_bot_media_ksi_performance;
```

预期约为：

```text
row_count       603420
min_month       2025-01-01
max_month       2026-05-01
big_v_cost      8448426911.47
ttl_engagement  2837801991
```

按月核对：

```sql
SELECT
    year,
    month,
    COUNT(*) AS row_count,
    ROUND(SUM(big_v_cost), 2) AS big_v_cost,
    SUM(ttl_engagement) AS ttl_engagement
FROM ai_bot_media_ksi_performance
GROUP BY year, month
ORDER BY year, month;
```

检查 2026 扩展字段为空：

```sql
SELECT
    SUM(bet IS NOT NULL) AS bet_rows,
    SUM(source_month IS NOT NULL) AS source_month_rows,
    SUM(selectivity IS NOT NULL) AS selectivity_rows,
    SUM(tier_secondary IS NOT NULL) AS tier_secondary_rows
FROM ai_bot_media_ksi_performance
WHERE year = 2026;
```

预期均为 0。

## 10. Tool 查询公式

### Tier 花费 Weight 和 Evol

以下示例比较相同的 1–5 月：

```sql
WITH tier_year AS (
    SELECT
        tier,
        year,
        SUM(big_v_cost) AS cost
    FROM ai_bot_media_ksi_performance
    WHERE year IN (2025, 2026)
      AND month BETWEEN 1 AND 5
    GROUP BY tier, year
),
year_total AS (
    SELECT year, SUM(cost) AS total_cost
    FROM tier_year
    GROUP BY year
)
SELECT
    t.tier,
    SUM(CASE WHEN t.year = 2025 THEN t.cost ELSE 0 END) AS cost_2025,
    SUM(CASE WHEN t.year = 2026 THEN t.cost ELSE 0 END) AS cost_2026,
    SUM(CASE WHEN t.year = 2025 THEN t.cost / NULLIF(y.total_cost, 0) ELSE 0 END)
        AS weight_2025,
    SUM(CASE WHEN t.year = 2026 THEN t.cost / NULLIF(y.total_cost, 0) ELSE 0 END)
        AS weight_2026,
    (
        SUM(CASE WHEN t.year = 2026 THEN t.cost ELSE 0 END) -
        SUM(CASE WHEN t.year = 2025 THEN t.cost ELSE 0 END)
    ) / NULLIF(
        SUM(CASE WHEN t.year = 2025 THEN t.cost ELSE 0 END),
        0
    ) AS evol
FROM tier_year t
JOIN year_total y ON y.year = t.year
GROUP BY t.tier
ORDER BY cost_2026 DESC;
```

按 KOL type 时，将 `tier` 替换为 `kol_type`。

### 按 Engagement 排 Top 10 KOL，并动态计算 CPE

```sql
SELECT
    platform,
    COALESCE(kol_id_front, kol_id_back, nickname) AS kol_key,
    MAX(nickname) AS nickname,
    SUM(big_v_cost) AS big_v_cost,
    SUM(ttl_engagement) AS ttl_engagement,
    SUM(big_v_cost) / NULLIF(SUM(ttl_engagement), 0) AS cpe
FROM ai_bot_media_ksi_performance
WHERE period_month BETWEEN '2026-01-01' AND '2026-05-01'
  AND COALESCE(kol_id_front, kol_id_back, nickname) IS NOT NULL
GROUP BY
    platform,
    COALESCE(kol_id_front, kol_id_back, nickname)
ORDER BY ttl_engagement DESC
LIMIT 10;
```
