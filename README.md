# sn2md-worker

[![CI](https://github.com/macdonaldr93/sn2md-worker/actions/workflows/ci.yml/badge.svg)](https://github.com/macdonaldr93/sn2md-worker/actions/workflows/ci.yml)

A background worker that watches a Google Drive folder for Supernote `.note`
files, converts them to Markdown with [`sn2md`](https://github.com/dsummersl/sn2md)
using Gemini 2.5 Pro, and drops the results into an Obsidian vault. Runs as
a single Docker container (linuxserver-style PUID/PGID) on a home server —
built for Unraid but should work anywhere Docker does.

- Product context — [`docs/product-brief.md`](docs/product-brief.md)
- Implementation design — [`docs/technical-brief.md`](docs/technical-brief.md)
- Verification scripts (M0 gates) — [`scripts/verify/README.md`](scripts/verify/README.md)

## How it works

```
Supernote ─sync→ Google Drive ─push notification→ /webhooks/drive
                                                       │
                                                       ▼
                    ┌─────── sn2md-worker (Docker) ────────┐
                    │  FastAPI  +  DBOS workflows           │
                    │  ┌─ poll_changes → convert_note ──┐  │
                    │  │                delete_output   │  │
                    │  │  renew_watch_channel (cron)     │  │
                    │  │  backfill (startup)             │  │
                    │  └── SQLite (DBOS + app tables) ──┘  │
                    │              │                        │
                    │              ▼                        │
                    │           /vault  (bind mount)        │
                    └───────────────────────────────────────┘
                                   │
                          Obsidian on the host
                                   │
                          Obsidian Sync → phones, laptop
```

Full architecture and workflow contracts in the technical brief.

## Quickstart

### 1. Prerequisites (one-time)

1. **GCP project + service account**
   - Create a GCP project, enable the Google Drive API.
   - Create a service account, download the JSON key.
   - Note the `client_email` from the JSON.
2. **Share the Drive folder** with the service account's email address as
   Viewer.
3. **Gemini API key** from [Google AI Studio](https://ai.google.dev).
4. **Public HTTPS URL** — route it through your reverse proxy (Cloudflare
   Tunnel, Nginx Proxy Manager, Traefik) to the container's port 8080,
   path `/webhooks/drive`.

Optional but recommended: run the two verification scripts in
[`scripts/verify/`](scripts/verify/README.md) before you deploy, to prove
the service account can see the folder's change feed and that sn2md can
transcribe a note via your Gemini key. Both scripts are self-contained
(`uv run scripts/verify/...`).

### 2. Deploy

Two options.

**From the published image** (recommended for Unraid — pulls the multi-arch
image from GitHub Container Registry, no local build):

```sh
docker pull ghcr.io/macdonaldr93/sn2md-worker:latest
```

Then adapt the `image:` line in your `docker-compose.yml` to
`ghcr.io/macdonaldr93/sn2md-worker:latest` and skip the `build:` block.
Available platforms: `linux/amd64`, `linux/arm64`.

**From source** (local build):

```sh
git clone https://github.com/macdonaldr93/sn2md-worker.git
cd sn2md-worker

cp .env.example .env
$EDITOR .env

# Put the service account JSON here — .gitignore already excludes it.
mkdir -p secrets && cp /path/to/sa.json secrets/service-account.json

docker compose up -d --build
```

`docker compose logs -f sn2md-worker` will show the structured JSON output.

### 3. Verify

```sh
curl http://localhost:8080/healthz     # liveness
curl http://localhost:8080/readyz      # 200 iff an active Drive watch exists
curl http://localhost:8080/status      # recent conversions, watch channel, cursor
```

The first startup enqueues a `backfill` that walks the source folder tree
and enqueues `convert_note` for every `.note` not yet in the vault.
Subsequent edits arrive via Drive push notifications.

## Configuration

Two overlapping surfaces:

- **`config.toml`** — file-based defaults. See
  [`config.example.toml`](config.example.toml). Optional; the container
  boots without one.
- **Environment variables** — `SN2MD_WORKER__SECTION__KEY` (double
  underscore for nesting) always overrides the file. This is the
  recommended surface for the docker-compose deployment.

### Required env vars

| Env var | Purpose |
|---|---|
| `LLM_GEMINI_KEY` | Gemini API key for the `llm-gemini` plugin |
| `SN2MD_WORKER__DRIVE__SOURCE_FOLDER_ID` | Drive folder ID the Supernote syncs into |
| `SN2MD_WORKER__WEBHOOK__URL` | Public HTTPS URL for Drive push notifications |

### linuxserver-style user / group

| Env var | Default | Notes |
|---|---|---|
| `PUID` | `1000` | Set to match your host user for correct file ownership under `/vault` |
| `PGID` | `1000` | Same |
| `TZ` | `Etc/UTC` | IANA timezone (e.g. `America/Toronto`) |
| `UMASK` | `022` | Standard permission mask for new files |
| `CHOWN_ON_START` | `true` | Set `false` to skip the startup chown pass on very large vaults |

### Volumes

| Container path | Purpose |
|---|---|
| `/data` | DBOS + application SQLite state (must be writable) |
| `/vault` | Obsidian vault directory (must be writable) |
| `/secrets/service-account.json` | Google service account JSON key (read-only) |

## Local development

```sh
uv sync                # deps + local package
uv run sn2md-worker    # boots on :8080
uv run pytest          # ~90 tests, in-memory SQLite
```

Pre-commit is wired up:

```sh
uv run pre-commit install
```

That runs ruff (check + format), mypy, and standard hygiene hooks on every
commit. CI runs the same set on push/PR.

### Overriding config locally

For development without editing `config.toml`, drop a `.env` in the repo
root (git-ignored) and set what you need. For the built-in run — not the
container — you'll typically want:

```sh
SN2MD_WORKER__GOOGLE__APPLICATION_CREDENTIALS=./secrets/service-account.json
SN2MD_WORKER__DRIVE__SOURCE_FOLDER_ID=<your-folder-id>
SN2MD_WORKER__VAULT__ROOT_PATH=/tmp/sn2md-vault
LLM_GEMINI_KEY=<your-key>
```

## Status

Milestone status per [tech brief §17](docs/technical-brief.md):

- ✅ M0 — verification scripts, service-account changes feed + sn2md-Gemini both proven
- ✅ M1 — FastAPI + DBOS + health endpoints + webhook route + Drive client
- ✅ M2 — conversion path (`convert_note`, sn2md runner, path helpers, state schema)
- ✅ M3 — `poll_changes`, `renew_watch_channel`, `delete_output`, `backfill`, cursor + token verification
- ⚙️ M4 — `/status` ✅, Docker + compose ✅, CI ✅; BDD test refactor and structured-log audit still pending
