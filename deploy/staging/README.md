# ECS staging environment

The staging runtime is isolated from production by directory, Linux user,
virtual environment, Redis process, port, queues, configuration, logs, and
systemd resource limits.

## Paths

- Local development checkout: `/Users/shuoyang/北极星/ai-business-qa-bot-staging`
- Server runtime root: `/srv/ai-business-qa-bot-staging`
- Active server application: `/srv/ai-business-qa-bot-staging/current`
- Immutable releases: `/srv/ai-business-qa-bot-staging/releases/<git-revision>`
- First-generation rollback copy: `/srv/ai-business-qa-bot-staging/app`
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

## Release deployment

Create the artifact from a clean local Git commit:

```bash
revision=$(git rev-parse --short=12 HEAD)
git archive --format=tar.gz --output="/tmp/ai-bot-${revision}.tar.gz" HEAD
scp "/tmp/ai-bot-${revision}.tar.gz" root@115.190.197.231:/tmp/
```

On the server, deploy that exact revision:

```bash
/srv/ai-business-qa-bot-staging/current/deploy/staging/deploy-release.sh \
  "/tmp/ai-bot-${revision}.tar.gz" "$revision"
```

The script extracts into a new release directory, copies the currently known-good
virtual environment, validates imports and configuration, installs the systemd
units, atomically switches `current`, and restarts staging services. If the new
services fail to become active, it switches `current` back to the previous target
and restarts that version. It never modifies `config/.env`, Redis data, logs,
production services, or older releases.

Dependency changes require preparing and validating a new release-specific
virtual environment before the switch; do not silently upgrade unpinned packages
during deployment. Old releases are removed only after an explicit review.

Roll back to a retained revision with:

```bash
/srv/ai-business-qa-bot-staging/current/deploy/staging/rollback-release.sh \
  <retained-git-revision>
```

For the first migration only, `legacy-app` selects the preserved pre-release
`/srv/ai-business-qa-bot-staging/app` directory. The rollback script also uses
an atomic link switch and restores the previous target if the selected version
does not become active.
