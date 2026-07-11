# Unraid Runbook — sn2md-worker

Ops reference for running sn2md-worker on Unraid. Not read top-to-bottom
— jump to the section for the task at hand.

- Product/architecture context — [`product-brief.md`](./product-brief.md),
  [`technical-brief.md`](./technical-brief.md).
- Live-vs-deferred behavior lives in the technical brief (§13, §5.3);
  this file is host-side operations only.

Sections:
1. [First install](#1-first-install)
2. [Enable Obsidian Sync](#2-enable-obsidian-sync)
3. [Verify health](#3-verify-health)
4. [Upgrade the image](#4-upgrade-the-image)
5. [Rotate the service account key](#5-rotate-the-service-account-key)
6. [Recover from a schema break](#6-recover-from-a-schema-break)
7. [Rollback](#7-rollback)
8. [Backup and restore](#8-backup-and-restore)
9. [Troubleshooting](#9-troubleshooting)
10. [Startup model](#10-startup-model)

Every command in this doc runs on the Unraid host as `root` unless
noted. `${APPDATA}` is `/mnt/user/appdata/sn2md-worker` throughout.

---

## 1. First install

### 1.1 Prerequisites (one-time, off-Unraid)

Do these before touching the Unraid box — see
[`technical-brief.md#11-prerequisites`](./technical-brief.md#11-prerequisites)
for the click-through.

1. GCP service account JSON key downloaded (`service-account.json`).
2. Supernote sync folder in Drive shared to that service account (Viewer).
3. Gemini API key from `ai.google.dev` (`LLM_GEMINI_KEY`).
4. Public HTTPS hostname wired at your reverse proxy —
   e.g. `sn2md.<yourdomain>` → `http://<unraid-host>:8080`. The reverse
   proxy sits in front of Unraid; sn2md-worker itself does no TLS.
5. Note your intended host UID/GID for file ownership. On Unraid the
   `nobody:users` pair is `99:100`; adjust if you prefer a real user.

### 1.2 Appdata layout

```sh
export APPDATA=/mnt/user/appdata/sn2md-worker
mkdir -p ${APPDATA}/{data,secrets}
```

`/vault` mounts an Obsidian vault directory — the same one Obsidian
itself reads (set up in [§2](#2-enable-obsidian-sync)). Point at
wherever that lives — e.g. `/mnt/user/obsidian/vault`. Do NOT put
the vault under `appdata/sn2md-worker` — that couples deletion of the
worker to loss of your notes.

Layout summary:

| Host path                              | Container path                    | Mode | Owner (PUID:PGID) |
|----------------------------------------|-----------------------------------|------|-------------------|
| `${APPDATA}/data`                      | `/data`                           | rw   | writable          |
| `${APPDATA}/secrets`                   | `/secrets`                        | ro   | readable          |
| `/mnt/user/obsidian/vault`             | `/vault`                          | rw   | writable          |

### 1.3 Install the service account key

```sh
cp /path/to/downloaded/service-account.json ${APPDATA}/secrets/service-account.json
chmod 600 ${APPDATA}/secrets/service-account.json
```

The container mounts `/secrets` read-only; ownership doesn't need to
match PUID, but tight `600` on the host keeps it from being world-readable.

### 1.4 Pull the image

The release workflow publishes multi-arch (`linux/amd64` + `linux/arm64`)
images on every push to `main` and every semver tag.

```sh
docker pull ghcr.io/macdonaldr93/sn2md-worker:latest
```

For production, prefer a pinned tag (`:v0.1.0` or `:sha-abc1234`) over
`:latest` — `latest` moves every time main merges. Record the tag you
installed; you'll need it for rollback.

### 1.5 Create the container (Unraid template)

Unraid's Docker tab → Add Container. Fill in:

| Field | Value |
|---|---|
| Name | `sn2md-worker` |
| Repository | `ghcr.io/macdonaldr93/sn2md-worker:latest` (or the pinned tag) |
| Network Type | `Bridge` |
| Console shell | `bash` |
| Restart Policy | `unless-stopped` |

Port mapping:

| Host port | Container port | Type | Notes |
|---|---|---|---|
| `8080` | `8080` | TCP | Reachable by the reverse proxy only |

Volumes:

| Container path | Host path | Access mode |
|---|---|---|
| `/data` | `/mnt/user/appdata/sn2md-worker/data` | Read/Write |
| `/vault` | `/mnt/user/obsidian/vault` | Read/Write |
| `/secrets` | `/mnt/user/appdata/sn2md-worker/secrets` | Read Only |

Environment variables:

| Name | Value | Notes |
|---|---|---|
| `PUID` | `99` | Or your host user's UID |
| `PGID` | `100` | Or your host user's GID |
| `TZ` | `America/Toronto` | Any IANA zone |
| `UMASK` | `022` | Optional |
| `LLM_GEMINI_KEY` | (secret) | Gemini API key |
| `SN2MD_WORKER__DRIVE__SOURCE_FOLDER_ID` | (Drive folder id) | The Supernote sync folder id |
| `SN2MD_WORKER__WEBHOOK__URL` | `https://sn2md.yourdomain.tld/webhooks/drive` | Public HTTPS URL to your reverse proxy |

Leave `SN2MD_WORKER__DATABASE__URL`, `SN2MD_WORKER__VAULT__ROOT_PATH`,
and `SN2MD_WORKER__GOOGLE__APPLICATION_CREDENTIALS` unset — the image
bakes container-canonical values. See [`CLAUDE.md`](../CLAUDE.md) item 6.

Optional tuning knobs (all have sane defaults, only add if you need to
change them):

| Name | Default | Purpose |
|---|---|---|
| `SN2MD_WORKER__OBSERVABILITY__LOG_LEVEL` | `INFO` | Set `DEBUG` to trace individual Drive calls |
| `SN2MD_WORKER__QUEUE__CONVERT_CONCURRENCY` | `2` | Parallel Gemini conversions |
| `CHOWN_ON_START` | `true` | Set `false` if `chown -R` on `/vault` is slow at boot |

Click **Apply**. First boot pulls the image and starts the container.

### 1.6 Reverse proxy route

Route `POST https://sn2md.<yourdomain>/webhooks/drive` →
`http://<unraid-host>:8080/webhooks/drive`. Terminate TLS at the proxy.
Do NOT expose `/healthz`, `/readyz`, or `/status` publicly — keep those
LAN-only or behind proxy auth. See
[`technical-brief.md#8-webhook`](./technical-brief.md#8-webhook-drivewebhookpy)
for the request shape.

Sanity-check the route from off-network:

```sh
curl -X POST -H 'X-Goog-Channel-Id: probe' https://sn2md.yourdomain.tld/webhooks/drive
# Expect 401 Unauthorized (the handler rejects requests without a real channel token).
# 502/504 means the reverse proxy isn't reaching the container.
# 200 is wrong — it means auth is off.
```

### 1.7 First-boot success signals

From the Unraid host (LAN-only endpoints):

```sh
curl -sf http://<unraid-host>:8080/healthz    # 200 {"status":"ok"}
curl -sf http://<unraid-host>:8080/readyz     # 200 when watch channel active
curl -s  http://<unraid-host>:8080/status | jq .backfill
```

Log lines to watch for in order (`docker logs -f sn2md-worker | jq .`):

1. `dbos_init`
2. `app_schema_ready`
3. `datasource_ready`
4. `drive_client_ready` (`service_account=<...iam.gserviceaccount.com>`)
5. `dbos_launched`
6. `queues_registered` → `schedules_registered`
7. `renew_watch_channel_created` (first channel — this proves the
   reverse proxy is reachable from Google)
8. `backfill_enqueued` → `backfill_succeeded` (may take a while on a
   folder with many existing notes; each note runs through Gemini)

If step 7 doesn't appear, the reverse proxy route is broken — see
[Troubleshooting](#9-troubleshooting).

---

## 2. Enable Obsidian Sync

sn2md-worker only writes Markdown into `/vault`. To read and edit
those notes on your phone and laptop, you need Obsidian's paid Sync
service, plus something server-side that watches the vault dir and
pushes changes into Sync. On Unraid the fit is a small container
running Obsidian's headless sync CLI (`obsidian-headless`) — much
lighter than a full desktop Obsidian in a browser, and purpose-built
for exactly this "server writes files, push them out" case.

`obsidian-headless` is in open beta as of 2026-07 — see
[`obsidian.md/help/sync/headless`](https://obsidian.md/help/sync/headless).

Prerequisites:
- An Obsidian account with a paid Sync subscription
  (`obsidian.md/sync`).
- A remote vault to sync into. You can either create one from a
  desktop or mobile Obsidian install first (recommended — the desktop
  UI walks you through picking an encryption password), or create it
  from headless later with `ob sync-create-remote`. Either way, note
  the vault name and its encryption password.

### 2.1 Pull the sync image

The release workflow publishes obsidian-sync alongside sn2md-worker
on every push to `main` and every semver tag, multi-arch
(`linux/amd64` + `linux/arm64`):

```sh
docker pull ghcr.io/macdonaldr93/sn2md-worker/obsidian-sync:latest
```

Same tag / rollback story as sn2md-worker (§1.4, §7) — prefer a pinned
tag (`:v0.1.0` or `:sha-abc1234`) over `:latest` in production, and
record the tag you installed. The Dockerfile lives at
[`docker/obsidian-sync/Dockerfile`](../docker/obsidian-sync/Dockerfile)
if you want to inspect or build it locally.

### 2.2 Add the container

Docker tab → Add Container:

| Field | Value |
|---|---|
| Name | `obsidian-sync` |
| Repository | `ghcr.io/macdonaldr93/sn2md-worker/obsidian-sync:latest` (or a pinned tag) |
| Network Type | `Bridge` |
| Restart Policy | `unless-stopped` |

No port mapping — the container makes outbound HTTPS only, to
Obsidian's Sync servers.

Volumes:

| Container path | Host path | Access mode |
|---|---|---|
| `/config` | `/mnt/user/appdata/obsidian-sync` | Read/Write |
| `/vault` | `/mnt/user/obsidian/vault` (same dir as sn2md-worker) | Read/Write |

Environment variables (linuxserver-style baseimage plus obsidian-sync's
own auto-config contract):

| Name | Value | Notes |
|---|---|---|
| `PUID` | `99` (same as sn2md-worker) | Baseimage reshapes `abc` to this UID and chowns `/config` on boot |
| `PGID` | `100` (same as sn2md-worker) | Must match sn2md-worker so both write the vault as the same user |
| `TZ` | `America/Toronto` | Any IANA zone |
| `OBSIDIAN_EMAIL` | your Obsidian account email | Mark secret |
| `OBSIDIAN_PASSWORD` | your Obsidian account password | Mark secret |
| `OBSIDIAN_VAULT` | remote vault name (or ID) from your Obsidian account | The vault you created from desktop/mobile Obsidian |
| `OBSIDIAN_ENCRYPTION_PASSWORD` | E2E encryption password for that vault | Mark secret |
| `OBSIDIAN_MFA` | 6-digit 2FA code | Only needed on very first boot if your account has 2FA on; can be removed after successful login |

Click Apply.

### 2.3 First boot behaviour

The s6 service is entirely self-configuring. On boot it, as `abc` with
`HOME=/config`:

1. Runs `ob login --email --password [--mfa]` if `/config/.config/obsidian-headless/auth_token`
   is missing.
2. Runs `ob sync-setup --vault "$OBSIDIAN_VAULT" --path /vault --password "$OBSIDIAN_ENCRYPTION_PASSWORD"`
   if `ob sync-status --path /vault` reports no configuration.
3. Runs `ob sync --continuous --path /vault`.

If a required env var is missing when it's needed, the container logs a
clear message and idles (no crash-loop). Tail the logs to see progress:

```sh
docker logs -f obsidian-sync
```

Do **not** run `ob login` / `ob sync-setup` manually via `docker exec`
or the Unraid console — the auto-config flow handles them, and any
files an interactive root shell writes into `/config/.config/` will be
root-owned and unreadable by the `abc` service. If you ever need to
reset from scratch, stop the container, `rm -rf /mnt/user/appdata/obsidian-sync/*`,
and restart. Auto-config will re-pair the vault from env.

### 2.4 Pair your devices

On each phone and laptop:

1. Install Obsidian, sign in with the same account.
2. Create a new vault → Sync → connect to the same remote vault.
3. Enter the encryption password. Wait for the initial download.

Once paired, edits on any device propagate through Sync. sn2md-worker's
writes into `/vault` on Unraid are picked up by `ob sync --continuous`
and pushed out — new notes should appear on your other devices within
seconds.

### 2.5 Ownership sanity check

sn2md-worker and obsidian-sync must both write the vault as the same
user, or one will produce files the other can't read:

```sh
ls -la /mnt/user/obsidian/vault | head
# All files should show the same owner. If sn2md-worker files are
# 99:100 but obsidian-sync's are e.g. 1000:1000, fix the `--user`
# flag on the obsidian-sync template, restart, and normalize
# existing files: `chown -R 99:100 /mnt/user/obsidian/vault`.
```

### 2.6 Do not run desktop Obsidian on the vault dir

Obsidian warns explicitly against mixing headless sync with
desktop-app Sync on the same on-disk vault — they race for the same
sync state. Only your remote devices (phone, laptop) should run the
desktop/mobile Obsidian; the Unraid vault dir is headless-only.

---

## 3. Verify health

Three endpoints, all on port 8080:

| Endpoint | Purpose | Meaning |
|---|---|---|
| `GET /healthz` | Liveness | 200 while the process is up |
| `GET /readyz` | Readiness | 200 if an active `drive_watch_channels` row exists with `expires_at > now`. 503 otherwise. In dev mode (empty `WEBHOOK__URL`) always 200. |
| `GET /status` | Snapshot | JSON with recent conversions, failures, queue depths, backfill status |

Quick check the pipeline is working end-to-end:

```sh
# Edit a .note on the Supernote → wait ~30s → look at logs:
docker logs --since 2m sn2md-worker | jq 'select(.event | test("^(drive_webhook|poll_changes|convert_note)"))'

# Or via /status:
curl -s http://<unraid-host>:8080/status | jq '.recent_conversions[0]'
```

Expected log sequence: `drive_webhook_notification` → `poll_changes_enqueued`
→ `convert_note_started` → `convert_note_succeeded pages=N cache_hits=N-1`.

---

## 4. Upgrade the image

The image tag drives everything — Unraid will pull the new manifest if
you click "Force update" on the container, or you can do it explicitly:

```sh
# 1. Note the currently running tag/digest for rollback.
docker inspect sn2md-worker --format '{{.Config.Image}} @ {{index .RepoDigests 0}}'

# 2. Optionally snapshot state before upgrading. Safe because SQLite is
#    tiny; skip only if you're comfortable rebuilding from Drive.
tar -czf /mnt/user/backups/sn2md-worker-$(date -u +%Y%m%dT%H%M%SZ).tgz \
  -C /mnt/user/appdata/sn2md-worker data

# 3. Pull and restart.
docker pull ghcr.io/macdonaldr93/sn2md-worker:latest
docker stop sn2md-worker && docker rm sn2md-worker
# ...then click Apply on the Unraid template, which recreates the
# container from the updated image with the same volumes/env.

# 4. Watch the boot sequence in §1.7 replay. `backfill_succeeded` on the
#    new version is your green light.
```

Cross-version state is safe as long as no schema changed. Schema changes
are announced in commit messages and covered by
[§6 — Recover from a schema break](#6-recover-from-a-schema-break).

---

## 5. Rotate the service account key

Rotation is stateless — the key file drives client init; nothing in
SQLite depends on the key.

```sh
# 1. Create a new key in GCP Console (Service Accounts → Keys → Add Key).
# 2. Copy it into place (overwrite the existing file):
cp /path/to/new-key.json ${APPDATA}/secrets/service-account.json
chmod 600 ${APPDATA}/secrets/service-account.json

# 3. Restart so DriveClient re-reads the file.
docker restart sn2md-worker

# 4. Confirm the boot log shows the same service_account email as before.
docker logs --since 30s sn2md-worker | jq 'select(.event=="drive_client_ready")'

# 5. Only after step 4 succeeds — delete the old key in GCP Console.
```

Rotating the key does NOT invalidate the active Drive watch channel;
the channel is tied to the project, not the key. No re-creation needed.

---

## 6. Recover from a schema break

This project uses `Base.metadata.create_all` only — no Alembic. New
columns / indexes only apply to a fresh SQLite file. Recovery path
(from [`CLAUDE.md`](../CLAUDE.md) item 3):

```sh
# 1. Stop the container.
docker stop sn2md-worker

# 2. (Optional) Snapshot the broken state for post-mortem.
mv ${APPDATA}/data/sn2md-worker.sqlite ${APPDATA}/data/sn2md-worker.broken-$(date -u +%s).sqlite

# 3. Nuke SQLite state — startup backfill will re-populate from Drive.
rm -f ${APPDATA}/data/sn2md-worker.sqlite*   # also removes -wal / -shm

# 4. Start the container. `init_schema` runs on the empty file, then
#    `backfill_enqueued` walks the entire Drive folder.
docker start sn2md-worker

# 5. Watch backfill complete. Duration ≈ notes × ~7.5s/page.
docker logs -f sn2md-worker | jq 'select(.event | test("^backfill"))'
```

`/vault` is not touched. `convert_note` short-circuits when the source
md5 matches the last conversion, so the vault stays as-is unless a note
was actually re-edited.

If you want to force re-conversion (Gemini prompt changed, for example),
also `rm -rf /mnt/user/obsidian/vault/<Supernote-subtree>` before
starting. Backfill will re-run every page through Gemini.

---

## 7. Rollback

If a new image behaves badly and state is intact, roll the image tag
back:

```sh
# 1. Note the failing tag for the post-mortem.
docker inspect sn2md-worker --format '{{.Config.Image}} @ {{index .RepoDigests 0}}'

# 2. Stop and remove the current container.
docker stop sn2md-worker && docker rm sn2md-worker

# 3. Point the Unraid template's Repository field at the previous known-good
#    tag (e.g. `ghcr.io/macdonaldr93/sn2md-worker:v0.1.0` or the sha- tag
#    you noted before the upgrade). Click Apply.

# 4. Confirm the boot sequence in §1.7. `drive_client_ready` +
#    `dbos_launched` on the old image = you're back.
```

If state is ALSO broken (rare — schema-changing upgrade rolled back),
combine §7 with §6: nuke the SQLite file after switching images.

---

## 8. Backup and restore

### What's precious

- `${APPDATA}/data/sn2md-worker.sqlite*` — DBOS workflow state + our
  tables. Losing this loses in-flight workflows and last-poll cursor;
  boot-time `backfill` reconstructs conversion records from Drive +
  the vault.
- `${APPDATA}/secrets/service-account.json` — one file. If lost,
  create a new GCP key and follow §5.
- `/mnt/user/obsidian/vault/**` — your actual notes. Back this up
  independently (Obsidian Sync from [§2](#2-enable-obsidian-sync) +
  Unraid's normal backup routine). sn2md-worker treats the vault as
  an output, not a source of truth.

### Snapshot

```sh
tar -czf /mnt/user/backups/sn2md-worker-$(date -u +%Y%m%dT%H%M%SZ).tgz \
  -C /mnt/user/appdata sn2md-worker
```

Safe to run against a live container — SQLite in WAL mode tolerates
concurrent reads. If you want a truly quiet snapshot, `docker stop
sn2md-worker` first, then start after the tar completes.

### Restore

```sh
# 1. Stop the container.
docker stop sn2md-worker

# 2. Restore the tarball over the appdata dir.
rm -rf ${APPDATA}
tar -xzf /mnt/user/backups/sn2md-worker-<timestamp>.tgz -C /mnt/user/appdata

# 3. Start.
docker start sn2md-worker
```

Any workflow that was in-flight when the snapshot was taken resumes
from its last DBOS checkpoint.

---

## 9. Troubleshooting

### Container comes up but `/readyz` is 503

Boot is deterministic — a bad service account or unreachable Drive
degrades the container into a live-but-not-ready state instead of
crashlooping. Check `/status.startup` first:

```sh
curl -s http://<unraid-host>:8080/status | jq '.startup'
# {
#   "drive_client": "failed",
#   "seed_cursor": "deferred",
#   "ensure_channel": "deferred",
#   "backfill_enqueue": "deferred",
#   "last_error": "drive_client: invalid credentials file /secrets/service-account.json (MalformedError): ..."
# }
```

Map the failing step to a fix:

| `startup` field | `"failed"` cause | Fix |
|---|---|---|
| `drive_client` | `secrets/service-account.json` is malformed or unreadable | Re-download from GCP, replace, `docker restart` |
| `drive_client` == `"deferred"` and log says `drive_client_skipped_no_credentials` | Volume mount wrong — no file at `/secrets/service-account.json` | Fix the Unraid volume config; confirm with `docker exec sn2md-worker ls /secrets/` |
| `seed_cursor` | Drive rejected `getStartPageToken` — usually bad credentials (`RefreshError`) or network to `oauth2.googleapis.com` down | Verify key by regenerating (§5). If key is good, check the container's egress network |
| `ensure_channel` | `changes().watch()` failed — most often the Drive folder isn't shared to the service account, or the webhook URL isn't reachable from Google | Share the folder to the SA email; verify `curl -X POST https://sn2md.<domain>/webhooks/drive` returns 401 from off-network |
| `backfill_enqueue` | Rare — DBOS enqueue error | Check DBOS logs; typically resolves on restart |

The daily `renew_watch_channel` cron and the next `docker restart` both
re-attempt the failing steps. Recovery is automatic once the underlying
issue is fixed.

### Container actually crashloops (rare)

Should only happen for out-of-scope failures — SQLite path unwritable,
DBOS init failed, an uncaught programming error. In that order:

```sh
docker logs sn2md-worker | jq .
```

| Traceback pattern | Cause | Fix |
|---|---|---|
| `sqlite3.OperationalError: unable to open database file` | `/data` isn't writable by PUID/PGID | Check host-side ownership of `${APPDATA}/data`; `chown` if needed |
| Any other unhandled exception during boot | Programming bug | Grab logs, open an issue; roll back per §7 |

### `/readyz` stays 503 forever

- `SN2MD_WORKER__WEBHOOK__URL` is set but no `renew_watch_channel_created`
  log line. Google can't reach your webhook.
- Confirm from off-network: `curl -X POST -sv https://sn2md.<domain>/webhooks/drive`.
  Expect 401 Unauthorized. Anything else (502, 504, cert error) means
  the reverse proxy is misrouted or TLS is broken.
- Check your reverse proxy's access log for a 401 from Google — that
  proves reachability. If Google's POSTs never arrive, the DNS →
  reverse proxy → container path is broken.

### `/readyz` was 200 and flipped to 503

- The Drive watch channel expired. Cron runs daily at 06:00 UTC to
  renew, so a fresh expiry means the last cron either didn't fire or
  errored.
- Immediate recovery: `docker restart sn2md-worker` — `ensure_active_channel_if_ready`
  creates a fresh channel on boot.
- Follow-up: search logs for `renew_watch_channel_failed` in the last
  36 hours to see why the scheduled renewal failed.

### Notes aren't converting

- Check `/status` — `recent_failures` will show the last 20 conversion
  errors with `last_error`.
- `queue_depth.convert_queue > 0` with no progress means Gemini is
  stalled or the concurrency limit is holding back work. Check for
  `convert_note_failed` events. Rate-limit errors from Gemini surface
  as retriable transients.
- Verify the Supernote is actually syncing to Drive (the Supernote app
  on the tablet shows sync progress). No sync = no changes for the
  worker to see.

### Watch channel expired while worker was down

Boot handles this automatically:
`ensure_active_channel_if_ready` sees the expired row, stops the old
channel, creates a fresh one, and enqueues a recovery `poll_changes`
that catches anything that changed while offline. Just start the
container — no manual steps.

### Container exited with `Killed` (OOM)

Downloads stream in 4 MB chunks so `.note` file size shouldn't OOM,
but Gemini's Python client can spike briefly. Bump the container's
memory limit in the Unraid template if you consistently OOM. As a
lower-risk tuning, drop `SN2MD_WORKER__QUEUE__CONVERT_CONCURRENCY`
from `2` to `1`.

---

## 10. Startup model

The container's boot sequence is designed to be deterministic and
degrade-friendly. Any failure that can only be diagnosed from outside
the container (bad credentials, unshared folder, network to Google
down) surfaces on `/status.startup` instead of crashing the process:

```
{
  "drive_client":     "ok" | "deferred" | "failed",
  "seed_cursor":      "ok" | "deferred" | "failed",
  "ensure_channel":   "ok" | "deferred" | "failed",
  "backfill_enqueue": "ok" | "deferred" | "failed",
  "last_error":       "<step>: <message>" | null
}
```

Meaning:

- `"ok"` — step ran and returned successfully.
- `"deferred"` — step was intentionally skipped because a prerequisite
  wasn't ready. In practice that's always `drive_client` — if the
  service-account file is missing entirely, the container comes up in
  dev mode and every subsequent Drive-touching step is deferred.
- `"failed"` — step tried and errored. `last_error` carries the first
  failing step's message; the log has structured events
  (`boot_step_failed step=<name>`, `drive_client_init_failed`) with
  full detail.

`/healthz` remains 200 through all of this — the process is up and
serving HTTP even in a fully-degraded startup. `/readyz` returns 503
until an active `drive_watch_channels` row exists (see [§3](#3-verify-health)).

Recovery is automatic. The daily `renew_watch_channel` cron re-attempts
channel creation from a healthy DriveClient at 06:00 UTC. `docker restart`
re-runs the entire boot sequence — if you fixed the underlying config
(replaced the key, shared the folder), the fields flip to `"ok"` on the
next boot.

### Non-negotiable: single process

DBOS + SQLite state lives in one file. The image runs single-process by
design ([`CLAUDE.md`](../CLAUDE.md) item 5, §5.1 in the technical
brief). Do NOT set `workers>1` or scale-out on Unraid — the singletons
race, and the schedule/queue registrations would run twice.
