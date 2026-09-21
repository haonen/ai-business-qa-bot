# Redis/RQ 队列部署

## 1. 选择配置

同一套代码支持不同 ECS 规格：

| ECS | `.env` | Redis `maxmemory` |
|---|---|---|
| 2 核 8 GiB | `BOT_WORKER_COUNT=2`、`BOT_INNER_QUERY_WORKERS=3` | `512mb` |
| 4 核 16 GiB | `BOT_WORKER_COUNT=4` | `1gb` |
| 8 核 32 GiB（稳定） | `BOT_WORKER_COUNT=8` | `2gb` |
| 8 核 32 GiB（突发） | `BOT_WORKER_COUNT=12` | `2gb` |
| 自动 | `BOT_WORKER_COUNT=auto` | 按实际机器设置 |

`auto` 等于系统检测到的逻辑 CPU 数。显式值允许 1—16；超过 CPU 两倍或数据库连接预算时，worker pool 会拒绝启动。

## 2. 安装 Redis 和依赖

在 Ubuntu 24.04 ECS 上执行：

```bash
apt-get update
apt-get install -y redis-server

cd /root/ai-business-qa-bot
source .venv/bin/activate
pip install -r requirements.txt
```

将 `deploy/redis-ai-bot.conf` 中的配置合并到 `/etc/redis/redis.conf`。该文件默认是 2 核档位的 `512mb`；升级到 4 核或 8 核后再分别改为 `1gb` 或 `2gb`。不要在安全组开放 6379。

```bash
systemctl enable --now redis-server
redis-cli ping
```

预期返回 `PONG`。

## 3. 配置并安装服务

先保持 `.env` 中：

```env
BOT_QUEUE_ENABLED=0
REDIS_URL=redis://127.0.0.1:6379/0
BOT_WORKER_COUNT=2
BOT_INNER_QUERY_WORKERS=3
MYSQL_POOL_SIZE=3
MYSQL_MAX_OVERFLOW=1
BOT_MAX_DB_CONNECTION_BUDGET=8
BOT_ASYNC_DOCUMENTS=1
BOT_DOCUMENT_QUEUE_NAME=ai-bot-documents
BOT_DOCUMENT_WORKER_COUNT=1
BOT_DOCUMENT_TIMEOUT_SECONDS=180
```

安装 systemd 服务：

```bash
cp deploy/ai-bot-receiver.service /etc/systemd/system/
cp deploy/ai-bot-workers.service /etc/systemd/system/
cp deploy/ai-bot-document-workers.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable ai-bot-receiver ai-bot-workers ai-bot-document-workers
systemctl start ai-bot-workers ai-bot-document-workers
```

健康检查：

```bash
cd /root/ai-business-qa-bot
source .venv/bin/activate
python -m bot.healthcheck
```

确认 Redis、MySQL 均为 `ok`，分析 worker 为 2、文档 worker 为 1，内部查询线程上限为 3。

## 4. 启用队列

将 `.env` 修改为：

```env
BOT_QUEUE_ENABLED=1
```

重启接收器：

```bash
systemctl restart ai-bot-receiver
systemctl status ai-bot-receiver ai-bot-workers ai-bot-document-workers
```

调整 worker 数只需要修改 `BOT_WORKER_COUNT`，然后执行：

```bash
systemctl restart ai-bot-workers
```

查看队列及日志：

```bash
source /root/ai-business-qa-bot/.venv/bin/activate
rq info -u redis://127.0.0.1:6379/0 ai-bot
rq info -u redis://127.0.0.1:6379/0 ai-bot ai-bot-documents
journalctl -u ai-bot-receiver -u ai-bot-workers -u ai-bot-document-workers -f
```

## 5. 回退

将 `BOT_QUEUE_ENABLED=0` 并重启 receiver，即可恢复原同步模式。不要在有任务运行时清空 Redis；先停止接收器并等待队列清空。
