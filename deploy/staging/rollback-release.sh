#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <git-revision|legacy-app>" >&2
  exit 64
fi

revision=$1
root=/srv/ai-business-qa-bot-staging
current="$root/current"
services=(
  ai-bot-staging-workers
  ai-bot-staging-document-workers
  ai-bot-staging-receiver
)

[[ -L $current ]] || { echo "current release link is missing" >&2; exit 78; }
previous_target=$(readlink -f "$current")

if [[ $revision == legacy-app ]]; then
  target=$root/app
elif [[ $revision =~ ^[0-9a-f]+$ ]]; then
  target=$root/releases/$revision
else
  echo "revision must contain only lowercase hexadecimal characters" >&2
  exit 64
fi

[[ -d $target/.venv ]] || { echo "invalid rollback target: $target" >&2; exit 66; }
[[ -f $target/bot/main.py ]] || { echo "rollback target has no bot/main.py" >&2; exit 66; }

if [[ $(readlink -f "$target") == "$previous_target" ]]; then
  echo "already running $target"
  exit 0
fi

next_link="$root/.current-rollback-$(date +%s)"
ln -s "$target" "$next_link"
mv -Tf "$next_link" "$current"
systemctl daemon-reload

if systemctl restart "${services[@]}" \
  && sleep 4 \
  && systemctl is-active --quiet "${services[@]}"; then
  echo "rolled back current=$target"
  echo "previous=$previous_target"
  exit 0
fi

echo "rollback target failed health checks; restoring $previous_target" >&2
restore_link="$root/.current-restore-$(date +%s)"
ln -s "$previous_target" "$restore_link"
mv -Tf "$restore_link" "$current"
systemctl daemon-reload
systemctl restart "${services[@]}"
exit 1
