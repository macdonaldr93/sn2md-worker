# CLAUDE.md — sn2md-worker

A single-user home service that converts Supernote `.note` files landing
in a Google Drive folder into Markdown inside an Obsidian vault. Runs as
a Docker container on Unraid.

- Product context — [`docs/product-brief.md`](docs/product-brief.md)
- Implementation design — [`docs/technical-brief.md`](docs/technical-brief.md)
- Unraid operations — [`docs/unraid-runbook.md`](docs/unraid-runbook.md)
- Nested guides: [`src/sn2md_worker/workflows/CLAUDE.md`](src/sn2md_worker/workflows/CLAUDE.md),
  [`tests/unit/CLAUDE.md`](tests/unit/CLAUDE.md)

## Non-obvious things worth knowing on day one

1. **Supernote sync replaces the Drive file** on every edit — new
   `fileId`, new md5, same name. `conversion_records` keys on
   `logical_key` (Drive path + name); `current_file_id` is a mutable
   pointer. `delete_output` consults `find_live_note` before removing
   vault output so it doesn't nuke a `.md` a preceding `convert_note`
   just wrote. See tech-brief §4a.

2. **DBOS + SQLite share ONE file.** `dbos.SQLAlchemyDatasource.create(url)`
   sits alongside the DBOS-managed engine at the same URL. Our tables:
   `conversion_records`, `page_conversions`, `drive_watch_channels`,
   `drive_change_cursor`, `debounce_state`. Schema init in
   `state/schema.py` runs `Base.metadata.create_all` only.

3. **No migration framework, no in-place migrations.** `create_all`
   handles fresh installs; any schema change (column add, index add,
   constraint) applies on the next fresh boot. Recovery path for a
   schema break is nuking `data/sn2md-worker.sqlite` and letting
   `backfill` re-populate from Drive — cheap because Drive is source of
   truth. Revisit Alembic if we ever grow multiple deploys or state we
   can't rebuild from Drive.

4. **Workflow wrapper vs impl pattern.** Every `@DBOS.workflow()` in
   `src/sn2md_worker/workflows/` is a thin wrapper that delegates to a
   `<name>_impl(...)` function taking injected dependencies. The seam'd
   impls (convert_note, backfill, delete_output) take
   `source: NoteSource` plus `settings`; poll_changes and renew_watch
   take `drive: DriveClient` because the change-feed/watch machinery is
   Drive infrastructure. Tests exercise the impl directly. See
   [`workflows/CLAUDE.md`](src/sn2md_worker/workflows/CLAUDE.md).

5. **Startup ordering matters and is fragile.** `__main__.py` sequences:
   load settings → configure logging → prep SQLite dir → `create_app` →
   `DBOS(...)` construct → `init_schema` → `set_settings/set_engine/set_datasource`
   → try DriveClient → import `workflows` package (registers decorators)
   → `seed_cursor_if_ready` → `DBOS.launch()` → `register_queues` →
   `register_schedules` (idempotent — pre-checks via `DBOS.get_schedule`
   before calling `create_schedule`) → `ensure_active_channel_if_ready`
   → `enqueue_startup_backfill`. `register_queue` and `create_schedule`
   need DBOS launched first (learned the hard way).

6. **Container-canonical paths are baked into the image**
   (`SN2MD_WORKER__DATABASE__URL`, `..VAULT__ROOT_PATH`,
   `..GOOGLE__APPLICATION_CREDENTIALS` as `ENV` in the Dockerfile) so
   the container boots without any config file. `.env` overrides.

7. **linuxserver-style entrypoint** (`docker/entrypoint.sh`) — the
   image starts as root, `usermod -o` reshapes the `app` user to match
   PUID/PGID, chowns writable volumes (`/data` and `/vault` only —
   `/app/.venv` is already app-owned at build time), then
   `exec gosu app:app "$@"`. CMD invokes `python` from
   `/app/.venv/bin/python` directly — do NOT go through `uv run` at
   container start (it triggers a dev-dep sync).

8. **Load-bearing subsystems worth naming.**
   - **SQLite tuning**: `db.py` registers a global connect listener that
     sets WAL + `busy_timeout=30000` + `synchronous=NORMAL` + foreign
     keys on every SQLite connection in the process (ours and DBOS's).
     `_dbos_config` also passes `check_same_thread=False` + `timeout=30`
     into DBOS's engine_kwargs — required for FastAPI's threadpool +
     DBOS's executor to share connections safely.
   - **Atomic upserts**: state repos use SQLite `INSERT ... ON CONFLICT
     DO UPDATE` (single statement per write). No get-then-set race.
   - **Per-key filelock** (`workflows/convert_note._lock_for`): OS
     advisory lock keyed on a SHA-256 of `logical_key`, lockfiles in
     `/tmp/sn2md-worker-locks/`. Serializes concurrent conversions of
     the same note across DBOS workers (and would across containers).
   - **Retry taxonomy**: `drive/client.py` distinguishes
     `DrivePermanentError` (4xx-except-429) from `DriveTransientError`
     (5xx / 429 / network); both subclass the source-neutral
     `SourcePermanentError` / `SourceTransientError` bases from
     `sources/`. The seam'd workflows (convert_note, backfill,
     delete_output) catch the neutral bases; poll_changes and
     renew_watch stay on the Drive-specific types (that machinery is
     Drive infrastructure). Permanent means log-skip; transient
     propagates so DBOS retries the workflow. Gemini retries via
     tenacity in `conversion/multi_page.py`.
   - **View dataclasses**: state repo getters return frozen
     `<Entity>View` dataclasses, not ORM instances. Callers can hold
     them past session close without `DetachedInstanceError`.
   - **Path sanitization**: `conversion/paths._reject_unsafe_component`
     rejects `..`, NUL, empty segments, and Windows-reserved names
     (`CON`/`NUL`/`COM[1-9]`/`LPT[1-9]`) before anything reaches the
     vault. `find_live_note` refuses non-printable filenames.

## Where the code lives

```
src/sn2md_worker/
├── __main__.py         entrypoint + startup sequencing
├── app.py              FastAPI factory
├── config.py           Settings (pydantic-settings) + singleton
├── db.py               engine + datasource singletons, sql_session(),
│                       global SQLite connect listener (WAL + 30s busy_timeout)
├── logging.py          structlog + stdlib JSON setup
├── observability.py    /healthz /readyz /status
├── sources/            NoteSource protocol + source-neutral models
│                       (NoteMetadata, ListedNote) and errors; the
│                       ingestion seam workflows depend on
├── drive/              DriveClient (implements NoteSource; retry-wrapped
│                       via tenacity), models, path resolver, webhook route
├── conversion/         paths + per-page runner (multi_page.py)
├── state/              SQLAlchemy models + per-table repos returning
│                       frozen `*View` dataclasses (detached-instance safe)
└── workflows/          DBOS workflows (see nested CLAUDE.md);
                        `queues.py` is a leaf module holding queue-name constants
```

## Common commands

```sh
uv sync                      # install deps + local package
uv run pytest                # 247 tests
uv run ruff check src tests scripts
uv run ruff format src tests scripts
uv run mypy src
uv run sn2md-worker          # boot locally on :8080
uv run pre-commit run --all-files

docker build -t sn2md-worker:test .
docker compose up -d --build
```

## Verification scripts

`scripts/verify/` has two self-contained (PEP 723) scripts that prove
the two load-bearing assumptions against real APIs:

- `01_drive_access.py` — service account can see + poll changes on a
  personal Drive folder shared to it.
- `02_sn2md_gemini.py` — sn2md's Python API produces sensible Markdown
  via `gemini/gemini-2.5-pro`. Note: this uses sn2md's high-level
  `import_supernote_file_core`; the running worker uses the per-page
  primitives instead (see `conversion/multi_page.py`).

Both were resolved 2026-07-04 and are documented in tech-brief §16.
Keep them as smoke tests if the setup ever needs re-verification.

## Coding conventions honored across this project

Some are user preferences saved to memory; some are project-specific:

- **Public methods at the top** of a file, private helpers below.
- **Tests mirror source folder structure** — `tests/unit/drive/test_paths.py`
  tests `src/sn2md_worker/drive/paths.py`.
- **BDD-style tests for behavior** — `TestWhen<Scenario>` class per
  scenario, `# GIVEN / # WHEN / # THEN` markers inside. See
  [`tests/unit/CLAUDE.md`](tests/unit/CLAUDE.md).
- **Pydantic for validation boundaries; dataclasses for internal DTOs**
  (`ConversionUpsert`, `NewWatchChannel` are plain frozen dataclasses).
- **Structured log events** follow `<workflow>_started` /
  `_succeeded` / `_failed` / `_skipped(reason=…)`. See tech-brief §14.
- **Conventional commit** subject lines, one-liner, no
  `Co-Authored-By` trailer (per user's global CLAUDE.md).
- **No migration framework by choice** — see item 3 above.

## Deferred by design (not bugs)

- `debounce_file` workflow — schema exists (`debounce_state` table +
  repo) but the workflow isn't built. Drive push notifications appear
  to only fire on completed uploads.
- ~~Fallback `poll_changes` cron~~ — now implemented. The
  `fallback-poll-changes` schedule (`register_schedules`) runs
  `scheduled_poll_changes` on `settings.drive.fallback_poll_cron`
  (default `*/5 * * * *`), enqueuing `poll_changes("fallback")`. This
  covers the case the boot-time paths can't: the process stays up but
  Google silently drops a push (or stops delivering to a still-valid
  channel). The boot-time paths remain the "worker was down" cover:
  startup `backfill`, the cursor-expired fallback in `poll_changes`
  (resets cursor via `get_start_page_token` + enqueues a fresh
  `backfill`), and `ensure_active_channel`'s recovery poll on boot.
  Renewal also enqueues a catch-up `poll_changes("renewal")` when it
  swaps an existing channel, closing the seam mid-swap.
- Hash-first page cache (v1 keys on `(logical_key, page_index)` — a
  page inserted mid-note re-runs downstream pages through Gemini).
  Fixable with hash-first matching if it becomes painful.
