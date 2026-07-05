#!/usr/bin/env bash
# linuxserver-style entrypoint: adjust the app user's UID/GID from PUID/PGID
# env vars, chown the writable volumes, then drop to that user and exec.

set -euo pipefail

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
UMASK_VALUE="${UMASK:-022}"
TZ="${TZ:-Etc/UTC}"

echo "[sn2md-worker-init] PUID=${PUID} PGID=${PGID} UMASK=${UMASK_VALUE} TZ=${TZ}"

# Reconcile the built-in 'app' account with the requested PUID/PGID. The
# `-o` flag lets us reuse an existing UID/GID if the host maps overlap
# (common on Unraid).
if [ "$(id -g app)" != "${PGID}" ]; then
  groupmod -o -g "${PGID}" app
fi
if [ "$(id -u app)" != "${PUID}" ]; then
  usermod -o -u "${PUID}" app
fi

# Timezone: link the requested TZ into /etc/localtime if the zoneinfo exists.
if [ -f "/usr/share/zoneinfo/${TZ}" ]; then
  ln -snf "/usr/share/zoneinfo/${TZ}" /etc/localtime
  echo "${TZ}" > /etc/timezone
fi

# Make sure the writable volumes are owned by app. `/app/.venv` is baked at
# build time and already `app`-owned, so it stays out of this list — no
# point paying the recursive chown on every start.  A very large vault could
# make `chown -R` slow; skip with `CHOWN_ON_START=false` if that bites.
#
# Errors are surfaced instead of swallowed with `2>/dev/null || true`. A
# read-only bind mount or a disk-full condition here means the app can't
# write anything at runtime — better to fail loudly at boot than to run a
# silently-broken container.
if [ "${CHOWN_ON_START:-true}" = "true" ]; then
  chown -R app:app /data /vault
fi

umask "${UMASK_VALUE}"

exec gosu app:app "$@"
