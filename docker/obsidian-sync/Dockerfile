# Obsidian headless sync CLI. See docs/unraid-runbook.md §2.
# Alpine 3.24 ships nodejs 24.x (Node LTS Jod) — verify before bumping.
FROM ghcr.io/linuxserver/baseimage-alpine:3.24

RUN apk --no-cache upgrade \
 && apk add --no-cache nodejs npm \
 && npm install -g obsidian-headless \
 && npm cache clean --force \
 && apk del npm \
 && rm -rf /root/.npm /tmp/*

# `ob` writes auth/state under $HOME/.config. The s6 run script sets
# HOME=/config only for the abc-supervised service invocation, so root
# shells (e.g. Unraid console) don't accidentally write into the volume
# and leave root-owned files the service can't read.
VOLUME ["/config", "/vault"]

COPY root/ /
RUN chmod +x /etc/s6-overlay/s6-rc.d/svc-obsidian-sync/run
