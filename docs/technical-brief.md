# sn2md-worker — Technical Brief

Companion to [product-brief.md](./product-brief.md). This document is
implementation-facing: architecture, components, workflows, data model,
config, deployment, and known unknowns. Anyone picking this up should be
able to start coding from here without re-litigating decisions.

## 1. Architecture

```
                           Google Drive
                     ┌──────────────────────┐
                     │ Source folder        │
                     │ (Supernote sync)     │
                     └───────┬──────────────┘
                             │ push notification
                             │ (POST webhook)
                             ▼
    Reverse proxy on Unraid (TLS, existing)
                             │
                             ▼
    ┌────────────────────────────────────────────────────┐
    │  sn2md-worker container (Python, uv, DBOS)         │
    │                                                    │
    │  FastAPI HTTP layer                                │
    │   ├── POST /webhooks/drive                         │
    │   ├── GET  /healthz  /readyz                       │
    │   └── GET  /status                                 │
    │                                                    │
    │  DBOS runtime (single SQLite file: workflow state  │
    │    + application tables)                           │
    │   ├── scheduled: renew_watch_channel (daily 06:00) │
    │   ├── scheduled: scheduled_backfill  (daily 05:00) │
    │   ├── scheduled: scheduled_poll_changes (fallback) │
    │   ├── enqueued : poll_changes  (poll_queue,   c=1) │
    │   ├── enqueued : convert_note  (convert_queue,c=2) │
    │   ├── enqueued : delete_output (delete_queue, c=2) │
    │   └── enqueued : backfill      (poll_queue,  once) │
    │                                                    │
    │  sn2md library + llm + llm-gemini                  │
    │            │                                       │
    │            ▼                                       │
    │       /vault (bind mount)                          │
    └────────────────────────────────────────────────────┘
                             │
                             ▼
                obsidian-sync container on Unraid
                (headless CLI on the same /vault,
                    pushes changes via Obsidian Sync)
                             │
                             ▼
                     Phones, laptop, etc.
```

Single process, single container. DBOS runs embedded (verified).
`debounce_file` is defined in the schema but the workflow is deferred —
Drive appears to only publish notifications for completed uploads, so
`poll_changes` enqueues `convert_note` directly.

## 2. Repository layout

```
sn2md-worker/
├── docs/
│   ├── product-brief.md
│   └── technical-brief.md          ← you are here
├── src/sn2md_worker/
│   ├── __init__.py
│   ├── __main__.py                 # `python -m sn2md_worker` entrypoint
│   ├── app.py                      # FastAPI app factory + request-id middleware
│   ├── clock.py                    # UTC wall-clock seam for tests
│   ├── config.py                   # pydantic-settings, TOML + env, singleton
│   ├── correlation.py              # correlation_id minting for cross-workflow tracing
│   ├── db.py                       # engine singleton, sql_session(), SQLite tuning
│   ├── logging.py                  # structlog + stdlib JSON setup, probe log filter
│   ├── observability.py            # /healthz /readyz /status
│   ├── startup_status.py           # boot-step outcomes surfaced at /status.startup
│   ├── sources/
│   │   ├── __init__.py
│   │   ├── models.py               # NoteMetadata + ListedNote (source-neutral DTOs)
│   │   └── protocol.py             # NoteSource Protocol + Source*Error hierarchy
│   ├── drive/
│   │   ├── __init__.py
│   │   ├── client.py               # DriveClient (implements NoteSource) over Drive v3
│   │   ├── models.py               # ChangeEvent / ChangesPage / ChannelInfo
│   │   ├── paths.py                # parent-chain source path resolution
│   │   └── webhook.py              # /webhooks/drive route + token verification
│   ├── conversion/
│   │   ├── __init__.py
│   │   ├── gemini.py               # single-page Gemini transcription + throttle retry
│   │   ├── multi_page.py           # per-page runner: extract, hash-first cache, render
│   │   └── paths.py                # vault path mapping + sanitization
│   ├── state/
│   │   ├── __init__.py
│   │   ├── conversions.py          # conversion_records repository
│   │   ├── cursor.py               # singleton drive_change_cursor repository
│   │   ├── debounce.py             # debounce_state repository (workflow deferred)
│   │   ├── models.py               # SQLAlchemy declarative + UTCDateTime TypeDecorator
│   │   ├── page_conversions.py     # per-page rows backing the hash-first cache
│   │   ├── schema.py               # schema initialization
│   │   └── watch_channels.py       # drive_watch_channels repository
│   └── workflows/
│       ├── __init__.py             # queue/schedule registration + startup helpers
│       ├── backfill.py             # startup and daily sweep backfill
│       ├── convert_note.py
│       ├── delete_output.py
│       ├── locks.py                # advisory file locks shared across workflows
│       ├── poll_changes.py         # walks changes.list; scheduled fallback poll
│       ├── queues.py               # queue-name constants (leaf module)
│       └── renew_watch.py          # scheduled channel renewal + catch-up poll
├── tests/
│   └── unit/                       # BDD scenario classes; mirrors src layout
│       ├── conversion/             # test_paths, test_multi_page, test_gemini
│       ├── drive/                  # test_client, test_models, test_paths, test_webhook
│       ├── sources/                # test_models, test_protocol
│       ├── state/                  # test_conversions, test_watch_channels, test_cursor, test_debounce, test_schema, test_page_conversions
│       ├── workflows/              # test_convert_note, test_poll_changes, test_delete_output, test_backfill, test_renew_watch, test_registration, test_locks
│       ├── test_correlation.py
│       ├── test_logging.py
│       ├── test_main.py
│       └── test_observability.py
├── scripts/verify/                 # M0 gate scripts (Drive access, sn2md-Gemini)
├── docker/
│   ├── entrypoint.sh               # linuxserver-style PUID/PGID/TZ/UMASK handling
│   └── obsidian-sync/              # companion sync image: Dockerfile + entrypoint.sh (see §10)
├── Dockerfile                      # python:3.12-slim + gosu + tzdata + uv sync
├── docker-compose.yml              # reference for local + Unraid
├── config.example.toml
├── .env.example
├── .dockerignore
├── pyproject.toml + uv.lock
├── .pre-commit-config.yaml
└── .github/workflows/
    ├── ci.yml                      # ruff + mypy + pytest + docker build verify
    └── release.yml                 # multi-arch GHCR publish (worker + obsidian-sync)
```

## 3. External dependencies

| Concern            | Library / API                             | Version target   |
| ------------------ | ----------------------------------------- | ---------------- |
| HTTP server        | `fastapi` + `uvicorn`                     | current stable   |
| Config             | `pydantic-settings`                       | v2.x             |
| Google Drive       | `google-api-python-client`, `google-auth` | current          |
| Durable workflows  | `dbos` (DBOS Transact)                    | current stable   |
| Note conversion    | `sn2md` (as library)                      | v2.7.0           |
| LLM provider       | `llm` + `llm-gemini`                      | llm-gemini ≥0.35 |
| Structured logging | `structlog` (or stdlib + json formatter)  | latest           |
| Testing            | `pytest`, `pytest-asyncio`                | current          |
| Quality            | `ruff`, `mypy`, `pre-commit`              | current          |

Python version pinned to **3.12**.

## 4a. Supernote sync semantics (empirical, 2026-07-04)

Verified via `scripts/verify/01_drive_access.py`: when the Supernote device
syncs an edited `.note` back to Drive, it does **not** update the file in
place. It creates a new Drive object (new `fileId`, new `md5`) with the
same name and trashes the previous one. Only one live file with that
name is present in the folder at rest.

Two consequences for the design:

1. **`fileId` is not a stable identity for a note.** Every device edit
   produces a fresh ID. Keying persistent records on `fileId` would
   leave orphan rows and, worse, cause a mirror-delete workflow to
   nuke the `.md` a preceding convert-workflow just wrote (because both
   file_ids resolve to the same output path).
2. **Delete + create ordering is not guaranteed** across polling
   windows or push notifications. Our persistence and workflow contracts
   must be safe under both `create_new → delete_old` and
   `delete_old → create_new`.

The design accommodates this by keying `conversion_records` on a
"logical key" (parent Drive path + name) and by making `delete_output`
consult live Drive state before removing anything — see the workflow
definitions in §5.

## 4. Data model

Held in a **single SQLite file** shared with DBOS. DBOS's own workflow
state lives in tables it manages; our application tables sit alongside
in the same SQLite database via our SQLAlchemy engine (`db.py`). One
file, one volume mount, one backup path. Every SQLite connection in the
process (both application and DBOS engines) is configured with WAL mode,
`IMMEDIATE` transaction isolation, `busy_timeout=30000`,
`synchronous=NORMAL`, and `foreign_keys=ON`.

Application tables are managed via SQLAlchemy declarative models with
**no migration framework or in-place migrations**. `Base.metadata.create_all`
runs on startup; any schema change applies on the next fresh boot. The
recovery path for a schema break is nuking the SQLite file and letting
`backfill` re-populate `conversion_records` from Drive.

`drive_watch_channels` has a partial unique index enforcing "at most
one row where `is_active = 1`" at the DB level; `drive_change_cursor`
has a `CHECK (id = 1)` constraint. Repositories return frozen `*View`
dataclasses so ORM objects never cross session boundaries.

**`conversion_records`** — one row per **logical** `.note` (Drive path +
name), because Supernote sync replaces the file rather than updating in
place (see §4a).

| column            | type     | notes                                                                                                          |
| ----------------- | -------- | -------------------------------------------------------------------------------------------------------------- |
| logical_key       | TEXT PK  | `<parent_path>/<name>` — stable across device edits                                                            |
| current_file_id   | TEXT     | Latest Drive file ID for this logical file; mutable                                                            |
| parent_folder_id  | TEXT     | Nullable; parent folder's Drive id — lets `delete_output` scope its live-file check without re-walking parents |
| source_name       | TEXT     | Drive file name at last conversion                                                                             |
| source_path       | TEXT     | Full Drive path (folder chain / name)                                                                          |
| source_md5        | TEXT     | md5Checksum from Drive metadata                                                                                |
| output_rel_path   | TEXT     | Path relative to `/vault`                                                                                      |
| last_status       | TEXT     | PENDING \| SUCCESS \| ERROR \| SKIPPED                                                                         |
| last_converted_at | DATETIME |                                                                                                                |
| attempts          | INTEGER  |                                                                                                                |
| last_error        | TEXT     | Nullable, last error message                                                                                   |

An index on `current_file_id` lets `delete_output` and `convert_note`
locate records by Drive file ID. `PENDING` is written before a
conversion starts downloading (see §5.4): a crash mid-convert leaves a
visible `PENDING` row (surfaced by `/status.recent_pending`), and the
retry treats the note as stale because the up-to-date check requires
`SUCCESS`.

**`page_conversions`** — one row per converted `.note` page, keyed on
`(logical_key, page_index)`. Used by the multi-page runner to skip
Gemini calls for pages whose PNG hash has already been transcribed
(lookup is hash-first; see §6).

| column            | type     | notes                                               |
| ----------------- | -------- | --------------------------------------------------- |
| logical_key       | TEXT     | Composite PK with page_index                        |
| page_index        | INTEGER  | Composite PK; 0-indexed                             |
| page_md5          | TEXT     | md5 of the rendered PNG                             |
| output_rel_path   | TEXT     | Path relative to the note folder, e.g. `page-01.md` |
| last_converted_at | DATETIME |                                                     |

**`drive_watch_channels`** — one row per active/superseded push channel
| column | type | notes |
|----------------|---------|--------------------------------------------------|
| channel_id | TEXT PK | UUID we generate |
| resource_id | TEXT | From Drive's response |
| token | TEXT | 32-byte hex, for authenticity check |
| webhook_url | TEXT | Nullable; the URL this channel was registered with. Compared against `settings.webhook.url` on renewal to force rotation on URL change |
| expires_at | DATETIME| From Drive's response |
| start_page_token | TEXT | pageToken used at watch time |
| created_at | DATETIME| |
| is_active | BOOL | Only one row is_active=True at a time |

**`drive_change_cursor`** — singleton row
| column | type | notes |
|----------------|---------|--------------------------------------------------|
| id | INTEGER PK | fixed value 1 |
| page_token | TEXT | last saved cursor (or startPageToken) |
| last_polled_at | DATETIME| |

**`debounce_state`** — one row per file currently being debounced
| column | type | notes |
|----------------|---------|--------------------------------------------------|
| file_id | TEXT PK | |
| last_size | INTEGER | |
| last_md5 | TEXT | |
| stable_since | DATETIME| Nullable; set when size+md5 unchanged |
| updated_at | DATETIME| |

## 5. Workflows

Every workflow is a thin `@DBOS.workflow()` wrapper that delegates to a
`<name>_impl(...)` function taking injected dependencies (`source:
NoteSource` for conversion workflows; `drive: DriveClient` for Drive
change-feed/watch workflows) plus `settings`. Any non-deterministic I/O
(Drive API calls, SQLite queries, filesystem checks) that determines
whether or what child workflows to enqueue is wrapped in a
`@DBOS.step()` returning a frozen dataclass (or primitive) so DBOS
recovery replay is deterministic. `DBOS.application_version` is pinned
(`"1"`), and any active workflows from a different version are cancelled
on startup so queue deduplication IDs are never wedged across container
upgrades.

Queues (registered after `DBOS.launch()`, names in
`workflows/queues.py`):

- `convert_queue` — `worker_concurrency=settings.queue.convert_concurrency`
  (default 2). Serves `convert_note` only. Enqueues carry
  `SetEnqueueOptions(deduplication_id=f"{file_id}:{source_path}")` so
  duplicate triggers do not queue redundant conversions.
- `delete_queue` — `worker_concurrency=2`. Serves `delete_output` on its
  own queue so long conversions don't stall stale-delete cleanup.
- `poll_queue` — `worker_concurrency=1`. Serves `poll_changes` and
  `backfill`.

Schedules (registered idempotently after `DBOS.launch()`, and updated in
place if a configured cron expression changes):

- `renew-watch-channel` — cron `0 6 * * *` (daily 06:00 UTC). Runs
  `renew_watch_channel`.
- `fallback-poll-changes` — cron from `settings.drive.fallback_poll_cron`
  (default `*/5 * * * *`). Runs `scheduled_poll_changes`, which enqueues
  `poll_changes("fallback")` onto `poll_queue` so it serializes behind
  any webhook-triggered poll instead of racing it on the shared cursor.
- `backfill-sweep` — cron from `settings.drive.backfill_sweep_cron`
  (default `0 5 * * *`, daily 05:00 UTC). Runs `scheduled_backfill`,
  which enqueues `backfill` onto `poll_queue` to retry any conversions
  that previously failed.

`convert_note` and `delete_output` acquire an OS advisory `FileLock`
(`workflows/locks.py`) keyed on a SHA-256 of `logical_key` (lockfiles
under `/tmp/sn2md-worker-locks/`) before touching the vault or DB rows
for a note, preventing concurrent conversions or a stale delete racing
an in-flight convert.

### 5.1 `renew_watch_channel` — scheduled daily

Serialized by an advisory file lock (`__watch_channel__`) so boot-time
channel checks and the daily cron cannot race. Renewal runs when a
webhook URL is configured and any of the following holds:

- No active channel row exists.
- The active channel's `webhook_url` differs from `settings.webhook.url`
  (best-effort stops the old channel on Drive, then creates a new one).
- The active channel's `expires_at - now` ≤ `RENEWAL_HEADROOM` (48h).

Channel creation uses a **two-phase** flow to survive a crash between
Drive and the DB write:

1. Insert a pending row (`channel_id`, `token`, placeholder
   `resource_id=""`, placeholder `expires_at = now + 7 days`) BEFORE
   calling Drive so pushes arriving immediately after Drive's handshake
   can authenticate.
2. Call `drive.watch_changes(...)` without sending `expiration` so Drive
   assigns its default 7-day maximum TTL regardless of host clock skew.
3. On success: confirm the row with Drive's real `resource_id` and
   `expires_at`, then promote it to active. On failure: delete the
   pending row.

When renewal replaces an existing channel, it enqueues a catch-up
`poll_changes("renewal")` on `poll_queue` to cover changes between the
old channel's last push and the new channel going active. At startup,
if the previously-active channel expired while the worker was down, an
additional `poll_changes("recovery")` is enqueued.

### 5.2 `poll_changes` — enqueued by the webhook and fallback schedule

Loads the persisted `page_token` (seeding from Drive's start token if
missing), walks `changes.list` pages inside a DBOS step per page, and
dispatches each change:

- Removed or trashed → enqueue `delete_output(file_id)` on `delete_queue`.
- `.note` inside the source-folder subtree → enqueue
  `convert_note(file_id, source_path)` on `convert_queue` (deduplicated
  by `file_id:source_path`).
- File outside the source-folder subtree that still has a conversion
  record for `file_id` (moved out of the source folder) → enqueue
  `delete_output(file_id)` on `delete_queue`.
- Anything else → ignored.

**Cursor advances per page** so a crash or transient failure mid-walk
resumes at the failing page. Parent-folder metadata lookups are cached
in memory across each poll run.

**Error handling**: per-change permanent 4xx errors
(`DrivePermanentError`) are logged and skipped so a deleted/inaccessible
file doesn't block later changes on the same page; transient errors
(`DriveTransientError`) propagate so DBOS retries from the last saved
cursor. If `changes.list` itself fails with `DrivePermanentError`
(persisted cursor expired beyond Drive's change window), the cursor is
reset to Drive's current start token and `backfill` is enqueued.

### 5.3 `debounce_file` — DEFERRED

Not built. The `debounce_state` table and repository exist for future
use, but Drive push notifications only fire on completed uploads, so
`poll_changes` enqueues `convert_note` directly.

### 5.4 `convert_note` — per-file, `convert_queue` (filelock-serialized)

Flow under the per-`logical_key` advisory lock:

1. Validate `source_path` and compute `logical_key` (`UnsafePathError`
   on traversal, NUL, empty segments, or Windows-reserved names is
   logged and skipped).
2. Fetch metadata from `source`; skip if trashed or if `file_id` returns
   `SourceNotFoundError` (404). Other `SourcePermanentError` (400/401/403)
   records `ERROR` status and returns without retrying.
3. Idempotency check: skip if `conversion_records[logical_key]` already
   has matching `source_md5`, `SUCCESS` status, and `current_file_id`.
4. Detect Drive move/rename: if prior conversion records exist for the
   same `file_id` under a different `logical_key`, seed existing page
   hashes and copy cached `page-*` / `index.md` files from the old vault
   directory before converting.
5. Download the `.note` file to a temp directory via chunked streaming
   and mark the conversion record `PENDING`.
6. Run per-page conversion (`conversion/multi_page.py`), persisting each
   page's `page_conversions` row incrementally as pages complete.
7. Finalize: mark the conversion record `SUCCESS`, delete
   `page_conversions` rows and vault `page-NN.*` files beyond the new
   page count (plus any legacy flat `<basename>.md` or sidecar files),
   and—outside the new key's lock, under each old key's lock—remove any
   stale vault directory and DB rows left behind by a move or rename.

Any uncaught transient or conversion exception records `ERROR` status in
`conversion_records`, logs `convert_note_failed`, and re-raises so DBOS
records the workflow as `ERROR`.

### 5.5 `delete_output` — per-file, `delete_queue` (filelock-serialized)

Handles Supernote's replace-then-delete semantics (inside a DBOS step
around the DB/Drive/filesystem work, followed by workflow-level enqueue
on repoint):

1. Look up `conversion_records` by `current_file_id`; no-op if absent.
2. Acquire the per-key `FileLock` for `record.logical_key` and re-read
   the record inside the lock.
3. If `parent_folder_id` is recorded, query `source.find_live_note` for
   a live file with the same `source_name` in that folder. If a live
   twin with a different `file_id` exists, repoint `current_file_id` to
   the new file, enqueue `convert_note(live.id, source_path)` (deduped),
   and skip the vault delete. (`SourceNotFoundError` on the parent
   folder falls through to deletion; other `SourcePermanentError` skips
   deletion safely.)
4. Otherwise, verify the target directory resolves strictly inside
   `vault_root`, remove the note's vault directory, and delete its
   `conversion_records` and `page_conversions` rows.

### 5.6 `backfill` — startup and daily sweep, `poll_queue`

Scans Drive and SQLite inside a DBOS step (`list_all_notes` against the
source folder compared with a single bulk load of `conversion_records`),
then enqueues `convert_note` (deduplicated by `file_id:source_path`) for
any note that is missing, has a different `current_file_id` or
`source_md5`, or whose `last_status != SUCCESS`. Unsafe paths are logged
and skipped without aborting the walk.

## 6. sn2md integration

We drive sn2md's per-page primitives as a library (`conversion/multi_page.py`
and `conversion/gemini.py`):

- `NotebookExtractor.extract_images` renders each `.note` page to PNG
  (sorted numerically by the last integer in each filename stem so
  date-prefixed note names or temp paths never skew page ordering).
- `sn2md.ai_utils.image_to_markdown` transcribes one page PNG at a time
  via Gemini (with one-time `llm` plugin loading serialized across
  worker threads by a lock).

**Prompt**: builds on sn2md's `TO_MARKDOWN_TEMPLATE` with stricter
guidance requiring ASCII-only characters inside `mermaid` code blocks
and escaped unbalanced backticks / angle brackets / pipes in prose.
Overridable via `[sn2md] prompt` in `config.toml` (must retain the
`{context}` placeholder, which receives the trailing 200 characters of
the previous page's markdown body).

Output shape per note:

```
<basename>/
├── index.md          # Obsidian wikilinks: [[page-01]], [[page-02]], ...
├── page-01.md        # frontmatter (created, tags, page, of) + Gemini output + image link
├── page-01.png
├── page-02.md
├── page-02.png
```

**Hash-first page caching**: before calling Gemini, the runner indexes
prior page bodies and `created` frontmatter dates by PNG md5 (from
DB-known `page_conversions` rows and any on-disk `page-NN.md` /
`page-NN.png` pairs). Cache-hit pages are staged to their target
`page-NN.md` / `page-NN.png` slots up front so a mid-note Gemini failure
after a page insertion cannot overwrite shifted cached pages before they
are saved at their new indices. Gemini runs only for PNG hashes never
transcribed before, and existing `created` dates in page and `index.md`
frontmatter are preserved across updates.

**Gemini retry budgets** (`conversion/gemini.py`):
- **Fast budget** (default): 3 attempts, exponential backoff with jitter
  (2s–20s) for generic transient errors.
- **Throttle budget**: 8 attempts, exponential backoff with jitter
  (5s–120s) when any exception in the chain matches rate-limit / quota
  markers (`429`, `rate limit`, `resource_exhausted`, `quota`,
  `too many requests`).

## 7. Drive client (`drive/client.py`)

Wraps `google-api-python-client` Drive v3 with service-account
credentials (`drive.readonly` scope). Because `httplib2.Http` and
`google-auth` credentials are not thread-safe, `DriveClient` builds a
per-thread `AuthorizedHttp` backed by a per-thread credentials copy and
a 60-second socket timeout.

**`NoteSource` seam** (`sources/`). `DriveClient` implements the
runtime-checkable `NoteSource` protocol (`list_all_notes`,
`get_metadata`, `download`, `find_live_note`) using source-neutral DTOs
(`NoteMetadata`, `ListedNote`). Drive-specific change-feed and watch
methods (`changes_list`, `watch_changes`, `stop_channel`,
`get_start_page_token`) remain on `DriveClient` outside the protocol.

**Retry + error taxonomy**. All Drive API calls and chunked media
downloads (4 MB chunks, truncated and restarted from byte 0 on retry)
retry up to 3 attempts with exponential backoff + jitter on HTTP 429,
5xx, and transport/SSL/timeout errors. Terminal failures map to:

- `DriveNotFoundError` (subclasses `DrivePermanentError` and
  `SourceNotFoundError`) — HTTP 404.
- `DrivePermanentError` (subclasses `SourcePermanentError`) — other
  non-429 4xx responses.
- `DriveTransientError` (subclasses `SourceTransientError`) — 5xx/429
  after retries, auth refresh failures, and transport errors.

Path resolution (`drive/paths.py`) walks parent chains up to depth 100
to compute a file's POSIX path relative to `source_folder_id`, trying
all parents on legacy multi-parent files.

## 8. Webhook (`drive/webhook.py`)

```
POST /webhooks/drive
Headers of interest:
  X-Goog-Channel-Id, X-Goog-Channel-Token,
  X-Goog-Resource-Id, X-Goog-Resource-State, X-Goog-Message-Number
```

Defined as a synchronous `def` route so FastAPI runs its blocking
SQLite and DBOS operations in the worker threadpool:

1. Return 200 immediately for `X-Goog-Resource-State: sync`.
2. Look up the channel by `X-Goog-Channel-Id`; if missing or expired,
   log a warning and return 200.
3. Verify `X-Goog-Channel-Token` in constant time (`hmac.compare_digest`);
   if invalid, log a warning and return 200.
4. Mint a `correlation_id` and enqueue `poll_changes("webhook", correlation_id)`
   with `SetEnqueueOptions(deduplication_id=f"{channel_id}:{message_number}")`,
   absorbing `DBOSQueueDeduplicatedError` as 200.

## 9. Configuration

See [`config.example.toml`](../config.example.toml) for the canonical
layout. Sections:

- `[drive]` — `source_folder_id`, `fallback_poll_cron` (default
  `*/5 * * * *`), `backfill_sweep_cron` (default `0 5 * * *`), debounce
  timing knobs (reserved for the deferred `debounce_file` workflow).
- `[vault]` — `root_path`.
- `[sn2md]` — `model` (default `gemini/gemini-3.8-flash`), `api_key`
  (SecretStr, optional; falls back to `LLM_GEMINI_KEY` env var),
  `prompt` (optional full-prompt override; must include `{context}`).
- `[queue]` — `convert_concurrency` (default 2).
- `[observability]` — `log_level`, `status_endpoint_enabled`.
- `[database]` — `url` (single SQLite/Postgres URL shared by DBOS and
  application tables).
- `[webhook]` — `url` (public HTTPS URL for Google Drive push
  notifications; empty in dev).
- `[google]` — `application_credentials` (path to the service account
  JSON key).

**Env override pattern**: `SN2MD_WORKER__SECTION__KEY=value` (double
underscore for nesting). Env always wins over TOML.

**Container-canonical paths baked into the Docker image**:

- `SN2MD_WORKER__DATABASE__URL=sqlite:////data/sn2md-worker.sqlite`
- `SN2MD_WORKER__VAULT__ROOT_PATH=/vault`
- `SN2MD_WORKER__GOOGLE__APPLICATION_CREDENTIALS=/secrets/service-account.json`

The image boots without any external config; you can add a
`config.toml` bind-mount or `.env` file for anything else.

**Secrets** (always env or mounted files, never in TOML):

- `LLM_GEMINI_KEY` — Gemini API key (name matches llm-gemini expectation)
- Service account JSON at `/secrets/service-account.json` (mount
  read-only)
- `SN2MD_WORKER__WEBHOOK__URL` — public URL Google POSTs to

## 10. Deployment

Canonical files:

- [`Dockerfile`](../Dockerfile) — `python:3.12-slim`, uv-managed venv at
  `/app/.venv`, gosu + tzdata installed, non-root `app` user. Root
  ENTRYPOINT drops to `app` after PUID/PGID reshape.
- [`docker/entrypoint.sh`](../docker/entrypoint.sh) — linuxserver-style:
  reads `PUID` / `PGID` / `TZ` / `UMASK` at startup, `usermod -o`
  reshapes `app`, symlinks `/etc/localtime`, chowns `/data` and
  `/vault` (`/app/.venv` is already `app`-owned from the build; skip
  the whole step with `CHOWN_ON_START=false`), then
  `exec gosu app:app "$@"`. Errors are surfaced under `set -euo
pipefail` instead of swallowed — a read-only mount or full disk
  fails the boot loudly.
- [`docker-compose.yml`](../docker-compose.yml) — reference compose for
  local dev / single-host deployments with env-driven volume paths.
- [`.env.example`](../.env.example) — required env vars and
  linuxserver-style knobs.

**Published images**: `ghcr.io/macdonaldr93/sn2md-worker:latest` and
`ghcr.io/macdonaldr93/sn2md-worker/obsidian-sync:latest` (also
`sha-<short>` and semver tags). Multi-arch: `linux/amd64` +
`linux/arm64`. Both built by `.github/workflows/release.yml` on push
to main and semver tags.

### Companion image: obsidian-sync

Built from [`docker/obsidian-sync/`](../docker/obsidian-sync/Dockerfile):
`node:22-alpine` + the `obsidian-headless` CLI (`ob`) under `tini`,
watching the same `/vault` bind mount and pushing changes out via
Obsidian Sync. Design points:

- **Fixed non-root user `99:100`** (Unraid's `nobody:users`), baked at
  build time. No linuxserver baseimage, no PUID/PGID reshaping, no s6:
  the container never runs as root, and both writable mounts
  (`/config`, `/vault`) must be owned `99:100` on the host.
  `HOME=/config` pins all `ob` auth/state into the volume.
- **Env-driven auto-configuration** in the entrypoint: first boot runs
  `ob login` from `OBSIDIAN_EMAIL` / `OBSIDIAN_PASSWORD` (optional
  `OBSIDIAN_MFA`), then `ob sync-setup` pairs `/vault` against the
  remote vault named by `OBSIDIAN_VAULT` with
  `OBSIDIAN_ENCRYPTION_PASSWORD`. Missing env vars idle the container
  with an instructive log line instead of crashlooping.
- **Stale `.sync.lock` clearing**: `obsidian-headless` treats a recent
  `/vault/.obsidian/.sync.lock/` as "another instance is running"; a
  back-to-back setup-then-sync or a hard container kill trips that
  check, so the entrypoint removes a leftover lock before
  `exec ob sync --continuous` (safe: this container is the only sync
  runner in the deployment).

Operational setup lives in `docs/unraid-runbook.md` §2.

### Volumes (defaults)

| Container path                  | Purpose                             |
| ------------------------------- | ----------------------------------- |
| `/data`                         | DBOS + app SQLite state (writable)  |
| `/vault`                        | Obsidian vault directory (writable) |
| `/secrets/service-account.json` | Google service account JSON (ro)    |
| `/app/config.toml` (optional)   | TOML overrides                      |

### linuxserver-style env vars

| Env var          | Default   | Notes                                                     |
| ---------------- | --------- | --------------------------------------------------------- |
| `PUID`           | `1000`    | Set to your host user's UID for correct file ownership    |
| `PGID`           | `1000`    |                                                           |
| `TZ`             | `Etc/UTC` | IANA timezone (e.g. `America/Toronto`)                    |
| `UMASK`          | `022`     |                                                           |
| `CHOWN_ON_START` | `true`    | Set `false` to skip the startup chown pass on huge vaults |

### Reverse proxy

Route `sn2md.<domain>/webhooks/drive` → `sn2md-worker:8080/webhooks/drive`.
Terminate TLS at the proxy (Let's Encrypt is fine). Only expose
`/webhooks/drive` publicly; `/status`, `/healthz`, `/readyz` should be
LAN-only or behind reverse-proxy auth.

## 11. Prerequisites (one-time, human tasks)

1. **GCP project + service account**
   - Create GCP project (personal Google account is fine).
   - Enable the Google Drive API.
   - Create a service account, create a JSON key, download it.
   - Note the `client_email` (something like
     `sn2md-worker@your-project.iam.gserviceaccount.com`).
2. **Share source folder to service account**
   - In personal Drive, share the Supernote sync folder → Viewer to the
     service account's email.
3. **Gemini API key**
   - Get one from Google AI Studio (`ai.google.dev`).
4. **Public webhook URL**
   - Add DNS `sn2md.<yourdomain>` → Unraid.
   - Wire your existing reverse proxy to route
     `/webhooks/drive` to the container's port 8080.
   - Confirm the URL is reachable externally with a valid TLS cert (curl
     from an off-network machine).
5. **Vault path**
   - Ensure the vault directory exists on Unraid and is mounted into both
     the `obsidian-sync` container (headless CLI) and this worker
     (`/vault`). See `docs/unraid-runbook.md` §2 for the sync setup.

## 12. Security notes

- Service account JSON: mount read-only, restrict host-side permissions.
- Webhook `token`: 32-byte random hex generated per channel, persisted
  alongside the channel record. Verify on every incoming request. Not a
  cryptographic signature (Google Drive push doesn't offer one), but
  prevents casual spoofing.
- `changes.list` calls use `fields=` to minimize response size and avoid
  fetching data we don't need.
- No inbound requests other than the webhook are accepted from the
  internet; `/status` should be LAN-only (or behind reverse-proxy auth).
- Gemini key: env-only, not logged.
- Consider adding basic-auth at the reverse proxy for `/status` if it
  ever exposes real data.

## 13. Failure modes and recovery

| Scenario                              | Recovery                                                                                                                                               |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Webhook missed (network glitch)       | The fallback `poll_changes` cron (§5.2) catches it within one interval (default 5 min); a restart also runs `backfill`.                                |
| Container restart mid-conversion      | DBOS resumes workflow from last completed step; already-converted pages are reused from the hash-first cache                                           |
| Watch channel expires without renewal | Daily `renew_watch_channel` cron creates a fresh channel; cursor preserves continuity between old and new channel                                      |
| Webhook URL changed (ngrok, DNS)      | Next renewal check detects `webhook_url != settings.webhook.url`, stops the old channel on Drive, and creates a new one                                |
| Gemini failure / rate limit           | 429-shaped errors retry in place on an extended budget (8 attempts, backoff to 120s, §6); on exhaustion, workflow records ERROR and the daily `backfill-sweep` re-drives it (completed pages skipped) |
| Malformed `.note`                     | sn2md raises; workflow terminates with ERROR; log + record status                                                                                      |
| Drive quota / auth error              | Workflow raises; log; next scheduled or manual retry                                                                                                   |
| SQLite locked / corrupted             | Container fails healthcheck; user restores from backup of `/data`                                                                                      |

## 14. Observability

### HTTP endpoints

`/readyz` and `/status` are defined as synchronous `def` handlers so
FastAPI executes their SQLite queries in the worker threadpool without
blocking the event loop:

- `/healthz` — 200 if the process is up serving HTTP.
- `/readyz` — 200 in dev mode (empty `webhook.url`) or when an active
  `drive_watch_channels` row exists with `expires_at > now`; else 503.
- `/status` (honors `observability.status_endpoint_enabled`) — JSON:
  ```json
  {
    "recent_conversions": [
      {
        "logical_key": "...",
        "file_id": "...",
        "source_md5": "...",
        "output_rel_path": "...",
        "last_converted_at": "...",
        "last_error": null
      }
    ],
    "recent_failures": [
      {
        "logical_key": "...",
        "file_id": "...",
        "source_md5": null,
        "output_rel_path": "",
        "last_converted_at": "...",
        "last_error": "..."
      }
    ],
    "recent_pending": [],
    "watch_channel": {
      "channel_id": "...",
      "resource_id": "...",
      "expires_at": "...",
      "is_active": true
    },
    "change_cursor": { "page_token": "...", "last_polled_at": "..." },
    "queue_depth": { "convert_queue": 0, "delete_queue": 0, "poll_queue": 0 },
    "backfill": {
      "status": "SUCCESS",
      "started_at": "...",
      "completed_at": "...",
      "error": null
    },
    "startup": {
      "drive_client": "ok",
      "seed_cursor": "ok",
      "ensure_channel": "ok",
      "backfill_enqueue": "ok",
      "last_error": null
    }
  }
  ```
  `recent_pending` lists `PENDING` conversion records (in-flight or
  crash-interrupted conversions). `queue_depth` and `backfill` query
  DBOS's `workflow_status` table directly, falling back to zero / null
  defaults if the table does not yet exist. `startup` reports per-step
  boot outcomes (`"ok"` | `"deferred"` | `"failed"`, see
  [`docs/unraid-runbook.md#10-startup-model`](./unraid-runbook.md#10-startup-model)).

### Structured log events

`structlog` JSON to stdout. Every workflow emits:

- `<workflow>_started` — entry, INFO.
- `<workflow>_succeeded` — happy exit, INFO. Includes outcome counters
  (`enqueued`, `skipped`) where relevant.
- `<workflow>_failed` — ERROR, `exc_info=True` (full traceback in the
  `exception` field), re-raised so DBOS records ERROR status.
- `<workflow>_skipped` — INFO or WARNING, always with a `reason=` field.
- Intermediate events (e.g. `poll_changes_enqueued`,
  `convert_note_running_multi_page`, `renew_watch_channel_created`)
  include concrete context on each.

**Health-probe access-log suppression.** Successful `GET /healthz` and
`GET /readyz` requests are filtered out of `uvicorn.access` (`logging.py`)
so periodic container healthchecks don't flood the JSON log; failed
probes (status ≥ 400) and all other traffic still log.

### Correlation IDs

Every log line inside a request or workflow scope carries tracing
context:

- **HTTP requests** — `RequestIdMiddleware` binds `request_id` (from
  `X-Request-Id` or a fresh 16-char hex id), `method`, and `path` into
  structlog contextvars and echoes `X-Request-Id` on the response.
- **Workflows** — each workflow binds `dbos_workflow_id`, `workflow`,
  and `correlation_id` (plus `file_id`, `logical_key`, or `trigger` as
  applicable). `correlation_id` is minted at each root trigger (webhook
  receipt, cron tick, startup helper) and passed as the trailing
  workflow argument across every child enqueue so an operation can be
  traced end-to-end across worker threads.

## 15. Testing strategy

Unit tests run against in-memory SQLite under `tests/unit/`, mirroring
the `src/sn2md_worker/` layout:

- **BDD scenario classes** (`TestWhen<Scenario>`) with `# GIVEN / # WHEN
  / # THEN` markers for behavior tests (workflows, webhook, state
  repositories, observability endpoints).
- **Plain function tests** for pure logic (path helpers, model alias
  mapping, `UTCDateTime`).

External dependencies (`NoteSource` / `DriveClient`, per-page runner,
`DBOS.enqueue_workflow`) are patched at the module boundary; manual
verification against live Drive + Gemini uses `scripts/verify/`.

## 16. Verifications (all resolved)

1. **Service account changes feed** — ✅ Personal-user edits on shared
   files appear in the service account's `changes.list` feed, and
   Supernote sync replaces files rather than updating in place (§4a).
2. **DBOS runtime shape** — ✅ Runs as an embedded library inside a
   single Python process with SQLite holding both DBOS and application
   tables.
3. **sn2md-as-library end-to-end** — ✅ `gemini/gemini-2.5-pro` via
   `llm-gemini` produces Markdown at ~7.5s/page baseline.
4. **Webhook reachability from Google** — ✅ Confirmed end-to-end in the
   production Unraid deployment behind DNS + reverse proxy.
