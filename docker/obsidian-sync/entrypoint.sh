#!/bin/sh
# obsidian-sync entrypoint: env-driven auto-config for `obsidian-headless`.
#
# Runs as a fixed non-root user (uid 99, gid 100 - Unraid's nobody:users)
# baked into the image. No PUID/PGID reshaping, no s6, no root drop.
# HOME is fixed to /config in the Dockerfile so `ob` writes auth/state
# into the /config volume.

set -eu

log() { echo "obsidian-sync: $*" >&2; }

idle_with_message() {
    log "$1"
    log "set the required env vars and restart the container"
    exec sleep infinity
}

if [ ! -s /config/.config/obsidian-headless/auth_token ]; then
    if [ -z "${OBSIDIAN_EMAIL:-}" ] || [ -z "${OBSIDIAN_PASSWORD:-}" ]; then
        idle_with_message "not logged in; OBSIDIAN_EMAIL / OBSIDIAN_PASSWORD not set"
    fi
    log "logging in as $OBSIDIAN_EMAIL"
    set -- --email "$OBSIDIAN_EMAIL" --password "$OBSIDIAN_PASSWORD"
    if [ -n "${OBSIDIAN_MFA:-}" ]; then
        set -- "$@" --mfa "$OBSIDIAN_MFA"
    fi
    if ! ob login "$@"; then
        idle_with_message "ob login failed"
    fi
fi

if ! ob sync-status --path /vault >/dev/null 2>&1; then
    if [ -z "${OBSIDIAN_VAULT:-}" ] || [ -z "${OBSIDIAN_ENCRYPTION_PASSWORD:-}" ]; then
        idle_with_message "/vault not configured; OBSIDIAN_VAULT / OBSIDIAN_ENCRYPTION_PASSWORD not set"
    fi
    log "configuring /vault against remote vault '$OBSIDIAN_VAULT'"
    if ! ob sync-setup --vault "$OBSIDIAN_VAULT" --path /vault --password "$OBSIDIAN_ENCRYPTION_PASSWORD"; then
        idle_with_message "ob sync-setup failed"
    fi
fi

# obsidian-headless treats /vault/.obsidian/.sync.lock/ with an mtime under
# 5 seconds old as "another instance is running." A back-to-back sync-setup
# then sync (as we do above) trips that check, and a hard container kill
# leaves the dir behind. This container is the only ob sync runner in the
# deployment, so we can safely clear a stale lock before starting.
# Upstream: https://github.com/obsidianmd/obsidian-headless/issues/4
if [ -d /vault/.obsidian/.sync.lock ]; then
    log "clearing stale /vault/.obsidian/.sync.lock before starting sync"
    rm -rf /vault/.obsidian/.sync.lock
fi

log "starting continuous sync for /vault"
exec ob sync --continuous --path /vault
