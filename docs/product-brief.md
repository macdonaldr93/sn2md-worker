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
   `convert_note` for each affected `.note` file.
4. `convert_note` downloads the file, extracts each page's PNG, calls
   Gemini 2.5 Pro only for pages whose hash has changed since the last
   conversion (via the `page_conversions` cache table), and writes one
   Markdown file per page plus an `index.md` under a per-note folder
   that mirrors the Drive layout.
5. Destination is a local vault directory on Unraid; an Obsidian instance
   on the same host has that directory open and pushes to Obsidian Sync.

## Decisions

### Language & tooling

- **Language**: Python 3.11+ (sn2md's floor). Drives sn2md's per-page
  primitives (`NotebookExtractor.extract_images`, `image_to_markdown`)
  from `conversion/multi_page.py` so we can cache per-page LLM output.
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
- **Fallback poller** (deferred): a scheduled `poll_changes` cron would
  provide a safety net when a webhook is missed. Currently not built —
  the startup backfill and manual webhook delivery both cover the
  observed failure modes, and we can add it if we see actual misses.
- **Public URL**: existing reverse proxy on Unraid handles TLS + routing to
  the worker container. Webhook path suggested: `/webhooks/drive`.
- **Watch channel renewal**: Confirmed max TTL 7 days (604800s) for
  `changes.watch`; no automatic renewal on Drive's side. We omit
  `expiration` from the `watch_changes` request so Drive picks its own
  max (avoiding host-clock skew), then a daily DBOS cron
  (`renew_watch_channel`) creates a fresh channel when the current one
  is within `RENEWAL_HEADROOM = 48h` of expiry (5-day cadence in
  practice).
- **Webhook authenticity**: `token` field set at channel creation (32-byte
  random hex), echoed as `X-Goog-Channel-Token` on every notification.
  Worker verifies token + `X-Goog-Channel-Id` against the active channel
  record.
- **Debounce** (deferred): the tech brief includes a per-file
  size/md5 stability poll before conversion. Not built — Drive appears
  to only publish notifications for completed uploads, so we've enqueued
  `convert_note` directly. Table `debounce_state` exists and the runtime
  is wired for it; we'll add the workflow if we observe bad conversions
  from partial files in practice.
- **Update handling**: at the note level, we skip when the source
  `.note` md5 matches the last successful conversion. Below that, the
  per-page cache (`page_conversions` table + `page-NN.png` md5) means
  we only call Gemini for pages whose rendered PNG has changed. Editing
  the last page of a 20-page notebook costs one Gemini call, not 20.
  Identity for "same note" is the Drive path + filename, not the
  `fileId` (see technical-brief §4a).
- **Deletion handling**: if a `.note` is deleted from Drive, delete the
  corresponding Markdown + assets from the vault (mirror source). Delete
  workflow first confirms no live file with the same logical key still
  exists — protects against the delete-then-recreate race during device
  edits.

### Backfill & idempotency

- **Backfill on startup**: scan the source folder; convert every `.note` that
  doesn't have a matching successful-conversion record. Ensures files that
  arrived while the worker was down are not missed.
- **Idempotency key**: source file hash (md5 from Drive metadata) → success
  record persisted in DBOS/SQLite.

### LLM / sn2md

- **Model**: `gemini/gemini-2.5-pro` (via `llm-gemini` plugin ≥0.32).
- **Prompt**: sn2md's default `TO_MARKDOWN_TEMPLATE` (works well with
  Gemini per sn2md's own guidance).
- **API key**: `LLM_GEMINI_KEY` env var, or `sn2md.api_key` in config.
  `_resolve_gemini_key` in `workflows/convert_note.py` prefers the
  config value, falls back to the env var, raises if neither is set.
- **Integration surface**: we drive sn2md's per-page primitives —
  `NotebookExtractor.extract_images` renders each page to PNG,
  `sn2md.ai_utils.image_to_markdown` calls Gemini for one page at a
  time. This lets us cache per-page LLM output on hash, which
  `import_supernote_file_core` (sn2md's single-file wrapper) doesn't
  support.

### Queue & durability

- **Framework**: [DBOS Transact](https://dbos.dev) for Python with the
  SQLite backend.
- **SQLite file**: on a mounted volume so state survives container
  restarts.
- **Confirmed constraints**: SQLite is officially supported but Postgres is
  the documented production recommendation. When using SQLite,
  `use_listen_notify=False` is required (falls back to polling). Judged
  acceptable given single-user throughput (a handful of notes per day, not
  a busy queue).
- **Retries**: DBOS workflows auto-recover from the last completed step on
  restart; step-level retries configurable (specifics in technical brief).
- **Concurrency**: `DBOS.register_queue("convert", worker_concurrency=N)` —
  configurable, default 2.
- **Scheduling**: `DBOS.create_schedule` used for weekly Drive watch-channel
  renewal.

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
  Multi-arch image (`linux/amd64` + `linux/arm64`) published to GHCR.
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
  conversions, recent failures, active watch channel + expiry, Drive
  changes cursor, in-flight `queue_depth` per DBOS queue, and the
  latest `backfill` workflow's outcome.
- **Logs**: structured JSON to stdout via `structlog`. Every workflow
  emits `_started` / `_succeeded` / `_failed` / `_skipped (reason=…)`
  events. Failures include a stringified exception plus the full
  traceback under `exception`.
- **Failure notifications**: none beyond logs (revisit if it becomes
  painful).

### Testing

- **Unit tests** (`tests/unit/`): 206 tests, in-memory SQLite. BDD
  scenario classes for behavior (workflows, webhook, repos); plain
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
   vault. Multi-arch image publish pending first push to GitHub.
