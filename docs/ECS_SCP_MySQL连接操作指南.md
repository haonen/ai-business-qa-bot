# 北极星项目：ECS、SCP 与 MySQL 连接操作指南

本文档用于说明如何从自己的电脑连接北极星 ECS、上传或下载文件，以及连接项目使用的 MySQL 数据库。

> 安全提示：本文档不记录 ECS 密码、SSH 私钥、MySQL 密码或应用密钥。这些信息应通过安全渠道单独提供，不要发到群聊，也不要提交到 Git。

## 0. 连接信息

北极星项目当前使用以下 ECS 和服务器目录：

```text
ECS 地址：115.190.197.231
项目目录：/root/ai-business-qa-bot
MySQL 数据库：ai_business_qa_bot
MySQL 默认端口：3306
```

根据操作内容，会用到两类 ECS 账号：

| 用途 | ECS 账号 | 说明 |
|---|---|---|
| 部署和维护项目 | `root` | 用于上传代码、安装依赖和启动服务 |
| 建立 MySQL SSH 隧道 | `data` | 用于让本机通过 ECS 访问内网 MySQL |

如果你只被分配了其中一个账号，请使用管理员实际提供的账号。

## 1. 如何连接 ECS

### 1.1 使用账号密码登录

在 Mac Terminal、Windows PowerShell 或其他命令行工具中执行：

```bash
ssh root@115.190.197.231
```

第一次连接时，终端可能会显示：

```text
Are you sure you want to continue connecting (yes/no/[fingerprint])?
```

确认地址正确后输入：

```text
yes
```

随后输入单独获取的 ECS 密码。输入密码时屏幕不会显示字符，这是正常现象。

登录成功后，进入项目目录：

```bash
cd /root/ai-business-qa-bot
pwd
```

`pwd` 应该返回：

```text
/root/ai-business-qa-bot
```

### 1.2 使用 SSH 私钥登录

如果管理员提供的是 SSH 私钥，使用：

```bash
ssh -i <SSH_PRIVATE_KEY_PATH> root@115.190.197.231
```

例如：

```bash
ssh -i ~/.ssh/beijixing_ecs root@115.190.197.231
```

如果提示私钥权限过宽，先在本机执行：

```bash
chmod 600 ~/.ssh/beijixing_ecs
```

然后重新连接。

### 1.3 退出 ECS

执行：

```bash
exit
```

或按 `Control + D`。

## 2. 如何使用 SCP 上传和下载

`scp` 用于在自己的电脑和 ECS 之间复制文件。`scp` 命令应在自己电脑的终端中执行，不是登录 ECS 后再执行。

### 2.1 上传整个项目目录

在北极星工作区的上一层目录执行：

```bash
cd "/Users/shuoyang/北极星"
scp -r ai-business-qa-bot root@115.190.197.231:/root/
```

该命令会将本地项目复制到：

```text
/root/ai-business-qa-bot
```

> 注意：重复执行 `scp -r` 会覆盖同名文件并添加新文件，但不会删除 ECS 上本地已经不存在的旧文件。

### 2.2 上传单个文件

例如只更新 `bot/router.py`：

```bash
scp "/Users/shuoyang/北极星/ai-business-qa-bot/bot/router.py" \
  root@115.190.197.231:/root/ai-business-qa-bot/bot/router.py
```

### 2.3 上传一个数据文件

例如上传一个 Parquet 文件：

```bash
scp "/path/to/data.parquet" \
  root@115.190.197.231:/root/ai-business-qa-bot/data_import/data/
```

如果 ECS 目标目录还不存在，先登录 ECS 并创建：

```bash
ssh root@115.190.197.231
mkdir -p /root/ai-business-qa-bot/data_import/data
exit
```

### 2.4 从 ECS 下载文件到本机

例如下载一个日志文件：

```bash
scp root@115.190.197.231:/root/ai-business-qa-bot/<REMOTE_FILE> \
  "/Users/shuoyang/北极星/"
```

下载整个目录需要加 `-r`：

```bash
scp -r root@115.190.197.231:/root/ai-business-qa-bot/<REMOTE_DIRECTORY> \
  "/Users/shuoyang/北极星/"
```

### 2.5 SCP 使用 SSH 私钥

```bash
scp -i <SSH_PRIVATE_KEY_PATH> <LOCAL_FILE> \
  root@115.190.197.231:/root/ai-business-qa-bot/<REMOTE_PATH>
```

### 2.6 检查文件是否上传成功

```bash
ssh root@115.190.197.231
cd /root/ai-business-qa-bot
ls -lh
```

检查特定文件：

```bash
ls -lh /root/ai-business-qa-bot/<REMOTE_PATH>
```

## 3. 如何连接 MySQL

北极星 MySQL 使用内网地址，通常不能从个人电脑直接访问。有两种连接方式：

1. 登录 ECS 后，从 ECS 直接连接 MySQL。
2. 在本机建立 SSH Tunnel，再使用本地 MySQL 客户端连接。

### 3.1 在 ECS 上直接连接 MySQL

先登录 ECS：

```bash
ssh root@115.190.197.231
cd /root/ai-business-qa-bot
```

将项目 `.env` 中的数据库配置载入当前终端：

```bash
set -a
source .env
set +a
```

使用 MySQL 命令行客户端连接：

```bash
mysql \
  --protocol=TCP \
  --host="$MYSQL_HOST" \
  --port="$MYSQL_PORT" \
  --user="$MYSQL_USER" \
  --password \
  "$MYSQL_DATABASE"
```

出现提示后，输入 `.env` 中对应的 `MYSQL_PASSWORD`。如果看到：

```text
mysql>
```

说明连接成功。

可以执行以下只读检查：

```sql
SELECT DATABASE();
SHOW TABLES;
```

退出 MySQL：

```sql
exit;
```

### 3.2 从本机通过 SSH Tunnel 连接 MySQL

需要事先获取：

```text
ECS 账号：data
ECS 密码或 SSH 私钥
MySQL 内网地址
MySQL 账号：data_writer
MySQL 密码
```

打开第一个本地终端，建立隧道：

```bash
ssh -N -L 3307:<MYSQL_INTERNAL_HOST>:3306 data@115.190.197.231
```

将 `<MYSQL_INTERNAL_HOST>` 替换成真实的 MySQL 内网地址。

这个终端可能不会显示新的命令提示符，这是正常的。只要窗口保持打开，隧道就会保持连接。

如果使用 SSH 私钥：

```bash
ssh -i <SSH_PRIVATE_KEY_PATH> -N \
  -L 3307:<MYSQL_INTERNAL_HOST>:3306 \
  data@115.190.197.231
```

再打开第二个本地终端，连接本地端口 `3307`：

```bash
mysql \
  --protocol=TCP \
  --host=127.0.0.1 \
  --port=3307 \
  --user=data_writer \
  --password \
  ai_business_qa_bot
```

此时输入的是 MySQL 密码，不是 ECS 密码。

也可以直接执行连接测试：

```bash
mysql \
  --protocol=TCP \
  --host=127.0.0.1 \
  --port=3307 \
  --user=data_writer \
  --password \
  ai_business_qa_bot \
  -e "SELECT DATABASE(); SHOW TABLES;"
```

### 3.3 本地代码通过隧道连接 MySQL

隧道建立后，本地代码使用以下配置：

```env
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3307
MYSQL_DATABASE=ai_business_qa_bot
MYSQL_USER=data_writer
MYSQL_PASSWORD=<MYSQL_PASSWORD>
```

注意：

- 本地 `.env` 里写 `127.0.0.1:3307`。
- MySQL 内网地址只写在 SSH Tunnel 命令里。
- 隧道终端关闭后，本地代码将无法连接 MySQL。
- 不要将包含真实密码的 `.env` 提交到 Git。

## 4. 常见问题

### 4.1 `Permission denied`

可能原因：

- ECS 账号或密码错误。
- SSH 私钥路径错误。
- SSH 私钥权限不是 `600`。
- 当前账号没有目标目录的权限。

排查命令：

```bash
ssh -v root@115.190.197.231
```

### 4.2 `Connection timed out`

可能原因：

- 当前网络无法访问 ECS。
- ECS 安全组没有放行当前出口 IP 的 SSH 端口。
- 公司 VPN 或防火墙拦截了连接。

### 4.3 本地端口 `3307` 被占用

可以改用其他本地端口，例如 `13307`：

```bash
ssh -N -L 13307:<MYSQL_INTERNAL_HOST>:3306 data@115.190.197.231
```

本地 MySQL 连接改为：

```bash
mysql --host=127.0.0.1 --port=13307 --user=data_writer --password ai_business_qa_bot
```

### 4.4 `mysql: command not found`

Mac 可以使用 Homebrew 安装 MySQL 客户端：

```bash
brew install mysql-client
```

如果安装后仍找不到命令，按 Homebrew 的安装提示将 `mysql-client` 加入 `PATH`。

Linux 根据系统使用：

```bash
sudo apt-get install mysql-client
```

或：

```bash
sudo yum install mysql
```

### 4.5 MySQL `Access denied`

确认：

- 输入的是 MySQL 密码，而不是 ECS 密码。
- MySQL 用户名正确。
- 直连 ECS 时使用内网地址和 `3306`。
- 通过本地隧道时使用 `127.0.0.1` 和 `3307`。

## 5. 快速命令清单

### 登录 ECS

```bash
ssh root@115.190.197.231
```

### 上传整个项目

```bash
cd "/Users/shuoyang/北极星"
scp -r ai-business-qa-bot root@115.190.197.231:/root/
```

### ECS 内连接 MySQL

```bash
cd /root/ai-business-qa-bot
set -a
source .env
set +a
mysql --host="$MYSQL_HOST" --port="$MYSQL_PORT" --user="$MYSQL_USER" --password "$MYSQL_DATABASE"
```

### 本地建立 MySQL 隧道

```bash
ssh -N -L 3307:<MYSQL_INTERNAL_HOST>:3306 data@115.190.197.231
```

### 本地通过隧道连接 MySQL

```bash
mysql --host=127.0.0.1 --port=3307 --user=data_writer --password ai_business_qa_bot
```
