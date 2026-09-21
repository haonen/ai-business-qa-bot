#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <git-archive.tar.gz> <git-revision>" >&2
  exit 64
fi

archive=$1
revision=$2
root=/srv/ai-business-qa-bot-staging
release="$root/releases/$revision"
current="$root/current"
service_user=ai-bot-staging
services=(
  ai-bot-staging-workers
  ai-bot-staging-document-workers
  ai-bot-staging-receiver
)

if [[ ! $revision =~ ^[0-9a-f]+$ ]]; then
  echo "revision must contain only lowercase hexadecimal characters" >&2
  exit 64
fi

archive=$(realpath "$archive")
[[ -f $archive ]] || { echo "archive not found: $archive" >&2; exit 66; }
[[ -f $root/config/.env ]] || { echo "missing staging config" >&2; exit 78; }
[[ ! -e $release ]] || { echo "release already exists: $release" >&2; exit 73; }

previous_target=$root/app
if [[ -L $current ]]; then
  previous_target=$(readlink -f "$current")
fi
[[ -d $previous_target/.venv ]] || {
  echo "missing known-good virtual environment: $previous_target/.venv" >&2
  exit 78
}

install -d -o "$service_user" -g "$service_user" -m 0750 "$root/releases"
install -d -o "$service_user" -g "$service_user" -m 0750 "$release"
tar -xzf "$archive" -C "$release" --no-same-owner --no-same-permissions
cp -aL "$previous_target/.venv" "$release/.venv"
printf '%s\n' "$revision" > "$release/REVISION"
chown -R "$service_user:$service_user" "$release"

runuser -u "$service_user" -- "$release/.venv/bin/python" -m compileall -q "$release"
runuser -u "$service_user" -- "$release/.venv/bin/python" -m pip check
runuser -u "$service_user" -- env PYTHONPATH="$release" \
  "$release/.venv/bin/python" -c \
  'from dotenv import load_dotenv; load_dotenv("/srv/ai-business-qa-bot-staging/config/.env"); import lark_oapi, openai, pandas, pymysql, redis, rq, sqlalchemy; import bot.main'

install -o root -g root -m 0644 \
  "$release/deploy/staging/ai-bot-staging-workers.service" \
  /etc/systemd/system/ai-bot-staging-workers.service
install -o root -g root -m 0644 \
  "$release/deploy/staging/ai-bot-staging-document-workers.service" \
  /etc/systemd/system/ai-bot-staging-document-workers.service
install -o root -g root -m 0644 \
  "$release/deploy/staging/ai-bot-staging-receiver.service" \
  /etc/systemd/system/ai-bot-staging-receiver.service

next_link="$root/.current-$revision"
ln -s "$release" "$next_link"
mv -Tf "$next_link" "$current"
systemctl daemon-reload

if systemctl restart "${services[@]}" \
  && sleep 4 \
  && systemctl is-active --quiet "${services[@]}"; then
  echo "deployed revision $revision"
  echo "current=$release"
  echo "previous=$previous_target"
  exit 0
fi

echo "new release failed health checks; rolling back to $previous_target" >&2
rollback_link="$root/.current-rollback-$revision"
ln -s "$previous_target" "$rollback_link"
mv -Tf "$rollback_link" "$current"
systemctl daemon-reload
systemctl restart "${services[@]}"
exit 1
