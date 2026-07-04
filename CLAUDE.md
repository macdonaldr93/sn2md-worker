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
   sits alongside the DBOS-managed engine at the same URL. Table names
   don't collide (our tables are `conversion_records`, `drive_watch_channels`,
   `drive_change_cursor`, `debounce_state`). Schema init happens via
   `Base.metadata.create_all` in `state/schema.py`.

3. **No migration framework.** `create_all` runs at startup. Column
   adds/renames require nuking `data/sn2md-worker.sqlite` and letting
   the `backfill` workflow re-populate from Drive. Drive is source of
   truth. Revisit Alembic when this recovery path stops being cheap.

4. **Workflow wrapper vs impl pattern.** Every `@DBOS.workflow()` in
   `src/sn2md_worker/workflows/` is a thin wrapper that delegates to a
   `<name>_impl(...)` function taking injected dependencies (`drive`,
   `settings`, ...). Tests exercise the impl directly. See
   [`workflows/CLAUDE.md`](src/sn2md_worker/workflows/CLAUDE.md).

5. **Startup ordering matters and is fragile.** `__main__.py` sequences:
   load settings → configure logging → prep SQLite dir → `create_app` →
   `DBOS(...)` construct → `init_schema` → `set_settings/set_engine/set_datasource` →
   try DriveClient → import `workflows` package (registers decorators) →
   `seed_cursor` → `DBOS.launch()` → `register_queues` → `register_schedules` →
   `enqueue_startup_backfill`. `register_queue` and `create_schedule`
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
├── conversion/         path helpers (logical_key etc) + sn2md runner
├── state/              SQLAlchemy models + per-table repos
└── workflows/          DBOS workflows (see nested CLAUDE.md)
```

## Common commands

```sh
uv sync                      # install deps + local package
uv run pytest                # ~90 tests
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
- `02_sn2md_gemini.py` — `sn2md.import_supernote_file_core` produces
  sensible Markdown via `gemini/gemini-2.5-pro`.

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
- `queue_depth` and `backfill.state` fields on `/status` — DBOS
  doesn't expose queue depth cleanly and we don't track backfill runs
  separately. Task backlog items exist.
