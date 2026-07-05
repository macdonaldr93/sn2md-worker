# CLAUDE.md — sn2md-worker

A single-user home service that converts Supernote `.note` files landing
in a Google Drive folder into Markdown inside an Obsidian vault. Runs as
a Docker container on Unraid.

- Product context — [`docs/product-brief.md`](docs/product-brief.md)
- Implementation design — [`docs/technical-brief.md`](docs/technical-brief.md)
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
   `state/schema.py` runs `Base.metadata.create_all` plus a targeted
   `_apply_micro_migrations` for column additions on existing installs.

3. **No migration framework.** `create_all` + a small
   `_apply_micro_migrations` for nullable column adds. Rename/type-change
   the recovery path is nuking `data/sn2md-worker.sqlite` and letting
   `backfill` re-populate from Drive. Drive is source of truth. Revisit
   Alembic if this stops being cheap.

4. **Workflow wrapper vs impl pattern.** Every `@DBOS.workflow()` in
   `src/sn2md_worker/workflows/` is a thin wrapper that delegates to a
   `<name>_impl(...)` function taking injected dependencies (`drive`,
   `settings`, ...). Tests exercise the impl directly. See
   [`workflows/CLAUDE.md`](src/sn2md_worker/workflows/CLAUDE.md).

5. **Startup ordering matters and is fragile.** `__main__.py` sequences:
   load settings → configure logging → prep SQLite dir → `create_app` →
   `DBOS(...)` construct → `init_schema` → `set_settings/set_engine/set_datasource`
   → try DriveClient → import `workflows` package (registers decorators)
   → `seed_cursor_if_ready` → `DBOS.launch()` → `register_queues` →
   `register_schedules` (idempotent — `DBOS.create_schedule` raises on
   restart, we swallow "already exists") → `ensure_active_channel_if_ready`
   → `enqueue_startup_backfill`. `register_queue` and `create_schedule`
   need DBOS launched first (learned the hard way).

6. **Container-canonical paths are baked into the image**
   (`SN2MD_WORKER__DATABASE__URL`, `..VAULT__ROOT_PATH`,
   `..GOOGLE__APPLICATION_CREDENTIALS` as `ENV` in the Dockerfile) so
   the container boots without any config file. `.env` overrides.

7. **linuxserver-style entrypoint** (`docker/entrypoint.sh`) — the
   image starts as root, `usermod -o` reshapes the `app` user to match
   PUID/PGID, chowns writable volumes, then `exec gosu app:app "$@"`.
   CMD invokes `python` from `/app/.venv/bin/python` directly — do NOT
   go through `uv run` at container start (it triggers a dev-dep sync).

## Where the code lives

```
src/sn2md_worker/
├── __main__.py         entrypoint + startup sequencing
├── app.py              FastAPI factory
├── config.py           Settings (pydantic-settings) + singleton
├── db.py               engine + datasource singletons, sql_session()
├── logging.py          structlog + stdlib JSON setup
├── observability.py    /healthz /readyz /status
├── drive/              DriveClient, models, path resolver, webhook route
├── conversion/         paths (logical_key etc) + per-page runner (multi_page.py)
├── state/              SQLAlchemy models + per-table repos
└── workflows/          DBOS workflows (see nested CLAUDE.md)
```

## Common commands

```sh
uv sync                      # install deps + local package
uv run pytest                # 111 tests
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
- Fallback `poll_changes` cron — startup `backfill` covers "worker was
  down" and Drive push has been reliable in testing.
- Hash-first page cache (v1 keys on `(logical_key, page_index)` — a
  page inserted mid-note re-runs downstream pages through Gemini).
  Fixable with hash-first matching if it becomes painful.
