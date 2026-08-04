# AI Business QA Bot

This folder contains the new implementation for AI生意问答Bot.

Current pilot scope:

- Import `sku_all.parquet` into MySQL table `sku_sales`.
- Import `kol_all.parquet` into MySQL table `kol_live_sales`.
- Clean monthly `Search Report.xlsx` data and load it into MySQL table
  `ai_bot_media_search_index`.
- Clean `Topline.xlsx`, apply overall/Xiaohongshu/Douyin BKFS labels, and load
  media investment records into `ai_bot_media_topline_investment`.
- Stream large annual KSI workbooks into lightweight Parquet files and load
  KOL cost/performance data into `ai_bot_media_ksi_performance`.
- Keep all new code under this folder and leave the previous project files untouched.
- Run the v3 bot from `bot/`, using MySQL + Tool + Skill + Formatter architecture.

Search data cleaning, field definitions, monthly refresh instructions, and Tool
query constraints are documented in
[`docs/search_data_pipeline.md`](docs/search_data_pipeline.md).
Topline field definitions, BKFS rules, upload commands, and validation queries
are documented in
[`docs/topline_data_pipeline.md`](docs/topline_data_pipeline.md).
KSI field compatibility, lightweight transfer, dynamic CPE, monthly refresh,
and MySQL validation are documented in
[`docs/ksi_data_pipeline.md`](docs/ksi_data_pipeline.md).
BET media report routing, period rules, Tool contracts, formulas, and runtime
checks are documented in
[`docs/media_analysis_runtime.md`](docs/media_analysis_runtime.md).
Market trend and Top-brand routing, monthly/daily blending, and ranking rules
are documented in
[`docs/market_analysis_runtime.md`](docs/market_analysis_runtime.md).

## Server Directory

Use one unified directory on ECS:

```text
/root/ai-business-qa-bot
```

## Local Data Preparation

From the old project root on your Mac:

```bash
mkdir -p ai-business-qa-bot/data_import/data
cp data/processed/sku_all.parquet ai-business-qa-bot/data_import/data/
cp data/processed/kol_all.parquet ai-business-qa-bot/data_import/data/
```

## Upload To ECS

From the old project root on your Mac:

```bash
scp -r ai-business-qa-bot root@115.190.197.231:/root/
```

If the folder already exists on ECS, this command updates/adds files inside it.

## ECS Setup

SSH into ECS:

```bash
ssh root@115.190.197.231
cd /root/ai-business-qa-bot
```

Create the real `.env` file on ECS only:

```bash
cp .env.example .env
nano .env
chmod 600 .env
```

Required runtime settings:

```bash
MYSQL_HOST=your-mysql-internal-host
MYSQL_PORT=3306
MYSQL_USER=your-mysql-user
MYSQL_PASSWORD=your-mysql-password
MYSQL_DATABASE=ai_business_qa_bot

FEISHU_APP_ID=your-feishu-app-id
FEISHU_APP_SECRET=your-feishu-app-secret

DASHSCOPE_API_KEY=your-dashscope-api-key
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
DASHSCOPE_MODEL=qwen-plus
DASHSCOPE_ROUTER_MODEL=qwen3.7-plus
DASHSCOPE_SUMMARY_MODEL=qwen-plus-latest
```

Install dependencies:

```bash
cd /root/ai-business-qa-bot/data_import

if command -v apt-get >/dev/null 2>&1; then
  apt-get update
  apt-get install -y mysql-client python3-venv python3-pip
elif command -v yum >/dev/null 2>&1; then
  yum install -y mysql python3 python3-pip
fi

python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

Test MySQL connection:

```bash
cd /root/ai-business-qa-bot
set -a
source .env
set +a

MYSQL_PWD="$MYSQL_PASSWORD" mysql \
  -h "$MYSQL_HOST" \
  -P "$MYSQL_PORT" \
  -u "$MYSQL_USER" \
  -e "CREATE DATABASE IF NOT EXISTS \`$MYSQL_DATABASE\` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; SHOW DATABASES;"
```

Run import:

```bash
cd /root/ai-business-qa-bot/data_import
source .venv/bin/activate
python import_parquet_to_mysql.py
```

## Runtime Bot Setup

Install runtime dependencies:

```bash
cd /root/ai-business-qa-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt
```

Start the Feishu bot:

```bash
cd /root/ai-business-qa-bot
source .venv/bin/activate
python -m bot.main
```

The v3 runtime code lives under:

```text
bot/
├── app.py
├── main.py
├── router.py
├── session.py
├── chains/
├── tools/
├── skills/
├── data/
└── db/
```

## Follow-up Skills V2

The executable follow-up architecture and rollout switches are documented in
[`docs/followup_skills_v2.md`](docs/followup_skills_v2.md). Default EC and BET
report chains remain separate; only narrow follow-up and data-organization
requests enter the V2 planner and whitelisted tools.
