# sn2md-worker — Product Brief

## Goal

Automatically convert Supernote `.note` files into Markdown for Obsidian as
they arrive in a Google Drive folder, using
[sn2md](https://github.com/dsummersl/sn2md) with Gemini as the LLM backend.
Runs continuously as a Docker container on my Unraid server.

## High-level flow

1. Supernote syncs `.note` files into a source folder on Google Drive.
2. Google Drive push-notification webhook hits the worker on Unraid at
   `POST /webhooks/drive`.
3. Worker verifies the channel + token, then enqueues a `poll_changes`
   DBOS workflow which walks the `changes.list` cursor and enqueues
   `convert_note` for each affected `.note` file. A scheduled fallback
   poll (default every 5 minutes) runs the same path even when no push
   arrives, so the pipeline no longer depends solely on Google
   delivering notifications.
4. `convert_note` downloads the file, extracts each page's PNG, calls
   Gemini 2.5 Pro only for pages whose hash has changed since the last
   conversion (via the `page_conversions` cache table), and writes one
   Markdown file per page plus an `index.md` under a per-note folder
   that mirrors the Drive layout.
5. Destination is a local vault directory on Unraid; a companion
   `obsidian-sync` container runs Obsidian's headless sync CLI against
   the same vault dir and pushes changes into Obsidian Sync, which
   fans them out to phone/laptop.

## Decisions

### Language & tooling

- **Language**: Python 3.12+. Drives sn2md's per-page extraction and
  transcription primitives so per-page LLM output can be cached.
- **Packaging**: `uv` for venv, dependencies, lockfile, and runners.
- **Lint/format**: `ruff`.
- **Type checking**: `mypy`.
- **Pre-commit hooks**: ruff (check + format) and mypy on every commit.
- **CI**: GitHub Actions runs lint + type check + tests on push/PR;
  a separate `release.yml` publishes multi-arch (`linux/amd64` +
  `linux/arm64`) images to GHCR on push to main and semver tags.

### Drive integration

- **Auth**: **Google Cloud service account**. The personal Drive user shares
  the Supernote source folder to the service account's `client_email` as
  Viewer. No user OAuth, no expiring refresh tokens. Destination is a local
  path — no Drive access needed there.
- **Change detection**: Drive API `changes.watch` push notifications
  (webhooks) → worker's HTTPS endpoint on Unraid. Notifications only signal
  "something changed" — the worker then polls `changes.list` (with a saved
  `pageToken`) to get actual change details.
- **Catch-up on restart**: `backfill` workflow enqueues on startup — it
  walks the source folder tree and enqueues `convert_note` for anything
  missing from `conversion_records` or whose md5 doesn't match.
- **Fallback poller**: a scheduled `poll_changes` cron (default every
  5 minutes, configurable) walks the change feed even when no push
  notification arrives, so a dropped webhook self-heals within minutes
  instead of waiting for a restart. Renewing a watch channel also
  triggers a catch-up poll to cover changes during the channel swap.
- **Public URL**: existing reverse proxy on Unraid handles TLS + routing to
  the worker container. Webhook path: `/webhooks/drive`.
- **Watch channel renewal**: Max TTL is 7 days (604800s) for
  `changes.watch` with no automatic renewal on Drive's side. We omit
  `expiration` from the watch request so Drive assigns its maximum TTL
  (avoiding host-clock skew), and a daily DBOS cron renews the channel
  when within 48 hours of expiry or when the configured webhook URL
  changes.
- **Webhook authenticity**: `token` field set at channel creation (32-byte
  random hex), echoed as `X-Goog-Channel-Token` on every notification.
  Worker verifies token + `X-Goog-Channel-Id` against the active channel
  record.
- **Debounce** (deferred by design): per-file size/md5 stability polling
  is defined in the schema (`debounce_state`), but Drive only publishes
  notifications for completed uploads so `poll_changes` enqueues
  `convert_note` directly.
- **Update, move, and rename handling**: at the note level, conversion
  is skipped when the source `.note` md5 matches the last successful
  conversion. Below that, the hash-first per-page cache
  (`page_conversions` table + rendered PNG md5) calls Gemini only for
  pages whose PNG hash has not been transcribed before — editing one
  page or inserting a page mid-note only transcribes the new or modified
  page, while preserving existing pages' `created` frontmatter dates.
  Identity for a note is its Drive path + filename (`logical_key`), not
  its `fileId` (see technical-brief §4a). When a note is moved or
  renamed in Drive, cached pages are seeded from the prior vault folder
  and the old folder and DB rows are cleaned up once conversion
  completes. Gemini rate-limit windows are ridden out in place with
  extended backoff; anything that still fails is re-driven by the daily
  backfill sweep.
- **Deletion handling**: if a `.note` is deleted from Drive (or moved
  outside the source folder), the corresponding Markdown + assets are
  removed from the vault. The delete workflow first checks whether a
  live replacement file with the same name exists in the parent folder
  to protect against Supernote's replace-on-sync pattern.
- **Ingestion seam**: conversion workflows read notes through a
  source-neutral `NoteSource` protocol; Google Drive is its current
  implementation, keeping a future local-folder or direct-device source
  a configuration choice rather than a workflow rewrite.

### Backfill & idempotency

- **Backfill on startup & daily sweep**: scans the source folder and
  enqueues `convert_note` for any `.note` lacking a matching successful
  conversion record (with queue deduplication so duplicate triggers do
  not stack redundant runs).
- **Idempotency key**: source file hash (md5 from Drive metadata) → success
  record persisted in SQLite.

### LLM / sn2md

- **Model**: `gemini/gemini-3.8-flash` (via `llm-gemini` plugin ≥0.35).
- **Prompt**: sn2md's default Markdown template augmented with stricter
  ASCII-only Mermaid and CommonMark escaping guidance (overridable via
  `sn2md.prompt` in config).
- **API key**: `LLM_GEMINI_KEY` env var or `sn2md.api_key` in config.
- **Integration surface**: uses sn2md's per-page image extraction and
  LLM transcription primitives so each page's output can be cached by
  PNG hash and emitted as its own Markdown file.

### Queue & durability

- **Framework**: [DBOS Transact](https://dbos.dev) for Python with the
  SQLite backend.
- **SQLite file**: single file on a mounted volume shared by DBOS
  workflow tables and application tables, tuned with WAL mode,
  `IMMEDIATE` transaction isolation, and a 30s busy timeout.
- **Confirmed constraints**: SQLite requires `use_listen_notify=False`
  (polling mode), which fits single-user home-service throughput.
- **Retries & recovery**: DBOS workflows checkpoint step outputs in
  SQLite and recover from the last completed step on restart; stale
  workflows from any prior DBOS application version are cancelled on
  boot so queue deduplication locks never wedge across upgrades.
- **Concurrency**: dedicated queues for conversions (`convert_queue`,
  configurable concurrency, default 2), deletes (`delete_queue`,
  concurrency 2), and polling/backfill (`poll_queue`, concurrency 1).
- **Scheduling**: DBOS schedules drive the daily watch-channel renewal
  check, the fallback change poll (default every 5 minutes), and the
  daily backfill sweep.

### Output layout

- **Folder per note**, mirroring the source Drive layout, with one
  Markdown file per page plus an `index.md` that links them via
  Obsidian wikilinks:
  - `Source/Notebooks/Journal/2026-07.note` →
    ```
    <vault>/Notebooks/Journal/2026-07/
    ├── index.md         # [[page-01]], [[page-02]], ...
    ├── page-01.md
    ├── page-01.png
    ├── page-02.md
    └── page-02.png
    ```
- **Assets**: one PNG next to each page's `.md`. Simplest layout for
  Obsidian's relative-image resolution.

### Deployment

- **Runtime**: Docker container, linuxserver.io-style — supports `PUID`,
  `PGID`, `TZ`, `UMASK` env vars for correct host-side file ownership.
  Two multi-arch (`linux/amd64` + `linux/arm64`) images are published to
  GHCR from the same repo: `sn2md-worker` (the worker) and
  `sn2md-worker/obsidian-sync` (a companion running Obsidian's headless
  sync CLI against the shared vault dir). The companion logs in and
  pairs the vault automatically from `OBSIDIAN_*` env vars, runs as a
  fixed non-root user (`99:100`, Unraid's `nobody:users`), and clears
  stale sync locks on startup. Unraid operations are documented in
  `docs/unraid-runbook.md`.
- **Mounts**:
  - `/data` — DBOS + application SQLite state, must be writable.
  - `/vault` — bind-mount to the Obsidian vault path on the host, must
    be writable.
  - `/secrets/service-account.json` — Google service account JSON key
    (mounted read-only).
- **Env vs config**: TOML config file for non-secret defaults;
  environment variables override at runtime. Container-canonical paths
  (DB URL, vault root, credentials) are baked into the image so it
  boots without any external config. Secrets always via env or mounted
  files.

### Observability

- **Health endpoints**: `/healthz` (liveness) and `/readyz` (readiness —
  200 iff an active Drive watch channel exists and hasn't expired, or
  the worker is in dev mode with no webhook URL configured).
- **Status endpoint**: `GET /status` returns JSON with recent
  conversions, recent failures, recent pending conversions, active
  watch channel + expiry, Drive changes cursor, in-flight `queue_depth`
  per DBOS queue, the latest `backfill` workflow's outcome, and
  per-step startup outcomes under `startup`.
- **Logs**: structured JSON to stdout via `structlog`. Every workflow
  emits `_started` / `_succeeded` / `_failed` / `_skipped (reason=…)`
  events. Failures include a stringified exception plus the full
  traceback under `exception`. Log lines carry a `correlation_id`
  minted at the original trigger (webhook push, scheduled poll,
  startup), so one operation is traceable end to end from trigger to
  vault write. Successful health-probe requests
  (`/healthz`, `/readyz`) are filtered out of the access log so
  container healthchecks don't flood it; failed probes and real
  traffic still appear.
- **Failure notifications**: none beyond logs (revisit if it becomes
  painful).

### Testing

- **Unit tests** (`tests/unit/`): in-memory SQLite throughout.
  BDD scenario classes for behavior (workflows,
  webhook, repos); plain
  functions for pure logic (path helpers, model alias mapping,
  TypeDecorator).
- **Fake externals via MagicMock and `patch`**: `DriveClient`,
  `run_multi_page`, and `DBOS.enqueue_workflow` at the call boundary.
- **No live-API tests in CI** — verify manually against a scratch
  Drive folder via `scripts/verify/`.

## Non-goals (for now)

- Two-way sync (Markdown edits don't feed back to `.note`).
- Handling non-`.note` files in the source folder.
- Hosting anywhere but Unraid.
- Failure notifications beyond logs.
- Any UI beyond `/status` JSON.

## Verifications (all resolved)

1. **Service-account changes feed on a personal user's shared folder** —
   verified 2026-07-04 via `scripts/verify/01_drive_access.py`.
   Personal-user edits on shared files appear in the service account's
   `changes.list` feed. Bonus finding: Supernote's device-sync flow
   replaces the Drive file (new `fileId`) rather than updating in
   place — reflected in tech-brief §4a.
2. **sn2md + Gemini end-to-end** — verified 2026-07-04. Model string
   `gemini/gemini-2.5-pro` (prefixed form required by llm-gemini
   ≥0.32). Baseline ~7.5s per page against Gemini 2.5 Pro.
3. **DBOS + SQLite** — supported with `use_listen_notify=False`.
   Postgres is DBOS's recommended production backend; SQLite is our
   zero-infrastructure tradeoff. Runs as an embedded library inside a
   single Python process; container smoke-tested end-to-end.
4. **Drive `changes.watch` TTL** — 7 days max, no automatic renewal.
   Worker creates a fresh channel on TTL headroom or URL change.
5. **Live conversion** — verified via ngrok deploy: Supernote edit →
   Drive push → `poll_changes` → `convert_note` → `page-NN.md` in the
   vault. Webhook reachability in the real deployment (production DNS
   + reverse proxy on Unraid) confirmed 2026-07-12. Multi-arch images
   have been publishing to GHCR since v0.1.0 (2026-07-05); v0.2.0
   followed on 2026-07-11.
