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
    │   ├── POST /webhooks/drive                          │
    │   ├── GET  /healthz  /readyz                       │
    │   └── GET  /status                                 │
    │                                                    │
    │  DBOS runtime (single SQLite file: workflow state  │
│    + app tables via SQLAlchemyDatasource)          │
    │   ├── scheduled: renew_watch_channel   (~6d)       │
    │   ├── scheduled: poll_changes fallback (~5m)       │
    │   ├── enqueued : debounce_file         (per file)  │
    │   ├── enqueued : convert_note          (per file)  │
    │   └── enqueued : delete_output         (per file)  │
    │                                                    │
    │  sn2md library + llm + llm-gemini                  │
    │            │                                       │
    │            ▼                                       │
    │       /vault (bind mount)                          │
    └────────────────────────────────────────────────────┘
                             │
                             ▼
                Obsidian container on Unraid
                    (opens /vault, pushes
                    changes via Obsidian Sync)
                             │
                             ▼
                     Phones, laptop, etc.
```

Single process, single container. DBOS runs embedded (assumption to verify
at first milestone).

## 2. Repository layout

```
sn2md-worker/
├── docs/
│   ├── product-brief.md
│   └── technical-brief.md          ← you are here
├── src/sn2md_worker/
│   ├── __init__.py
│   ├── __main__.py                 # `python -m sn2md_worker`
│   ├── app.py                      # FastAPI + DBOS init, lifespan
│   ├── config.py                   # pydantic-settings, TOML + env
│   ├── db.py                       # SQLAlchemyDatasource singleton
│   ├── logging.py                  # structured JSON logger setup
│   ├── drive/
│   │   ├── client.py               # Google Drive API wrapper
│   │   ├── webhook.py              # /webhooks/drive route + verify
│   │   └── models.py               # pydantic models for Drive resources
│   ├── workflows/
│   │   ├── renew_watch.py
│   │   ├── poll_changes.py
│   │   ├── debounce_file.py
│   │   ├── convert_note.py
│   │   ├── delete_output.py
│   │   └── backfill.py
│   ├── conversion/
│   │   ├── runner.py               # sn2md invocation (library path)
│   │   └── paths.py                # Drive layout → vault path mapping
│   ├── state.py                    # SQLAlchemy models + DBOS DB helpers
│   └── observability.py            # /healthz, /readyz, /status
├── tests/
│   ├── unit/
│   └── integration/                # mocked Drive + Gemini
├── config.example.toml
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── uv.lock
├── .pre-commit-config.yaml
└── .github/workflows/ci.yml
```

## 3. External dependencies

| Concern            | Library / API                              | Version target             |
|--------------------|--------------------------------------------|----------------------------|
| HTTP server        | `fastapi` + `uvicorn`                      | current stable             |
| Config             | `pydantic-settings`                        | v2.x                       |
| Google Drive       | `google-api-python-client`, `google-auth`  | current                    |
| Durable workflows  | `dbos` (DBOS Transact)                     | current stable             |
| Note conversion    | `sn2md` (as library)                       | v2.7.0                     |
| LLM provider       | `llm` + `llm-gemini`                       | llm-gemini ≥0.32           |
| Structured logging | `structlog` (or stdlib + json formatter)   | latest                     |
| Testing            | `pytest`, `pytest-asyncio`, `respx`        | current                    |
| Quality            | `ruff`, `mypy`, `pre-commit`               | current                    |

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
migration framework** — `Base.metadata.create_all` runs on startup;
column adds/renames require nuking the SQLite file and letting the
`backfill` workflow re-populate `conversion_records` from Drive.
Acceptable because Drive is source of truth and the vault also lives in
Obsidian Sync. Revisit Alembic the first time that recovery path stops
being cheap.

**`conversion_records`** — one row per **logical** `.note` (Drive path +
name), because Supernote sync replaces the file rather than updating in
place (see §4a).

| column           | type    | notes                                       |
|------------------|---------|---------------------------------------------|
| logical_key      | TEXT PK | `<parent_path>/<name>` — stable across device edits |
| current_file_id  | TEXT    | Latest Drive file ID for this logical file; mutable |
| source_name      | TEXT    | Drive file name at last conversion          |
| source_path      | TEXT    | Full Drive path (folder chain / name)       |
| source_md5       | TEXT    | md5Checksum from Drive metadata             |
| output_rel_path  | TEXT    | Path relative to `/vault`                   |
| last_status      | TEXT    | SUCCESS \| ERROR \| SKIPPED                 |
| last_converted_at| DATETIME|                                             |
| attempts         | INTEGER |                                             |
| last_error       | TEXT    | Nullable, last error message                |

An index on `current_file_id` lets `delete_output` find a record from a
removed change event.

**`drive_watch_channels`** — one row per active/superseded push channel
| column         | type    | notes                                            |
|----------------|---------|--------------------------------------------------|
| channel_id     | TEXT PK | UUID we generate                                 |
| resource_id    | TEXT    | From Drive's response                            |
| token          | TEXT    | 32-byte hex, for authenticity check              |
| expires_at     | DATETIME| From Drive's response                            |
| start_page_token | TEXT  | pageToken used at watch time                     |
| created_at     | DATETIME|                                                  |
| is_active      | BOOL    | Only one row is_active=True at a time (except during overlap window) |

**`drive_change_cursor`** — singleton row
| column         | type    | notes                                            |
|----------------|---------|--------------------------------------------------|
| id             | INTEGER PK | fixed value 1                                 |
| page_token     | TEXT    | last saved cursor (or startPageToken)            |
| last_polled_at | DATETIME|                                                  |

**`debounce_state`** — one row per file currently being debounced
| column         | type    | notes                                            |
|----------------|---------|--------------------------------------------------|
| file_id        | TEXT PK |                                                  |
| last_size      | INTEGER |                                                  |
| last_md5       | TEXT    |                                                  |
| stable_since   | DATETIME| Nullable; set when size+md5 unchanged             |
| updated_at     | DATETIME|                                                  |

## 5. Workflows

All workflows are `@DBOS.workflow()`; helper functions that touch external
systems are `@DBOS.step()` so DBOS records their return values and skips
re-execution on recovery.

### 5.1 `renew_watch_channel` — scheduled every 6 days (UTC)

```python
@DBOS.workflow()
def renew_watch_channel(scheduled_time: datetime, context: dict) -> None:
    active = get_active_channel()
    if active and (active.expires_at - now()) > timedelta(hours=24):
        return  # nothing to do
    new_channel = drive.create_change_watch(
        webhook_url=settings.webhook_url,
        token=secrets.token_hex(16),
        start_page_token=state.get_or_fetch_page_token(),
    )
    persist_channel(new_channel, mark_active=True)
    # let the old channel expire naturally
```

Registered with `DBOS.create_schedule("renew-watch", renew_watch_channel,
"0 6 */6 * *")` (06:00 UTC every 6 days). `automatic_backfill=True` so a
missed run catches up on restart.

### 5.2 `poll_changes` — invoked by webhook, also scheduled every 5 minutes

```python
@DBOS.workflow()
def poll_changes(trigger_source: str) -> None:
    cursor = state.load_cursor()
    while True:
        page = drive.changes_list(page_token=cursor,
                                  include_removed=True,
                                  restrict_to_my_drive=False,
                                  fields="nextPageToken,newStartPageToken,changes(...)")
        for change in page.changes:
            handle_change(change)  # DBOS.step: enqueues downstream workflows
        if page.next_page_token:
            cursor = page.next_page_token
        else:
            cursor = page.new_start_page_token
            break
    state.save_cursor(cursor)
```

`handle_change` decides whether to enqueue `debounce_file` (for a
`.note` add/update within the source folder subtree) or `delete_output`
(for a removal). Non-`.note` changes and changes outside the source folder
are ignored.

### 5.3 `debounce_file` — per-file, queued to `debounce_queue`

```python
@DBOS.workflow()
def debounce_file(file_id: str) -> None:
    for _ in range(MAX_DEBOUNCE_ITER):
        DBOS.sleep(10)
        meta = drive.get_metadata(file_id, fields="size,md5Checksum,modifiedTime")
        stable = state.record_debounce_probe(file_id, meta)
        if stable_for(state.get(file_id), seconds=30):
            state.clear_debounce(file_id)
            DBOS.enqueue_workflow("convert_queue", convert_note, file_id)
            return
    logger.warn("debounce gave up", file_id=file_id)
    state.clear_debounce(file_id)
```

`DBOS.sleep` is durable — an interrupted debounce resumes after restart.

### 5.4 `convert_note` — per-file, queued to `convert_queue`

```python
@DBOS.workflow()
def convert_note(file_id: str) -> None:
    meta = drive.get_metadata(file_id, fields="id,name,parents,md5Checksum,trashed")
    if meta.trashed:
        return  # covered by delete_output
    source_path = drive.resolve_full_path(file_id)  # cached lookup
    logical_key = paths.logical_key(source_path)     # parent_path + name

    record = state.get_conversion_by_logical_key(logical_key)
    if record and record.source_md5 == meta.md5Checksum \
       and record.last_status == "SUCCESS" \
       and record.current_file_id == file_id:
        return  # already up to date

    tmp_path = drive.download_to_temp(file_id, meta.name)
    try:
        output_dir = paths.map_drive_to_vault(source_path)
        conversion.run_sn2md(tmp_path, output_dir)
        state.upsert_conversion(
            logical_key=logical_key,
            current_file_id=file_id,
            source_name=meta.name,
            source_path=source_path,
            source_md5=meta.md5Checksum,
            output_rel_path=paths.relative_to_vault(output_dir),
            status="SUCCESS",
        )
    finally:
        tmp_path.unlink(missing_ok=True)
```

`convert_queue` registered with `worker_concurrency=settings.convert_concurrency`
(default 2) and optional `limiter` for Gemini rate control.

### 5.5 `delete_output` — per-file, queued to `convert_queue`

```python
@DBOS.workflow()
def delete_output(file_id: str) -> None:
    record = state.get_conversion_by_current_file_id(file_id)
    if not record:
        return  # no record we're tracking under this file_id

    # Guard against Supernote's replace-semantics: a delete event for the
    # OLD file_id may arrive after we've already ingested the NEW file for
    # the same logical key. If Drive still has a live file at this
    # logical_key, the delete is stale — just drop the pointer, keep the .md.
    live = drive.find_live_by_logical_key(record.logical_key)
    if live is not None:
        if live.id != record.current_file_id:
            state.update_current_file_id(record.logical_key, live.id)
        return

    output_dir = pathlib.Path(settings.vault_root) / record.output_rel_path
    if output_dir.exists():
        shutil.rmtree(output_dir.parent if is_sn2md_note_dir(output_dir)
                      else output_dir)
    state.delete_conversion(record.logical_key)
```

sn2md puts each note in its own subdirectory
(`<output>/<file_basename>/<file_basename>.md` + assets), so we delete the
whole per-note subdirectory. `is_sn2md_note_dir` guards against surprises.

The `find_live_by_logical_key` lookup is a single `files.list` call with
`q="'<parent_id>' in parents and name='<name>' and trashed=false"` — the
cost is bounded and the safety win is significant.

### 5.6 `backfill` — runs once at startup

```python
@DBOS.workflow()
def backfill() -> None:
    for file in drive.list_all_notes(settings.source_folder_id):
        source_path = drive.resolve_full_path(file.id)
        logical_key = paths.logical_key(source_path)
        record = state.get_conversion_by_logical_key(logical_key)
        if not record or record.source_md5 != file.md5Checksum \
           or record.last_status != "SUCCESS":
            DBOS.enqueue_workflow("convert_queue", convert_note, file.id)
```

`list_all_notes` recursively walks the source folder tree (following
subfolders), filters to `.note` extension.

## 6. sn2md integration

Chosen path: **use as a library**, not subprocess.

```python
from sn2md.importer import import_supernote_file_core
from sn2md.importers.note import NotebookExtractor
from sn2md.types import Config

def run_sn2md(note_path: Path, output_dir: Path) -> None:
    cfg = Config(
        model=settings.sn2md_model,               # "gemini/gemini-2.5-pro"
        api_key=settings.gemini_api_key.get_secret_value(),
        # leave prompt/templates as sn2md defaults
    )
    import_supernote_file_core(
        image_extractor=NotebookExtractor(),
        file_name=str(note_path),
        output=str(output_dir),
        config=cfg,
        force=True,      # our own idempotency layer decides when to run
        progress=False,  # no tqdm bars in daemon logs
        model=None,
    )
    # Clean up sn2md's own idempotency sidecar; we don't consult it and
    # it's noise in the vault.
    for sidecar in output_dir.rglob(".sn2md.metadata.yaml"):
        sidecar.unlink(missing_ok=True)
```

**Notes / gotchas**:
- sn2md maintains its own `.sn2md.metadata.yaml` sidecar for
  idempotency, but its `check_metadata_file` **raises `ValueError` if the
  source is unchanged** and if the *output* was modified externally. Since
  we're the sole writer and we gate on our own `conversion_records`
  table, we bypass sn2md's check by passing `force=True`.
- After each successful `run_sn2md`, delete the `.sn2md.metadata.yaml`
  sidecar from the output directory. It has no value to us (we use our
  own `conversion_records`) and it's noise in the vault.
- sn2md's default output shape is `<output>/<file_basename>/<file_basename>.md`
  with images inside that subdir. We choose `output_dir` per note so the
  vault ends up mirroring Drive layout.
- sn2md's `image_to_text` also calls the LLM to decode Supernote H1–H4
  title highlights — every title costs one Gemini call. Not a bug, just
  worth knowing for rate-limit sizing.
- `sn2md` writes to stdout on success (`print(output_path_and_file)`). We
  redirect stdout or accept the noise; not critical.
- **Model string**: use `gemini/gemini-2.5-pro` (with prefix). Verified
  2026-07-04 that the unprefixed `gemini-2.5-pro` doesn't resolve via
  `llm-gemini` 0.32+.
- **Measured baseline**: ~7.5s wall-clock per single-page mostly-drawing
  note against Gemini 2.5 Pro. Scales linearly with page count. Informs
  the `convert_concurrency=2` default — Gemini rate-limit friendly, and
  a Supernote user's throughput doesn't need more.

## 7. Drive client (`drive/client.py`)

Wraps `googleapiclient.discovery.build("drive", "v3", ...)` with a service
account credential. Scope: `https://www.googleapis.com/auth/drive.readonly`
(we never write to Drive).

Methods:
- `create_change_watch(webhook_url, token, start_page_token) → ChannelRecord`
- `stop_channel(channel_id, resource_id) → None` (best-effort during
  container shutdown; not required — channels expire)
- `changes_list(page_token, ...) → ChangesPage`
- `get_start_page_token() → str`
- `get_metadata(file_id, fields) → FileMetadata`
- `download_to_temp(file_id, name) → Path`
- `list_all_notes(folder_id) → Iterator[FileMetadata]` (uses
  `files.list` with `q="'<id>' in parents and mimeType='application/vnd.google-apps.folder'"`
  recursively, then `q="'<id>' in parents and name contains '.note'"`)
- `resolve_full_path(file_id) → str` (walks `parents` chain to root, LRU-cached)

Change list params:
- `restrictToMyDrive=false`
- `includeRemoved=true`
- `spaces="drive"`
- `fields="nextPageToken,newStartPageToken,changes(fileId,removed,file(id,name,md5Checksum,parents,mimeType,trashed))"`

**Verification note** (see §11): whether the service account's changes
feed contains changes on files shared to it by a personal user is the
single most consequential runtime assumption. If it doesn't, fallback:
periodic `files.list?q="'<folder_id>' in parents and modifiedTime > '<iso>'"`
on the source folder + descendants.

## 8. Webhook (`drive/webhook.py`)

```
POST /webhooks/drive
Headers of interest:
  X-Goog-Channel-Id, X-Goog-Channel-Token,
  X-Goog-Resource-Id, X-Goog-Resource-State, X-Goog-Message-Number
```

Handler:
1. Return 200 immediately for `X-Goog-Resource-State: sync` (initial
   handshake).
2. Look up channel by `X-Goog-Channel-Id`; if unknown, return 200 and log
   (a stale channel we haven't cleaned up).
3. Verify `X-Goog-Channel-Token` matches persisted token; else 200 + warn.
4. Enqueue `poll_changes(trigger_source="webhook")` if not already
   running/pending. Return 200.

Response codes: return 200 quickly; DBOS handles the actual work
asynchronously. Google retries on 5xx with exponential backoff — we do
not want to leverage that (we have our own poller for catch-up).

## 9. Configuration

`config.example.toml`:
```toml
[drive]
source_folder_id = "REPLACE_ME"
poll_debounce_stable_seconds = 30
poll_debounce_interval_seconds = 10
poll_debounce_max_iterations = 60
watch_channel_ttl_days = 6
fallback_poll_cron = "*/5 * * * *"

[vault]
root_path = "/vault"
mirror_source_layout = true

[sn2md]
model = "gemini/gemini-2.5-pro"

[queue]
convert_concurrency = 2
convert_rate_limit_per_minute = 30
debounce_concurrency = 8

[observability]
log_level = "INFO"
status_endpoint_enabled = true

[database]
# Single SQLite/Postgres URL. DBOS's system tables and our own app tables
# both live here (via dbos.SQLAlchemyDatasource).
url = "sqlite:////data/sn2md-worker.sqlite"
```

Env override pattern (double underscore for nesting):
`SN2MD_WORKER__DRIVE__SOURCE_FOLDER_ID=abc123`

Secrets (never in the TOML, always env or mounted files):
- `LLM_GEMINI_KEY` — Gemini API key (name matches llm-gemini expectation)
- `GOOGLE_APPLICATION_CREDENTIALS=/secrets/service-account.json`
- `SN2MD_WORKER__WEBHOOK_URL` — public URL Google POSTs to
- `SN2MD_WORKER__WEBHOOK_SEED` — optional; if set, used to derive channel
  tokens deterministically (aids testing), else randomly generated per
  channel

## 10. Deployment

### Dockerfile

```dockerfile
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY src/sn2md_worker/ src/sn2md_worker/
COPY config.example.toml /app/config.example.toml

# Install the llm-gemini plugin into the uv-managed venv
RUN uv run llm install llm-gemini

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz').read()"

CMD ["uv", "run", "python", "-m", "sn2md_worker"]
```

### docker-compose.yml (reference)

```yaml
services:
  sn2md-worker:
    build: .
    restart: unless-stopped
    ports:
      - "8080:8080"
    env_file:
      - .env
    volumes:
      - /mnt/user/appdata/sn2md-worker/data:/data
      - /mnt/user/appdata/sn2md-worker/config.toml:/app/config.toml:ro
      - /mnt/user/appdata/sn2md-worker/service-account.json:/secrets/service-account.json:ro
      - /mnt/user/appdata/obsidian-vault:/vault
```

`.env` (git-ignored):
```
LLM_GEMINI_KEY=...
GOOGLE_APPLICATION_CREDENTIALS=/secrets/service-account.json
SN2MD_WORKER__DRIVE__SOURCE_FOLDER_ID=...
SN2MD_WORKER__WEBHOOK_URL=https://sn2md.example.com/webhooks/drive
```

### Reverse proxy

Route `sn2md.<domain>/webhooks/drive` → `sn2md-worker:8080/webhooks/drive`.
Terminate TLS at the proxy (Let's Encrypt is fine). Only expose the
webhook path publicly if you want to lock it down; `/status`,
`/healthz`, `/readyz` should be reachable only from your LAN.

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
     the Obsidian container and this worker (`/vault`).

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

| Scenario                                     | Recovery                                         |
|----------------------------------------------|--------------------------------------------------|
| Webhook missed (network glitch)              | Fallback `poll_changes` cron catches up          |
| Container restart mid-conversion             | DBOS resumes workflow from last step             |
| Watch channel expires without renewal        | Next fallback poll runs; renew workflow makes a fresh channel; cursor preserves continuity |
| Gemini rate limit hit                        | Queue `limiter` throttles; convert workflows retry via DBOS step retries |
| Malformed `.note`                            | sn2md raises; workflow terminates with ERROR; log + record status; do not retry |
| Drive quota / auth error                     | Workflow raises; log + retry with backoff (step-level) |
| SQLite locked / corrupted                    | Container fails healthcheck; user restores from backup of `/data` |

## 14. Observability

- `/healthz` — 200 if FastAPI is up.
- `/readyz` — 200 iff DBOS is initialized AND a `drive_watch_channels`
  row exists with `is_active=true` AND `expires_at > now`.
- `/status` — JSON:
  ```json
  {
    "recent_conversions": [{"file_id":"...", "status":"SUCCESS", "at":"..."}],
    "recent_failures":    [{"file_id":"...", "error":"...", "at":"..."}],
    "queue_depth":        {"convert_queue": 0, "debounce_queue": 1},
    "watch_channel":      {"id":"...","expires_at":"...","active":true},
    "cursor":             {"page_token":"...","last_polled_at":"..."},
    "backfill":           {"state":"COMPLETE","last_run_at":"..."}
  }
  ```
- Logs: JSON to stdout (`structlog`), one event per workflow start/step/end.
  Include `file_id`, `workflow_id`, `attempt` on every relevant line.

## 15. Testing strategy

- **Unit tests** (`tests/unit/`) — pure logic:
  - `paths.map_drive_to_vault`
  - debounce stability decision
  - webhook signature/token verification
  - conversion_records upsert logic
  - backfill diff
- **Integration tests** (`tests/integration/`) — with mocked externals:
  - Drive client using `respx` against the Discovery-built HTTP layer
    (mock at the transport, not the client abstraction).
  - sn2md invocation using a small canned `.note` fixture + mocked `llm`
    provider (patch `llm.get_model`).
  - Full flow: fake webhook POST → `poll_changes` → `convert_note` →
    file written to a tmp `/vault`.
- **No live-API tests in CI**. A manual smoke procedure (in
  `docs/smoke-test.md`, to be written) walks through a real end-to-end
  run against a scratch Drive folder + real Gemini key.

## 16. Open verification tasks (must resolve before merging first milestone)

Numbered in dependency order — 1 blocks everything else.

1. **Service account changes feed** — ✅ **RESOLVED 2026-07-04.**
   Verified via `scripts/verify/01_drive_access.py`: the service
   account's `changes.list` feed does carry personal-user edits on files
   shared to it. See §4a for the Supernote-specific sync semantics
   surfaced during this test.
2. **DBOS runtime shape**: confirm library-embedded (single process) is
   supported for production and doesn't require the DBOS Conductor
   control plane.
3. **DBOS SQLite under our load pattern**: sustained scheduled workflow
   + queued conversions + step retries, no data loss on kill -9.
4. **sn2md-as-library end-to-end** — ✅ **RESOLVED 2026-07-04.**
   `import_supernote_file_core` with `model="gemini/gemini-2.5-pro"` +
   `LLM_GEMINI_KEY` produces sensible Markdown (drawing tag, transcribed
   text, embedded image). ~7.5s per page baseline; output layout matches
   `<output>/<basename>/<basename>.md` + assets + `.sn2md.metadata.yaml`
   sidecar (we delete the sidecar in our runner).
5. **Webhook reachability from Google**: after DNS + reverse-proxy wiring,
   confirm Google can POST to the endpoint (they'll fail-silent otherwise).

## 17. Milestones

Suggested — pick apart or reorder as we go.

- **M0 — Prove the risky bits** (verification tasks 1, 4). No app code
  yet; ad-hoc scripts against real APIs.
- **M1 — Skeleton service** — FastAPI + DBOS + Drive client stub +
  health endpoints. No conversion yet. Deploys to Unraid, receives a
  webhook.
- **M2 — Conversion path** — `debounce_file` + `convert_note` +
  `conversion_records`. Manual test: drop a `.note` in Drive, observe
  Markdown in vault.
- **M3 — Renewal + backfill + deletion** — the workflows that keep it
  running and honest.
- **M4 — Observability + hardening** — `/status`, structured logs,
  concurrency/rate-limit tuning, CI green.
