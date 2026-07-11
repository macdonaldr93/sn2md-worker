# Obsidian headless sync CLI. See docs/unraid-runbook.md §2.
#
# Runs as a fixed non-root user 99:100 (Unraid's `nobody:users`). Because
# the uid/gid are baked at build time, there is no PUID/PGID reshaping at
# start, no root-owned files leaking into the volumes, and no s6/init
# gymnastics. The two writable bind mounts (/config, /vault) must be
# owned by 99:100 on the host - see the runbook.
FROM node:22-alpine

ARG APP_UID=99
ARG APP_GID=100

# The base image already ships a group at gid 100 (`users`) and a user at
# uid 1000 (`node`). We reuse the existing gid-100 group if present so
# `adduser -G` resolves; a fresh `app` group is created only when the
# requested APP_GID is unused.
RUN set -eux \
 && apk --no-cache upgrade \
 && apk add --no-cache tini tzdata \
 && npm install -g obsidian-headless \
 && npm cache clean --force \
 && rm -rf /tmp/* \
 && if ! getent group "${APP_GID}" >/dev/null; then addgroup -g "${APP_GID}" -S app; fi \
 && APP_GROUP="$(getent group "${APP_GID}" | cut -d: -f1)" \
 && adduser -S -D -H -u "${APP_UID}" -G "${APP_GROUP}" app \
 && mkdir -p /config /vault \
 && chown "${APP_UID}:${APP_GID}" /config /vault

COPY entrypoint.sh /usr/local/bin/obsidian-sync-entrypoint
RUN chmod +x /usr/local/bin/obsidian-sync-entrypoint

# `ob` writes auth/state under $HOME/.config. Pin HOME to /config so the
# volume is the only place any state ever lands.
ENV HOME=/config

USER ${APP_UID}:${APP_GID}
VOLUME ["/config", "/vault"]

ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/obsidian-sync-entrypoint"]
