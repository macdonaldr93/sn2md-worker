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
   pointer. `delete_output` checks for a live replacement file in Drive
   before removing vault output so it doesn't nuke a `.md` a preceding
   `convert_note` just wrote. When a file is moved or renamed in Drive
   (same `fileId`, new `logical_key`), `convert_note` seeds cached pages
   from the old vault folder and cleans up the old folder and DB rows
   after conversion. See tech-brief §4a.

2. **DBOS + SQLite share ONE file.** Our SQLAlchemy engine (`db.py`)
   sits alongside the DBOS-managed engine at the same URL. Our tables:
   `conversion_records`, `page_conversions`, `drive_watch_channels`,
   `drive_change_cursor`, `debounce_state`. Schema init in
   `state/schema.py` runs `Base.metadata.create_all` only.

3. **No migration framework, no in-place migrations.** `create_all`
   handles fresh installs; any schema change (column add, index add,
   constraint) applies on the next fresh boot. Recovery path for a
   schema break is nuking `data/sn2md-worker.sqlite` and letting
   `backfill` re-populate from Drive — cheap because Drive is source of
   truth. DBOS `application_version` is pinned (`"1"`), and any active
   workflows from a stale version are cancelled at startup so container
   upgrades don't leave orphaned queue deduplication locks.

4. **Workflow wrapper vs impl + DBOS step boundaries.** Every
   `@DBOS.workflow()` in `src/sn2md_worker/workflows/` is a thin wrapper
   that delegates to a `<name>_impl(...)` function taking injected
   dependencies (`source: NoteSource` for conversion workflows;
   `drive: DriveClient` for Drive change-feed/watch workflows). Any
   non-deterministic I/O (Drive, SQLite, filesystem) that gates child
   `DBOS.enqueue_workflow` calls is isolated inside a `@DBOS.step()`
   returning a frozen dataclass so DBOS recovery replay is deterministic.
   Tests exercise the impl directly. See
   [`workflows/CLAUDE.md`](src/sn2md_worker/workflows/CLAUDE.md).

5. **Startup ordering matters.** `__main__.py` sequences: load settings →
   configure logging → prep SQLite dir → create FastAPI app → construct
   `DBOS(...)` → init schema → wire singletons → init DriveClient →
   import `workflows` package (registers decorators) → seed change cursor
   → `DBOS.launch()` → cancel stale-version workflows → register queues
   → register schedules (idempotent) → ensure active watch channel →
   enqueue startup backfill. Queue and schedule registration require
   DBOS to be launched first.

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

8. **Load-bearing subsystems.**
   - **SQLite tuning** (`db.py`): a global SQLAlchemy connect listener
     sets `IMMEDIATE` transaction isolation, WAL mode,
     `busy_timeout=30000`, `synchronous=NORMAL`, and `foreign_keys=ON`
     on every SQLite connection in the process (ours and DBOS's). DBOS
     is also configured with `check_same_thread=False` and `timeout=30`
     so FastAPI's threadpool and DBOS's executor share connections safely.
   - **Atomic upserts** (`state/`): state repositories use SQLite
     `INSERT ... ON CONFLICT` statements (single statement per write)
     with no read-then-write races.
   - **Advisory locks** (`workflows/locks.py`): OS file locks in
     `/tmp/sn2md-worker-locks/` serialize concurrent operations per
     `logical_key` across workers (and serialize watch-channel renewal).
   - **Retry taxonomy** (`sources/`, `drive/`, `conversion/`): permanent
     4xx errors (`SourcePermanentError`, `SourceNotFoundError`) are
     skipped or recorded without retrying; transient errors
     (`SourceTransientError`: 5xx, 429, network) retry with backoff and
     propagate on exhaustion so DBOS retries the workflow. Gemini page
     transcription uses a dual retry budget in `conversion/gemini.py`
     (fast budget for generic failures, extended budget for 429 rate
     limits).
   - **Thread safety**: `DriveClient` uses thread-local HTTP transports
     with per-thread credential copies and a 60s timeout; `llm` plugin
     discovery is serialized behind a lock; SQLite-touching FastAPI
     routes are synchronous `def` handlers dispatched to FastAPI's
     threadpool.
   - **View dataclasses** (`state/`): repository getters return frozen
     `*View` dataclasses rather than ORM instances so callers never hold
     live ORM objects across session boundaries.
   - **Path sanitization** (`conversion/paths.py`, `drive/client.py`):
     rejects `..`, NUL, empty segments, and Windows-reserved names
     before anything reaches the vault, and refuses non-printable
     filenames in Drive queries.

## Where the code lives

```
src/sn2md_worker/
├── __main__.py         entrypoint + startup sequencing
├── app.py              FastAPI factory + request-id middleware
├── clock.py            UTC wall-clock seam for tests
├── config.py           Settings (pydantic-settings) + singleton
├── correlation.py      correlation_id minting for cross-workflow tracing
├── db.py               engine singleton, sql_session(), SQLite tuning
├── logging.py          structlog + stdlib JSON setup
├── observability.py    /healthz /readyz /status
├── startup_status.py   per-step boot outcome reporting
├── sources/            NoteSource protocol + source-neutral DTOs/errors
├── drive/              DriveClient, change-feed models, path resolver, webhook
├── conversion/         vault paths, per-page runner, Gemini transcription
├── state/              SQLAlchemy models + per-table repositories
└── workflows/          DBOS workflows, queues, and advisory file locks
```

## Common commands

```sh
uv sync                      # install deps + local package
uv run pytest                # full unit suite
uv run ruff check src tests scripts
uv run ruff format src tests scripts
uv run mypy src
uv run sn2md-worker          # boot locally on :8080
uv run pre-commit run --all-files

docker build -t sn2md-worker:test .
docker compose up -d --build
```

## Verification scripts

`scripts/verify/` has two self-contained (PEP 723) smoke-test scripts
against real APIs:

- `01_drive_access.py` — service account can see + poll changes on a
  personal Drive folder shared to it.
- `02_sn2md_gemini.py` — sn2md's Python API produces sensible Markdown
  via Gemini (uses sn2md's high-level single-file API;
  the running worker uses per-page extraction and transcription instead).

## Coding conventions honored across this project

- **Public methods at the top** of a file, private helpers below.
- **Tests mirror source folder structure** — `tests/unit/drive/test_paths.py`
  tests `src/sn2md_worker/drive/paths.py`.
- **BDD-style tests for behavior** — `TestWhen<Scenario>` class per
  scenario, `# GIVEN / # WHEN / # THEN` markers inside. See
  [`tests/unit/CLAUDE.md`](tests/unit/CLAUDE.md).
- **Pydantic for validation boundaries; dataclasses for internal DTOs**
  (`ConversionUpsert`, `NewWatchChannel`, DBOS step outputs).
- **Structured log events** follow `<workflow>_started` /
  `_succeeded` / `_failed` / `_skipped(reason=…)`. See tech-brief §14.
- **Conventional commit** subject lines, one-liner, no
  `Co-Authored-By` trailer.
- **Draft PRs by default** — open pull requests with `gh pr create --draft`.
- **Documentation stays high-level and version-free** — keep docs concise,
  describe high-level concepts rather than private function names, and
  never hardcode specific Gemini model versions or dependency versions in
  prose (defer to `pyproject.toml` and `config.example.toml` as the
  single sources of truth).
- **CI scripts are tested Python modules** — put CI helper logic under
  `scripts/ci/` with unit tests under `tests/unit/ci/` and run via
  `uv run`, rather than inlining Python inside workflow YAML.
- **No migration framework by choice** — see item 3 above.

## Deferred by design (not bugs)

- `debounce_file` workflow — schema exists (`debounce_state` table +
  repo) but the workflow isn't built. Drive push notifications appear
  to only fire on completed uploads.
