# 搜索数据清洗与 MySQL 入库说明

## 1. 数据源与表设计

数据源为 `Search Report.xlsx`，每个月一个 sheet。

搜索数据使用一张 MySQL 表 `ai_bot_media_search_index`：

- `grain_level = brand`：品牌总榜，`category` 为空。目前适用于 1、2、5、6 月。
- `grain_level = brand_category`：品牌×类目明细，`category` 有值。目前适用于 3、4 月。

不拆成两张表的原因：

1. 两种数据拥有相同的时间、品牌、排名、搜索指数和同比字段。
2. 月度更新只需维护一条导入链路。
3. Tool 可以先判断用户是否问类目，再显式选择粒度。

严禁在一个查询中直接混合两个粒度后求和、排名或计算占比。搜索指数也不应跨类目或跨月份直接求和。

## 2. 标准字段

| 字段 | 类型 | 含义 |
|---|---|---|
| `record_key` | CHAR(64) | 年、月、粒度、品牌、类目的稳定 SHA-256 业务键 |
| `report_month` | DATE | 报告月份，统一存当月 1 日 |
| `report_year` | SMALLINT | 当年搜索指数对应年份 |
| `report_month_num` | TINYINT | 月份 1–12 |
| `previous_year` | SMALLINT | 对比年份，通常为 `report_year - 1` |
| `grain_level` | VARCHAR(32) | `brand` 或 `brand_category` |
| `brand` | VARCHAR(255) | 品牌 |
| `category` | VARCHAR(255) | 类目；品牌总榜为空 |
| `current_rank` | INT | 当年排名 |
| `current_search_index` | BIGINT | 当年搜索指数 |
| `previous_search_index` | BIGINT | 上年同期搜索指数 |
| `source_yoy_rate` | DECIMAL | 源文件同比，10% 存为 0.1 |
| `calculated_yoy_rate` | DECIMAL | 根据两个搜索指数重算的同比 |
| `source_file` | VARCHAR(255) | 来源文件名 |
| `source_sheet` | VARCHAR(100) | 来源 sheet |
| `source_row_number` | INT | Excel 原始行号 |
| `import_batch_id` | VARCHAR(100) | 导入批次 |

## 3. 清洗规则

1. 只读取名称形如 `1月`–`12月` 的 sheet，月份以 sheet 名为准。
2. 去除表头和文本字段中的多余空格、换行及不间断空格。
3. 有有效类目列的 sheet 标记为 `brand_category`，否则标记为 `brand`。
4. 搜索指数、排名转换为整数；同比百分比转换为小数。
5. `calculated_yoy_rate = (current_search_index - previous_search_index) / previous_search_index`。
6. 上年搜索指数为 0 时，重算同比为空，不输出无限增长。
7. 源同比与重算同比差异超过 0.02 个百分点时写入质量警告。
8. 关键字段缺失的行不入库，并记录数量。
9. 同一个月、粒度、品牌、类目出现重复业务键时终止导入，避免静默覆盖。
10. 当前文件的“5月”sheet 表头写成 06 月，按 sheet 名作为 5 月入库并记录警告。
11. “6月”sheet 使用通用字段名，年份从同一工作簿其他 sheet 推断；若无法推断必须传 `--report-year`。

清洗后同时生成：

- `media_search_index.parquet`：后续程序和归档使用。
- `media_search_index.csv`：人工抽查使用。
- `media_search_quality_report.json`：每个 sheet 的行数、粒度和质量告警。

## 4. 首次清洗

在项目根目录执行：

```bash
python -m data_import.search_pipeline \
  --input "/path/to/Search Report.xlsx" \
  --output-dir "data_import/output/search"
```

## 5. MySQL 建表与写入

先在项目根目录配置 `.env`：

```text
MYSQL_HOST=...
MYSQL_PORT=3306
MYSQL_USER=...
MYSQL_PASSWORD=...
MYSQL_DATABASE=ai_business_qa_bot
```

清洗并写入：

```bash
python -m data_import.search_pipeline \
  --input "/path/to/Search Report.xlsx" \
  --output-dir "data_import/output/search" \
  --load-mysql
```

程序会执行 `data_import/sql/media_search_index.sql`，不存在时建表；随后在同一事务中：

1. 删除本次文件所覆盖的“年份×月份×粒度”分区；
2. 分批写入清洗后的完整分区。

因此同一月份重复运行不会重复插入；如果修订版源文件删除了某个品牌或类目，数据库中也不会残留旧记录。
任一步骤失败时整个事务回滚，不会留下只删未写的月份。

## 6. 每月更新

新文件可以只包含新月份，也可以包含历史月份：

1. 将新文件放入约定的数据接收目录。
2. 运行同一条清洗和入库命令。
3. 检查质量报告无 error，并复核 warning。
4. 执行入库后的月份、粒度和行数检查。

若新文件只有通用字段 `now_date_search_index` / `last_date_search_index`，执行时明确指定：

```bash
python -m data_import.search_pipeline \
  --input "/path/to/Search Report.xlsx" \
  --report-year 2026 \
  --load-mysql
```

## 7. Tool 查询约束

品牌问题只查品牌粒度：

```sql
SELECT *
FROM ai_bot_media_search_index
WHERE brand = :brand
  AND grain_level = 'brand'
ORDER BY report_month;
```

类目问题只查类目粒度：

```sql
SELECT *
FROM ai_bot_media_search_index
WHERE brand = :brand
  AND category = :category
  AND grain_level = 'brand_category'
ORDER BY report_month;
```

如果用户追问 1、2、5、6 月的类目搜索表现，Tool 应明确回复“该月份数据仅支持品牌粒度”，
不得退化为品牌数据后冒充类目结论。

## 8. 入库后校验 SQL

```sql
SELECT report_year, report_month_num, grain_level, COUNT(*) AS row_count
FROM ai_bot_media_search_index
GROUP BY report_year, report_month_num, grain_level
ORDER BY report_year, report_month_num, grain_level;
```

```sql
SELECT source_sheet, COUNT(*) AS yoy_mismatch_count
FROM ai_bot_media_search_index
WHERE source_yoy_rate IS NOT NULL
  AND calculated_yoy_rate IS NOT NULL
  AND ABS(source_yoy_rate - calculated_yoy_rate) > 0.0002
GROUP BY source_sheet;
```
