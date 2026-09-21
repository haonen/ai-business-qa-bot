# ECS staging environment

The staging runtime is isolated from production by directory, Linux user,
virtual environment, Redis process, port, queues, configuration, logs, and
systemd resource limits.

## Paths

- Local development checkout: `/Users/shuoyang/北极星/ai-business-qa-bot-staging`
- Server runtime root: `/srv/ai-business-qa-bot-staging`
- Server application: `/srv/ai-business-qa-bot-staging/app`
- Server secrets: `/srv/ai-business-qa-bot-staging/config/.env`
- Staging Redis: `127.0.0.1:6380`

## Deployment contract

Deploy a reviewed Git commit as an archive. Do not copy the local `.env`,
virtual environment, test output, or a dirty working tree. The server `.env`
is maintained separately and must use a dedicated Feishu test application and
a dedicated read-only database account.

The staging slice limits the combined receiver, workers, and Redis to 180% CPU
and 6 GiB memory. Production services are not members of this slice.

Do not enable the staging receiver or workers until the staging `.env` is
complete and validated. Redis may be enabled independently.
