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
    │    + app tables via SQLAlchemyDatasource)          │
    │   ├── scheduled: renew_watch_channel (daily 06:00) │
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
│   ├── app.py                      # FastAPI app factory + RequestIdMiddleware
│   ├── clock.py                    # now_utc() wall-clock seam (tests monkeypatch it)
│   ├── config.py                   # pydantic-settings, TOML + env, singleton
│   ├── correlation.py              # new_correlation_id() for cross-enqueue log tracing
│   ├── db.py                       # engine + SQLAlchemyDatasource singletons, sql_session()
│   ├── logging.py                  # structlog + stdlib JSON setup, HealthProbeAccessFilter
│   ├── observability.py            # /healthz /readyz /status
│   ├── startup_status.py           # boot-step outcomes surfaced at /status.startup
│   ├── sources/
│   │   ├── __init__.py
│   │   ├── models.py               # NoteMetadata + ListedNote (source-neutral DTOs)
│   │   └── protocol.py             # NoteSource Protocol + Source*Error hierarchy
│   ├── drive/
│   │   ├── __init__.py
│   │   ├── client.py               # DriveClient (implements NoteSource) over google-api-python-client
│   │   ├── models.py               # ChangeEvent / ChangesPage / ChannelInfo (change-feed models)
│   │   ├── paths.py                # resolve_source_path (parent chain walk)
│   │   └── webhook.py              # /webhooks/drive route + token verify
│   ├── conversion/
│   │   ├── __init__.py
│   │   ├── gemini.py               # one page PNG to markdown via Gemini, throttle-aware retry
│   │   ├── multi_page.py           # per-page runner: extract, hash-first cache, Gemini
│   │   └── paths.py                # logical_key / note_output_dir / output_rel_path
│   ├── state/
│   │   ├── __init__.py
│   │   ├── conversions.py          # upsert / record_failure / mark_success / get / list
│   │   ├── cursor.py               # singleton get / set_cursor
│   │   ├── debounce.py             # record_probe / clear (workflow deferred)
│   │   ├── models.py               # SQLAlchemy declarative + UTCDateTime TypeDecorator
│   │   ├── page_conversions.py     # per-page rows backing the hash-first cache
│   │   ├── schema.py               # init_schema
│   │   └── watch_channels.py       # create / list / get_active / mark_active
│   └── workflows/
│       ├── __init__.py             # register_queues, register_schedules, startup helpers
│       ├── backfill.py             # startup one-shot
│       ├── convert_note.py
│       ├── delete_output.py
│       ├── locks.py                # per-logical-key FileLock shared by convert + delete
│       ├── poll_changes.py         # walks changes.list; scheduled_poll_changes fallback
│       ├── queues.py               # queue-name constants (leaf module)
│       └── renew_watch.py          # scheduled channel renewal + catch-up poll
├── tests/
│   └── unit/                       # BDD scenario classes; mirrors src layout
│       ├── conversion/             # test_paths, test_multi_page
│       ├── drive/                  # test_client, test_models, test_paths, test_webhook
│       ├── sources/                # test_models, test_protocol
│       ├── state/                  # test_conversions, test_watch_channels, test_cursor, test_debounce, test_schema, test_page_conversions
│       ├── workflows/              # test_convert_note, test_poll_changes, test_delete_output, test_backfill, test_renew_watch, test_registration, test_locks
│       ├── test_logging.py
│       ├── test_main.py
│       └── test_observability.py
├── scripts/verify/                 # M0 gate scripts (Drive access, sn2md-Gemini)
├── docker/
│   ├── entrypoint.sh               # linuxserver-style PUID/PGID/TZ/UMASK handling
│   └── obsidian-sync/              # companion sync image: Dockerfile + entrypoint.sh (see §10)
├── Dockerfile                      # python:3.11-slim + gosu + tzdata + uv sync
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
| LLM provider       | `llm` + `llm-gemini`                      | llm-gemini ≥0.32 |
| Structured logging | `structlog` (or stdlib + json formatter)  | latest           |
| Testing            | `pytest`, `pytest-asyncio`                | current          |
| Quality            | `ruff`, `mypy`, `pre-commit`              | current          |

Python version pinned to **3.11** (sn2md's floor).

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
via `dbos.SQLAlchemyDatasource`, whose `@ds.transaction()` decorator
gives us atomic writes with DBOS's durability guarantees. One file, one
volume mount, one backup path.

```python
# src/sn2md_worker/db.py
from dbos import SQLAlchemyDatasource
datasource = SQLAlchemyDatasource.create(settings.database.url)
```

Then, from workflows:

```python
@datasource.transaction()
def upsert_conversion(...) -> None:
    session = datasource.sql_session()
    ...
```

Application tables managed via SQLAlchemy declarative models. **No
migration framework, no in-place migrations.** `Base.metadata.create_all`
runs on startup and that's it — any schema change (column add, index,
constraint) applies on the next fresh boot. Recovery path for any
break is nuking the SQLite file and letting `backfill` re-populate
`conversion_records` from Drive. Acceptable because Drive is source of
truth and the vault also lives in Obsidian Sync. Revisit Alembic if
we ever grow multiple deploys or state we can't rebuild from Drive.

`drive_watch_channels` has a partial unique index enforcing "at most
one row where `is_active = 1`" at the DB level; `drive_change_cursor`
has a `CHECK (id = 1)` constraint. Both are belt-and-suspenders on top
of the code that manages them.

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

An index on `current_file_id` lets `delete_output` find a record from a
removed change event. `PENDING` is written before a conversion starts
downloading (see §5.4): a crash mid-convert leaves a visible `PENDING`
row (surfaced by `/status.recent_pending`), and the retry treats the
note as stale because the up-to-date check requires `SUCCESS`.

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
| webhook_url | TEXT | Nullable; the URL this channel was registered with. `renew_watch_channel_impl` compares against `settings.webhook.url` and forces renewal on mismatch (e.g. ngrok URL changed) |
| expires_at | DATETIME| From Drive's response |
| start_page_token | TEXT | pageToken used at watch time |
| created_at | DATETIME| |
| is_active | BOOL | Only one row is_active=True at a time (except during overlap window) |

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
plain `<name>_impl(...)` function — the impl takes injected dependencies
plus `settings` (sometimes `now`) so tests bypass DBOS entirely. The
conversion-side impls (`convert_note`, `delete_output`, `backfill`)
take `source: NoteSource`, the ingestion seam from `sources/` (see §7);
the wrapper is the composition point that passes `get_drive_client()`
as `source=`. `poll_changes` and `renew_watch_channel` keep taking
`drive: DriveClient` on purpose: the change-feed/watch machinery is
Drive infrastructure that would be deleted, not abstracted, if a local
source ever replaced Drive. All impls follow the same log discipline:
`_started`, `_succeeded` / `_failed` / `_skipped(reason=…)` — see §14.
Every enqueued workflow also takes a trailing `correlation_id`
argument for end-to-end log tracing (mechanics in §14); the signature
snippets in this section show the core arguments and omit it.

Queues (registered after `DBOS.launch()`, names in
`workflows/queues.py`):

- `convert_queue` — `worker_concurrency=settings.queue.convert_concurrency`
  (default 2). Serves `convert_note` only.
- `delete_queue` — `worker_concurrency=2`. Serves `delete_output` on its
  own queue so long conversions don't stall stale-delete cleanup.
- `poll_queue` — `worker_concurrency=1`. Serves `poll_changes` and
  `backfill`.

Schedules (registered after `DBOS.launch()`; the register step
pre-checks each via `DBOS.get_schedule` so re-boots on the same DB are
a no-op):

- `renew-watch-channel` — cron `0 6 * * *` (daily 06:00 UTC). Runs
  `renew_watch_channel`.
- `fallback-poll-changes`: cron from `settings.drive.fallback_poll_cron`
  (default `*/5 * * * *`). Runs `scheduled_poll_changes`, which enqueues
  `poll_changes("fallback")` onto `poll_queue` so it serializes behind
  any webhook-triggered poll instead of racing it on the shared cursor
  (see §5.2).
- `backfill-sweep`: cron from `settings.drive.backfill_sweep_cron`
  (default `0 5 * * *`, daily 05:00 UTC). Runs `scheduled_backfill`,
  which enqueues `backfill` onto `poll_queue`: the safety net that
  re-drives conversions that failed permanently (for example Gemini
  throttle retries exhausted, §13) without waiting for a restart or a
  new edit of the note.

`convert_note` and `delete_output` both acquire a `filelock.FileLock`
via `workflows/locks.lock_for`, keyed on a SHA-256 of `logical_key`
(lockfiles under `/tmp/sn2md-worker-locks/`), before touching the
vault — a per-key mutex that serializes concurrent conversions of the
same note across workers and keeps a stale delete from rmtree-ing
under an in-flight convert.

### 5.1 `renew_watch_channel` — scheduled daily

```python
@DBOS.workflow()
def renew_watch_channel(scheduled_time: datetime, context: str) -> None:
    renew_watch_channel_impl(
        trigger_source=f"scheduled:{context}",
        drive=get_drive_client(),
        settings=get_settings(),
        now=now_utc(),
    )
```

(`now_utc()` is the `clock.py` seam: a single wall-clock accessor that
tests monkeypatch to a fixed instant. Everything that compares against
a persisted timestamp (channel expiry, renewal headroom, `/readyz`,
webhook auth) goes through it.)

`renew_watch_channel_impl` decides to renew when any of the following
holds:

- No webhook URL is configured → skip.
- No active channel row → create one.
- Active channel's `webhook_url` doesn't match `settings.webhook.url`
  → `drive.stop_channel(...)` best-effort on the old one, then create.
  This is the "ngrok URL changed" story — just edit `.env`, restart.
- Active channel's `expires_at - now` ≤ `RENEWAL_HEADROOM` (48h) → renew.

Otherwise skip with `reason="still_fresh"`. On a decision to renew,
it's a **two-phase** flow to survive a crash between Drive and the DB
write:

1. Insert a _pending_ row (channel_id + token + placeholder
   `resource_id=""`, placeholder `expires_at = now + 7 days`, Drive's
   default TTL) BEFORE calling Drive. If we crash after Drive succeeds
   but before we commit the real values, the pending row still lets
   incoming webhook pushes authenticate; the 7-day placeholder keeps
   the webhook's expiry check from rejecting pushes that arrive
   between phase 1 and phase 2.
2. Call `drive.watch_changes(...)` — we don't pass `expiration`, so
   Drive picks its own max (avoids host-clock skew).
3. On success: `confirm` the row with Drive's real `resource_id` and
   `expires_at`, then `mark_active`. On failure: `delete_by_id` rolls
   the pending row back.

When the renewal replaced an existing channel (near-expiry or URL
change), it also enqueues a catch-up `poll_changes("renewal")` on
`poll_queue`, covering the seam between the old channel's last
delivered push and the new channel going active. First-ever creation
(`active is None`) skips the catch-up: the startup backfill already
covers the initial state.

Startup helper `ensure_active_channel(drive, settings)` delegates to
the same impl with `trigger_source="startup"` and additionally enqueues
`poll_changes("recovery")` if the previously-active channel expired
while we were down.

### 5.2 `poll_changes` — enqueued by the webhook

```python
@DBOS.workflow()
def poll_changes(trigger_source: str) -> None:
    poll_changes_impl(trigger_source=trigger_source, drive=..., settings=...)
```

`poll_changes_impl` loads the persisted `page_token` (seeding via
`get_start_page_token` if missing), walks `changes.list` pages, and
dispatches each change:

- Removed → `enqueue_workflow(DELETE_QUEUE_NAME, delete_output, file_id)`
- Trashed → same as removed
- `.note` in the source-folder subtree →
  `enqueue_workflow(CONVERT_QUEUE_NAME, convert_note, file_id, source_path)`
- Anything else → ignored

**Cursor advances per page**, not once at the end — a crash mid-multi-
page walk resumes at the next unprocessed page. `resolve_source_path`
memoizes `drive.get_metadata` lookups for the duration of one
`poll_changes_impl` run, so sibling changes sharing ancestors don't
each pay the full ancestor walk.

**Per-change exception handling**: catches `DrivePermanentError`
(4xx-except-429) — the file is gone / rejected, log-skip and continue.
`DriveTransientError` (5xx after retries, network) and any other
exception propagate to stall the workflow so DBOS retries from the
last-saved cursor.

**Cursor-expired fallback**: if `drive.changes_list` itself raises
`DrivePermanentError` (persisted cursor older than Drive's change-log
window), we reset via `get_start_page_token`, enqueue `backfill`, and
return cleanly.

**Fallback poll cron** (shipped 2026-07-12): the
`fallback-poll-changes` schedule runs `scheduled_poll_changes` on
`settings.drive.fallback_poll_cron` (default `*/5 * * * *`), which
enqueues `poll_changes("fallback")` onto `poll_queue`. This covers the
one case the boot-time paths can't: the process stays up but Google
silently drops a push (or stops delivering to a still-valid channel).
Startup backfill + the cursor-expired fallback above +
`ensure_active_channel`'s recovery poll on boot remain the
"worker was down" cover.

### 5.3 `debounce_file` — DEFERRED

Not built. The `debounce_state` table and its repo are in place so we
can add the workflow later without a schema change. Rationale: Drive
push notifications appear to only fire on completed uploads, so we've
enqueued `convert_note` directly from `poll_changes` without seeing bad
conversions in practice. Revisit if we observe partial-file
conversions.

### 5.4 `convert_note` — per-file, `convert_queue` (filelock-serialized)

```python
@DBOS.workflow()
def convert_note(file_id: str, source_path: str) -> None:
    convert_note_impl(
        file_id=file_id, source_path=source_path,
        source=get_drive_client(), settings=get_settings(),
    )
```

`convert_note_impl` flow:

1. Compute `logical_key(source_path)` — raises `UnsafePathError` on
   `..`, NUL, empty segments, or Windows-reserved names, which is
   log-skipped (DBOS won't retry).
2. Acquire the per-key `FileLock` (`workflows/locks.lock_for`, shared
   with `delete_output`) BEFORE the up-to-date check, so two workers
   can't both observe "stale" and both re-run.
3. `source.get_metadata(file_id)` — early return if trashed.
4. Idempotency check: if `conversion_records[logical_key]` has matching
   `source_md5`, `SUCCESS` status, and same `current_file_id`, skip.
5. Load per-page state: `page_conversions.list_for_note(session, key)` →
   `{page_index: page_md5}` dict.
6. Upsert the conversion record as `PENDING` before downloading:
   crash-visible to `/status.recent_pending`, and a retry treats the
   note as stale instead of up-to-date.
7. `source.download(file_id, tmp_dir, name)` → chunked
   `MediaIoBaseDownload` streaming; retried at whole-download level via
   tenacity.
8. `note_output_dir(source_path, vault_root)` gives the per-note vault
   folder (`<vault>/<parents>/<basename>/`).
9. `run_multi_page(...)` — extracts PNGs (sorted numerically by the
   first int in each filename), hashes each page, and reuses prior
   page bodies hash-first (see §6): Gemini runs only for hashes never
   transcribed before, via tenacity-wrapped retry (3 fast attempts for
   generic failures; 429-shaped throttle errors get 8 attempts with
   backoff up to 120s so a quota window is ridden out in place). Writes
   `page-NN.md` + `page-NN.png` per page and an `index.md`, firing
   `on_page_done` after each page so the workflow upserts that page's
   `page_conversions` row immediately: a crash mid-note resumes
   without re-transcribing completed pages.
10. Finalize: `conversions.mark_success` (an UPDATE, not a second
    upsert, so `attempts` isn't double-counted) and delete
    `page_conversions` rows for indexes ≥ current page count (pages
    removed from the note).
11. Sweep the note folder: remove stale `page-NN.md` / `page-NN.png`
    for dropped pages (both `.md` and PNG-only orphans) plus the
    legacy flat `<basename>.md` / `.sn2md.metadata.yaml` from
    pre-multi-page installs.

Any uncaught exception is logged as `convert_note_failed` (with
traceback) and re-raised so DBOS records the workflow as ERROR.

### 5.5 `delete_output` — per-file, `delete_queue` (filelock-serialized)

```python
@DBOS.workflow()
def delete_output(file_id: str) -> None:
    delete_output_impl(file_id=file_id, source=..., settings=...)
```

`delete_output_impl` handles Supernote's replace-then-delete semantics:

1. Look up `conversion_records` by `current_file_id`. No record → no-op.
2. Acquire the per-key `FileLock` for the record's `logical_key`
   (`workflows/locks.lock_for`, shared with `convert_note`) and
   re-read the record inside the lock: the decisive read serializes
   against an in-flight conversion of the same note.
3. If the record has a `parent_folder_id`, call
   `source.find_live_note(parent_folder_id, source_name)`. If a live
   file with the same name exists at that location with a different
   id, **re-point** `current_file_id` to it (the newer Supernote-side
   upload) via `conversions.set_current_file_id`, **enqueue a
   follow-up `convert_note(live.id, source_path)`** on
   `CONVERT_QUEUE_NAME` (in case `poll_changes` never saw the
   create-side push), and skip the vault delete.
4. Otherwise: `_delete_from_vault(output_rel_path, vault_root)` — a
   guarded `shutil.rmtree` that refuses empty paths, paths that resolve
   outside `vault_root`, or `vault_root` itself.
5. Delete the conversion record and all of its `page_conversions`
   rows.

The `find_live_note` lookup is a single `files.list` call with
`q="name = '<x>' and trashed = false and '<parent_folder_id>' in parents"`.
Names containing non-printable characters (`str.isprintable() == False`)
are refused before the query — Google's query language quotes with `'`
and control chars could inject.

### 5.6 `backfill` — startup one-shot, `poll_queue`

```python
@DBOS.workflow()
def backfill() -> None:
    backfill_impl(source=get_drive_client(), settings=get_settings())
```

Iterates `source.list_all_notes(source_folder_id)`, which depth-first
walks the source folder tree and yields a `ListedNote(metadata,
source_path)` for every `.note`. **All conversion records are loaded
once up front** via `conversions.list_all_by_key(session)` — the
per-file check is a Python dict lookup, not another SQLite hit. For
each: if no matching `logical_key`, or the stored md5 differs, or
`last_status != SUCCESS`, enqueue `convert_note`. A path that fails
`logical_key`'s safety checks is logged and counted (`unsafe`) without
aborting the walk.

Enqueued at startup via `workflows.enqueue_startup_backfill()` (only if
a `DriveClient` was successfully initialized), and re-enqueued by
`poll_changes` when the persisted cursor is rejected as expired.

## 6. sn2md integration

Chosen path: **use as a library**, not subprocess. We **don't** call
`import_supernote_file_core` — that's the single-file wrapper we
outgrew when multi-page caching became a requirement. Instead we drive
sn2md's per-page primitives ourselves in `conversion/multi_page.py`:

- `NotebookExtractor.extract_images(file_name, output_path) → list[str]`
  — renders each `.note` page to PNG.
- `sn2md.ai_utils.image_to_markdown(png_path, context, api_key, model,
prompt) → str` — LLM call for a single page.

**Prompt**: `conversion.multi_page.DEFAULT_PROMPT` — sn2md's
`TO_MARKDOWN_TEMPLATE` verbatim as the base, with two additional
bullets appended: (1) ASCII-only characters inside `mermaid` code
blocks (Unicode arrows, en/em dashes, smart quotes, ellipses, and
emoji were breaking Mermaid's parser), (2) escape unbalanced
backticks / angle brackets / pipes in prose. Users can override the
whole thing with `[sn2md] prompt = "..."` in `config.toml`. Any
override must contain the `{context}` placeholder — the runner
substitutes the previous page's tail into it.

Output shape per note (under `note_output_dir(source_path, vault_root)`):

```
<basename>/
├── index.md          # Obsidian wikilinks: [[page-01]], [[page-02]], ...
├── page-01.md        # frontmatter (created, tags, page, of) + Gemini output + image link
├── page-01.png
├── page-02.md
├── page-02.png
```

Caching (hash-first since 2026-07-05): `run_multi_page` extracts +
hashes every page, then looks each hash up in an index of prior page
bodies (`_preload_cached_bodies`) built in two passes: DB-known
`page_conversions` rows keyed by their recorded md5, plus orphan
`page-NN.md` files on disk keyed by the sibling PNG's md5 (covers a
crash between the `.md` write and the DB upsert). Gemini runs only for
hashes never transcribed before; a matching body is reused regardless
of which index it previously lived at. Page files are rewritten
unconditionally (a cache hit at a shifted index still needs output at
the new slot), and `on_page_done` fires per page so the caller can
persist progress incrementally. Cache hits surface as
`PageOutcome(was_cached=True)` so callers see the full page list;
result counters (`gemini_calls`, `cache_hits`) get logged as
`convert_note_succeeded` fields.

**Notes / gotchas**:

- **We no longer use sn2md's `.sn2md.metadata.yaml` sidecar.** It was
  needed when we called `import_supernote_file_core`; multi-page mode
  doesn't touch it. If it lingers in the vault from a pre-multi-page
  install, `_cleanup_stale_pages` deletes it on the next convert.
- **Legacy flat `<basename>.md`** — same story. Removed on next
  convert.
- **Page reordering** — solved by the hash-first cache (2026-07-05).
  The `page_conversions` table still keys rows on
  `(logical_key, page_index)`, but body lookup is by page hash: a page
  inserted at position 2 costs one Gemini call for the new page, and
  pages 2..N reuse their cached bodies at their shifted indexes.
- **Model string**: use `gemini/gemini-2.5-pro` (with prefix). Verified
  2026-07-04 that the unprefixed `gemini-2.5-pro` doesn't resolve via
  `llm-gemini` 0.32+.
- **Measured baseline**: ~7.5s wall-clock per page against Gemini 2.5
  Pro. Scales linearly with the number of pages that missed the cache
  — a single new page on a 20-page note is ~7.5s (not 150s). Informs
  the `convert_concurrency=2` default.

## 7. Drive client (`drive/client.py`)

Wraps `googleapiclient.discovery.build("drive", "v3", ...)` with a service
account credential. Scope: `https://www.googleapis.com/auth/drive.readonly`
(we never write to Drive).

**NoteSource seam** (added 2026-07-12). `DriveClient` is the sole
implementation of the runtime-checkable `sources.NoteSource` Protocol:
`list_all_notes`, `get_metadata`, `download`, and `find_live_note` form
the surface the conversion workflows depend on, expressed in the
source-neutral DTOs from `sources/models.py`: `NoteMetadata` (the
renamed `drive/models.FileMetadata`; Drive's camelCase aliases still
map onto it) and `ListedNote(metadata, source_path)` (replacing the
old `(FileMetadata, str)` tuple yield). A `_note_source_conformance`
helper in `drive/client.py` fails mypy if the class drifts from the
protocol. The change-feed and watch machinery (`changes_list`,
`watch_changes`, `stop_channel`, `get_start_page_token`) deliberately
sits outside the seam: if a local source ever replaces Drive, that
machinery gets deleted, not reimplemented.

**Retry + error taxonomy**. All `.execute()` calls are routed through
`_call` → `_invoke_with_drive_retry` (tenacity, 3 attempts, exponential
backoff with jitter, retries 429/5xx + `ServerNotFoundError`/`SSLError`/
`TimeoutError`/`ConnectionError`). Terminal errors are classified by
`_drive_error_from_http`:

- `DrivePermanentError` — 4xx except 429 (retry exhausted for 429
  counts as transient). Callers can log-skip.
- `DriveTransientError` — 5xx after retries + all transport errors.
  Callers let it propagate so DBOS retries.
- Both subclass `DriveClientError` for backwards-compatible catches,
  and the hierarchy multiply-inherits from the source-neutral
  exceptions in `sources/protocol.py`: `DriveClientError` extends
  `SourceError`, `DrivePermanentError` extends `SourcePermanentError`,
  `DriveTransientError` extends `SourceTransientError`. Seam'd callers
  catch the `Source*` types without knowing the backend.

Public methods on `DriveClient`:

- `get_metadata(file_id, fields=DEFAULT_FILE_FIELDS) → NoteMetadata`
- `get_start_page_token() → str`
- `download(file_id, dest_dir, name) → Path` — chunked stream via
  `MediaIoBaseDownload` (4 MB chunks); truncate-and-restart on retry.
- `list_all_notes(folder_id) → Iterator[ListedNote]` —
  depth-first walk; per-folder listing dedupes file ids across pages
  (Drive can return the same id twice under concurrent edits).
- `find_live_note(parent_folder_id, name) → NoteMetadata | None` — scoped
  `files.list` with `q="name = '<x>' and trashed = false and '<pid>' in parents"`.
  Refuses non-printable names (`str.isprintable()`).
- `watch_changes(webhook_url, channel_id, token, start_page_token) → ChannelInfo` —
  no `expiration` in the request, so Drive picks its own max TTL.
- `stop_channel(channel_id, resource_id)` — swallows 404 (channel
  already gone); other errors surface via `_drive_error_from_http`.
- `changes_list(page_token, ...) → ChangesPage`

Path resolution moved out of `DriveClient`: `drive/paths.py` exposes a
pure `resolve_source_path(file_id, root_folder_id, get_metadata) → str | None`
that walks _every_ parent chain (multi-parent legacy files fall back
past a stray parent that dead-ends), max depth 100. `poll_changes`
memoizes `drive.get_metadata` for the duration of one run and passes
the memoized callable in.

Changes-list defaults:

- `restrictToMyDrive=false`
- `includeRemoved=true`
- `spaces="drive"`
- `fields="nextPageToken,newStartPageToken,changes(fileId,removed,time,file(id,name,md5Checksum,parents,mimeType,trashed,modifiedTime))"`

## 8. Webhook (`drive/webhook.py`)

```
POST /webhooks/drive
Headers of interest:
  X-Goog-Channel-Id, X-Goog-Channel-Token,
  X-Goog-Resource-Id, X-Goog-Resource-State, X-Goog-Message-Number
```

Route is `def` (not `async def`) so FastAPI runs it in its threadpool
— the body does blocking SQLA + DBOS work.

Handler:

1. Return 200 immediately for `X-Goog-Resource-State: sync` (initial
   handshake).
2. Look up channel by `X-Goog-Channel-Id`; if unknown OR the channel's
   `expires_at` is in the past, return 200 and log (a stale channel).
3. Verify `X-Goog-Channel-Token` matches persisted token via
   `hmac.compare_digest` (constant-time); else 200 + warn.
4. Mint a `correlation_id` (`new_correlation_id()`, see §14) and
   enqueue `poll_changes` with `trigger_source="webhook"` and
   `SetEnqueueOptions(deduplication_id=f"{channel_id}:{message_number}")`
   so Google's push retries (same message number) are dropped by DBOS
   with `DBOSQueueDeduplicatedError`, which we absorb as 200. The
   handler's log events carry both the HTTP `request_id` (contextvars)
   and the minted `correlation_id`, linking the request to the async
   chain.

Response codes: return 200 quickly; DBOS handles the actual work
asynchronously. Google retries on 5xx with exponential backoff — we do
not want to leverage that (we have our own poller for catch-up).

## 9. Configuration

See [`config.example.toml`](../config.example.toml) for the canonical
layout. Sections:

- `[drive]` — `source_folder_id`, `fallback_poll_cron` (default
  `*/5 * * * *`; drives the `fallback-poll-changes` schedule, §5.2),
  `backfill_sweep_cron` (default `0 5 * * *`; drives the
  `backfill-sweep` schedule, §5), debounce timing knobs (currently
  unused; kept for the deferred `debounce_file` workflow)
- `[vault]` — `root_path`, `mirror_source_layout`
- `[sn2md]` — `model` (default `gemini/gemini-2.5-pro`), `api_key`
  (SecretStr, optional), `prompt` (optional full-prompt override, §6;
  must keep the `{context}` placeholder).
  `workflows.convert_note._resolve_gemini_key`
  prefers `api_key` then falls back to the `LLM_GEMINI_KEY` env var.
- `[queue]` — `convert_concurrency` (default 2)
- `[observability]` — `log_level`, `status_endpoint_enabled`
- `[database]` — `url` (single SQLite/Postgres URL; DBOS + app tables
  share it via `SQLAlchemyDatasource`)
- `[webhook]` — `url` (public HTTPS the Google Drive push channel
  targets; empty in dev)
- `[google]` — `application_credentials` (path to the service account
  JSON key)

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

- [`Dockerfile`](../Dockerfile) — `python:3.11-slim`, uv-managed venv at
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
Obsidian Sync. Design points (all shipped by 2026-07-12):

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
| Container restart mid-conversion      | DBOS resumes workflow from last step                                                                                                                   |
| Watch channel expires without renewal | Daily `renew_watch_channel` cron creates a fresh channel; cursor preserves continuity between old and new channel                                      |
| Webhook URL changed (ngrok, DNS)      | `renew_watch_channel_impl` sees `webhook_url != settings.webhook.url` on next fire, calls `drive.stop_channel` on the old channel, creates a new one   |
| Gemini failure / rate limit           | 429-shaped errors retry in place on an extended budget (8 attempts, backoff to 120s, §5.4); on exhaustion, workflow ERROR and the daily `backfill-sweep` re-drives it (completed pages skipped)        |
| Malformed `.note`                     | sn2md raises; workflow terminates with ERROR; log + record status; do not retry                                                                        |
| Drive quota / auth error              | Workflow raises; log; next scheduled or manual retry                                                                                                   |
| SQLite locked / corrupted             | Container fails healthcheck; user restores from backup of `/data`                                                                                      |

## 14. Observability

### HTTP endpoints

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
    "queue_depth": { "convert_queue": 0, "poll_queue": 0 },
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
  `recent_pending` lists `PENDING` conversion records: in-flight
  converts, plus crash-stuck ones (a row that lingers here means a
  convert wrote `PENDING` and never finalized).
  `queue_depth` and `backfill` are read via raw SQL against DBOS's own
  `workflow_status` table (`observability._query_queue_depth` /
  `_query_backfill_status`). If the table is missing (early test setup,
  pre-`DBOS.launch()`), both fields fall back to zero / null defaults
  instead of erroring the endpoint. `startup` reads the process-wide
  `startup_status.StartupStatus` singleton written once at boot by
  `__main__.py`; each field is `"ok"` | `"deferred"` | `"failed"` (see
  [`docs/unraid-runbook.md#10-startup-model`](./unraid-runbook.md#10-startup-model)).
  When no startup has been recorded yet (early boot, tests without the
  entrypoint), every field defaults to `"deferred"`.

### Structured log events

`structlog` JSON to stdout. Every workflow emits (event names,
consistent across the codebase):

- `<workflow>_started` — entry, INFO.
- `<workflow>_succeeded` — happy exit, INFO. Includes outcome counters
  (`enqueued`, `skipped`) where relevant.
- `<workflow>_failed` — ERROR, `exc_info=True` (full traceback in the
  `exception` field), re-raised so DBOS records ERROR status.
- `<workflow>_skipped` — INFO or WARNING, always with a `reason=` field.
- Intermediate events (e.g. `poll_changes_enqueued`,
  `convert_note_running_multi_page`, `renew_watch_channel_created`)
  include concrete context on each.

Log levels: INFO for happy path, WARNING for graceful degradation
(config missing, safety guard tripped), ERROR for failures.

**Health-probe access-log suppression.** `configure_logging` attaches
`HealthProbeAccessFilter` (in `logging.py`) to uvicorn's
`uvicorn.access` logger: access records for successful `GET /healthz`
and `GET /readyz` requests are dropped, so a 30-second container
healthcheck doesn't flood the JSON log. Failed probes (status 400 and
up) and all other traffic keep logging, and any record that doesn't
match uvicorn's five-argument access format is kept (fail open), so a
uvicorn upgrade can't silently eat access logs.

### Correlation IDs

Every log line inside a request or workflow scope carries context that
makes end-to-end tracing possible. Two ids, per best practice:
`request_id` is scoped to one HTTP request; `correlation_id` follows
one logical operation across DBOS enqueue boundaries.

- **HTTP requests** — `app.RequestIdMiddleware` binds `request_id`
  (from an incoming `X-Request-Id` header, else a fresh 16-char uuid4
  hex), plus `method` and `path`, into structlog's contextvars for the
  duration of the request. The same id is echoed back in the response
  header. Grep the JSON log for `request_id` to follow a request end
  to end.
- **Workflows** — each `<workflow>_impl` opens
  `structlog.contextvars.bound_contextvars(workflow=..., file_id=...,
logical_key=..., trigger=..., correlation_id=...)`. Every log line
  inside the scope auto-picks up those fields so individual call
  sites don't repeat them.

Correlation across the enqueue boundary (webhook → `poll_changes`
worker → `convert_note` worker) rides on `correlation_id`.
`new_correlation_id()` in `correlation.py` mints one at each root
trigger: a webhook notification received, a fallback-poll cron tick, a
renewal run's catch-up poll, boot-time channel recovery (a single id
shared by the recovery poll and the renewal it drives), and the
startup backfill. Contextvars don't cross DBOS worker threads, but
workflow arguments are persisted, so the id travels as an explicit
trailing workflow argument (`poll_changes(trigger_source, correlation_id)`,
`convert_note(file_id, source_path, correlation_id)`,
`delete_output(file_id, correlation_id)`, `backfill(correlation_id)`),
gets bound into each workflow's structlog context, and is inherited
unchanged by every child enqueue: poll to convert / delete / backfill,
backfill to convert, and delete's re-point follow-up convert. A
workflow invoked without one (an in-flight replay from before this
shipped) mints its own, so every workflow log line carries a
`correlation_id`. The webhook's log events include both `request_id`
(from contextvars) and the `correlation_id` it minted, linking the
HTTP scope to the async chain. Workflow wrappers additionally bind
`dbos_workflow_id` (from `DBOS.workflow_id`) so any log line also
links back to DBOS's own workflow row for retry forensics.

## 15. Testing strategy

Unit tests run against in-memory SQLite. All tests live under
`tests/unit/` mirroring the source layout (per-project convention).
Two shapes:

- **BDD scenario classes** for behavior — `TestWhen<Scenario>` with
  `test_<expected_outcome>` methods and explicit `# GIVEN / # WHEN /
  # THEN` markers inside. Used for: webhook handler, all workflow impls,
  the state repos, observability endpoints.
- **Plain function tests** for pure logic — path helpers (`drive/paths.py`,
  `conversion/paths.py`), Drive model alias mapping, the `UTCDateTime`
  TypeDecorator.

External dependencies are patched at the call boundary:

- Note source: the seam'd workflow tests (`convert_note`,
  `delete_output`, `backfill`) use `MagicMock(spec=NoteSource)`;
  `poll_changes` / `renew_watch` tests use
  `MagicMock(spec=DriveClient)`. Per-method return values / side
  effects set inside `# GIVEN`.
- `run_multi_page` — `patch("sn2md_worker.workflows.convert_note.run_multi_page")`
  in the workflow tests. `test_multi_page.py` patches sn2md's
  `NotebookExtractor.extract_images` and `image_to_markdown` directly.
- `DBOS.enqueue_workflow` — `patch(...)` in the workflow module.

No `respx`, no live-API tests in CI. Manual verification against real
Drive + Gemini is via `scripts/verify/`.

## 16. Verifications (all resolved)

1. **Service account changes feed** — ✅ Resolved 2026-07-04 via
   `scripts/verify/01_drive_access.py`. Personal-user edits on shared
   files DO appear in the service account's `changes.list` feed. Bonus
   finding: Supernote's sync-replace semantics (§4a).
2. **DBOS runtime shape** — ✅ Runs as an embedded library inside a
   single Python process; no DBOS Conductor required. Verified in the
   Docker smoke test.
3. **DBOS SQLite under our load pattern** — ✅ Sustained scheduled +
   queued workflows through repeated boots; the single SQLite file
   holds both DBOS state and our tables cleanly. No data loss observed.
4. **sn2md-as-library end-to-end** — ✅ Resolved 2026-07-04.
   `import_supernote_file_core` with `model="gemini/gemini-2.5-pro"` +
   `LLM_GEMINI_KEY` produces sensible Markdown. ~7.5s/page baseline.
   (The verify script exercises sn2md's high-level single-file
   wrapper; the shipped worker drives the per-page primitives
   instead, see §6. Both scripts stay useful as smoke tests if the
   setup ever needs re-verification.)
5. **Webhook reachability from Google** — ✅ Resolved 2026-07-12 in the
   real deployment (production DNS + reverse proxy on Unraid).
   Google's push POSTs reach the container. The webhook handler,
   authentication, and enqueue path were already unit-tested; the live
   deploy confirmed end-to-end delivery.

## 17. Milestones

- **M0** — ✅ Verification scripts, both gates passed (see §16).
- **M1** — ✅ FastAPI + DBOS + health endpoints + Drive client +
  `/webhooks/drive` route with authentication.
- **M2** — ✅ Conversion path: `convert_note` + per-page cached runner
  (`conversion/multi_page.py`) + path helpers + state schema
  (`conversion_records` + `page_conversions`) + DBOS singletons wired.
  `debounce_file` deferred (see §5.3).
- **M3** — ✅ `poll_changes`, `renew_watch_channel`, `delete_output`,
  `backfill`. Cursor lifecycle + token verification in place.
- **M4** — ✅ `/status`, real `/readyz`, Docker image (linuxserver
  PUID/PGID), CI (`ci.yml`), multi-arch release workflow
  (`release.yml`), standardized log events, BDD test refactor.

### Remaining polish (not blocking)

Still outstanding as of 2026-07-12:

- `debounce_file` stays deferred by choice (§5.3): schema + repo exist,
  the workflow doesn't, because Drive push appears to fire only on
  completed uploads.

Done since this list was written:

- Correlation id propagation across the DBOS enqueue boundary: shipped
  2026-07-12. A `correlation_id` minted at the root trigger travels as
  an explicit workflow argument through every child enqueue (§14).
- Webhook reachability from Google: confirmed 2026-07-12 in the real
  deployment (production DNS + reverse proxy on Unraid), resolving §16
  item 5.
- Multi-arch images published: `release.yml` builds both
  `sn2md-worker` and `obsidian-sync` for `linux/amd64` +
  `linux/arm64` on every push to main and semver tags (§10).
- Hash-first page cache: shipped 2026-07-05 with mid-note crash
  resume (§5.4 steps 6 and 9, §6). The old "inserted page re-runs
  downstream pages" limitation is gone.

### Shipped after M4 (2026-07-05 through 2026-07-12)

- Mid-note crash resume + hash-first page cache (2026-07-05):
  `PENDING` status, per-page persistence via `on_page_done`, body
  reuse keyed on page hash, shared per-key locks in
  `workflows/locks.py` (§5.4, §5.5, §6).
- Two-phase renewal placeholder fix: the pending channel row carries
  `expires_at = now + 7 days` so webhook auth accepts pushes during
  the phase-1 to phase-2 window (§5.1).
- Startup status reporting: `/status.startup` surfaces per-boot-step
  outcomes from the `startup_status` singleton (§14), plus the Unraid
  runbook and obsidian-sync deployment docs.
- obsidian-sync image hardening (2026-07-11 and 2026-07-12): fixed
  `99:100` non-root user replacing the linuxserver-style baseimage,
  env-driven login/vault pairing (`OBSIDIAN_*`), stale `.sync.lock`
  clearing (§10).
- `now_utc()` clock seam in `clock.py` (2026-07-11): single wall-clock
  accessor so time-sensitive checks stay deterministic in tests (§5.1).
- Fallback poll cron + renewal catch-up poll (2026-07-12): §5.1, §5.2.
- NoteSource ingestion seam (2026-07-12): `sources/` package;
  `convert_note` / `delete_output` / `backfill` depend on the protocol
  rather than on `DriveClient` (§5, §7).
- Health-probe access-log suppression (2026-07-12): §14.
- Correlation-id propagation (2026-07-12): `RequestIdMiddleware`
  (renamed from `CorrelationIdMiddleware`) keeps `request_id`
  HTTP-scoped; `correlation.py` mints a `correlation_id` at each root
  trigger, passed as a trailing workflow argument across every
  enqueue (§14).
- Throttle-aware Gemini backoff (2026-07-12): 429-shaped failures ride
  an extended in-place retry budget instead of failing the workflow
  after three attempts (§5.4, §13).
- Daily `backfill-sweep` schedule (2026-07-12): re-drives conversions
  that failed permanently, bounding their staleness to one day (§5,
  §13).

### Hardening shipped in the audit pass (2026-07-05)

Load-bearing subsystems worth naming; each one has its own tests:

- **SQLite tuning**: global `event.listens_for(Engine, "connect")` in
  `db.py` sets WAL / `busy_timeout=30000` / `synchronous=NORMAL` /
  foreign_keys on every SQLite connection in the process (ours + DBOS's).
  `_dbos_config` forwards `check_same_thread=False` + `timeout=30` to
  DBOS's `db_engine_kwargs` so its internal engines are threadpool-safe.
- **Atomic upserts**: every write in `state/*` is a single SQLite
  `INSERT ... ON CONFLICT DO UPDATE` — no get-then-set race.
- **Per-key filelock**: `convert_note` and `delete_output` share a
  `filelock.FileLock` (`workflows/locks.py`) keyed on a SHA-256 of
  `logical_key` (lockfiles under `/tmp/sn2md-worker-locks/`) so
  conversions and deletes of the same note serialize cleanly across
  workers.
- **Retry taxonomy**: `DrivePermanentError` vs `DriveTransientError`,
  multiply-inheriting from the source-neutral `SourcePermanentError` /
  `SourceTransientError` (see §7). Gemini calls in
  `conversion/multi_page` also retry via tenacity.
- **View dataclasses**: state repos return frozen `<Entity>View`
  dataclasses instead of ORM objects — callers can hold them past
  session close without any `DetachedInstanceError` risk.
- **Path safety**: `conversion/paths` rejects `..`, NUL, empty
  segments, and Windows-reserved names before anything reaches the
  vault. `find_live_note` refuses non-printable filenames.
- **Cursor recovery**: `poll_changes` catches `DrivePermanentError` on
  `changes.list`, resets the cursor via `get_start_page_token`, and
  enqueues a fresh `backfill`. `ensure_active_channel` on boot also
  enqueues a recovery poll if the previously-active channel expired
  during downtime.
- **DB-level singletons**: partial unique index on
  `drive_watch_channels(is_active) WHERE is_active = 1` and a
  `CHECK (id = 1)` on `drive_change_cursor`.
