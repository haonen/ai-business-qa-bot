# AI Business QA Bot

This folder contains the new implementation for AI生意问答Bot.

Current pilot scope:

- Import `sku_all.parquet` into MySQL table `sku_sales`.
- Import `kol_all.parquet` into MySQL table `kol_live_sales`.
- Keep all new code under this folder and leave the previous project files untouched.
- Run the v3 bot from `bot/`, using MySQL + Tool + Skill + Formatter architecture.

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
DASHSCOPE_ROUTER_MODEL=qwen-turbo
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
