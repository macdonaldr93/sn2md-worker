# sn2md-worker

[![CI](https://github.com/macdonaldr93/sn2md-worker/actions/workflows/ci.yml/badge.svg)](https://github.com/macdonaldr93/sn2md-worker/actions/workflows/ci.yml)

Watches a Google Drive folder for Supernote `.note` files, transcribes
each page with Gemini 2.5 Pro, and writes the results into an Obsidian
vault. Runs as a Docker container (linuxserver-style PUID/PGID), built
for Unraid but works anywhere Docker does.

- Product context — [`docs/product-brief.md`](docs/product-brief.md)
- Implementation design — [`docs/technical-brief.md`](docs/technical-brief.md)
- Contributor knowledge — [`CLAUDE.md`](CLAUDE.md) (start here if you're
  landing on this repo cold)

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

Each note becomes a folder with one Markdown file per page and an
`index.md` that links them. A page whose PNG hash matches the last
conversion skips Gemini — edit only the last page and only the last
page re-transcribes.

## Getting started (development)

### Prerequisites

- Docker + Docker Compose (Desktop is fine on macOS / Windows).
- [`uv`](https://docs.astral.sh/uv/) 0.5+ for local iteration outside
  the container.
- A Gemini API key and a Google Cloud service account JSON (see
  [`docs/technical-brief.md#11-prerequisites`](docs/technical-brief.md)
  for the click-through). For local live testing, ngrok gives you a
  public HTTPS URL the Drive push channel can hit — but you can also
  do all of development without ngrok since `backfill` at startup
  triggers the same conversion path.

### Clone, install, run the tests

```sh
git clone https://github.com/macdonaldr93/sn2md-worker.git
cd sn2md-worker

uv sync                        # installs deps + the local package into .venv
uv run pytest                  # 206 tests, in-memory SQLite, no network
uv run pre-commit install      # ruff + mypy + hygiene on every commit
```

### Boot locally (no Docker)

```sh
cp .env.example .env
$EDITOR .env                   # LLM_GEMINI_KEY, DRIVE__SOURCE_FOLDER_ID (webhook URL can stay empty)

mkdir -p secrets
cp /path/to/service-account.json secrets/service-account.json

uv run sn2md-worker            # http://localhost:8080
```

`/healthz`, `/readyz`, and `/status` are on 8080. With `WEBHOOK__URL`
empty the worker skips channel creation but still runs `backfill` at
startup, so you can iterate on the conversion path without any public
routing.

### Boot in Docker

The same `.env` and `secrets/` layout drives the container.

```sh
mkdir -p vault                 # gitignored; where output lands
docker compose up --build      # rebuild + attach logs

docker compose up -d --build   # detached
docker compose logs -f sn2md-worker | jq .   # structured JSON — jq is handy
```

Volumes bind by default to `./data`, `./vault`, `./secrets`. Override
via `DATA_DIR`, `VAULT_DIR`, `SECRETS_DIR` in `.env` if you want
different paths.

The image is linuxserver-style: `PUID`, `PGID`, `TZ`, `UMASK` in `.env`
control the runtime user. Set `PUID`/`PGID` to match your host UID so
files under `vault/` land with the right ownership.

### Testing live with ngrok

Drive push notifications need a publicly-trusted HTTPS URL. ngrok is
the quickest local option:

```sh
# terminal 1
ngrok http 8080                # note the https://<subdomain>.ngrok-free.app

# terminal 2 — set the webhook URL in .env, then bring the container up
# SN2MD_WORKER__WEBHOOK__URL=https://<subdomain>.ngrok-free.app/webhooks/drive
docker compose up --build -d
```

Look for `renew_watch_channel_created` in the logs. When you edit a
note on the Supernote and it syncs to Drive, you should see
`drive_webhook_notification` → `poll_changes_enqueued` →
`convert_note_started` → `convert_note_succeeded pages=N cache_hits=N-1`.

If you restart ngrok you'll get a new URL — update `.env` and
`docker compose restart`. The worker detects the URL change, stops the
old Drive channel, and creates a fresh one. No SQLite nuke required.

### Iterating

- **Code changes**: `uv run sn2md-worker` (or `docker compose up
  --build`) — no watcher yet.
- **Config changes**: env-var overrides (`SN2MD_WORKER__SECTION__KEY`)
  win over `config.toml`, always. Simplest is to edit `.env` and
  restart.
- **State reset**: `rm -rf data/sn2md-worker.sqlite` — startup
  `backfill` re-populates from Drive.
- **Vault reset**: `rm -rf vault/` — same idea; conversions will
  re-run.
- **Pre-commit hooks**: ruff auto-fixes on commit. If a hook rewrites
  a file, re-stage and commit again.

## Deploying

### From the published image (Unraid, headless servers)

```sh
docker pull ghcr.io/macdonaldr93/sn2md-worker:latest
```

Adapt the `image:` line in your `docker-compose.yml` to point at
`ghcr.io/macdonaldr93/sn2md-worker:latest` and drop the `build:`
block. Multi-arch: `linux/amd64` + `linux/arm64`.

### From source

Same three steps as local Docker above, but on the server. Wire your
reverse proxy so
`https://sn2md.<yourdomain>/webhooks/drive` reaches the container's
port 8080. Only expose `/webhooks/drive` publicly — `/status`,
`/healthz`, `/readyz` should be LAN-only or behind reverse-proxy auth.

## Configuration

Two overlapping surfaces:

- **`config.toml`** — file-based defaults (see
  [`config.example.toml`](config.example.toml)). Optional; the
  container's baked-in env vars mean it boots without one.
- **Environment variables** — `SN2MD_WORKER__SECTION__KEY` (double
  underscore for nesting) always wins. This is the recommended surface
  for Docker deployments.

### Required env vars

| Env var | Purpose |
|---|---|
| `LLM_GEMINI_KEY` | Gemini API key for the `llm-gemini` plugin |
| `SN2MD_WORKER__DRIVE__SOURCE_FOLDER_ID` | Drive folder ID the Supernote syncs into |
| `SN2MD_WORKER__WEBHOOK__URL` | Public HTTPS URL for Drive push (skip in dev; backfill covers you) |

### linuxserver-style user / group

| Env var | Default | Notes |
|---|---|---|
| `PUID` | `1000` | Match your host UID for correct file ownership under `/vault` |
| `PGID` | `1000` |  |
| `TZ` | `Etc/UTC` | IANA timezone (e.g. `America/Toronto`) |
| `UMASK` | `022` |  |
| `CHOWN_ON_START` | `true` | `false` skips the startup chown on huge vaults |

### Volumes

| Container path | Purpose |
|---|---|
| `/data` | DBOS + application SQLite state (writable) |
| `/vault` | Obsidian vault directory (writable) |
| `/secrets/service-account.json` | Google service account JSON (read-only) |

## Status

- ✅ M0 — verification scripts, service-account changes feed +
  sn2md/Gemini proven end-to-end.
- ✅ M1 — FastAPI + DBOS + health endpoints + Drive client +
  authenticated `/webhooks/drive`.
- ✅ M2 — conversion path with per-page caching (one `page-NN.md` per
  page + `index.md`).
- ✅ M3 — `poll_changes`, `renew_watch_channel` (auto-renews on URL
  change), `delete_output`, `backfill`.
- ✅ M4 — `/status` (with `queue_depth` + `backfill`), real
  `/readyz`, Docker image, CI, multi-arch release workflow,
  structured logs with correlation IDs.
- 🚧 Remaining polish tracked in
  [`docs/technical-brief.md#17-milestones`](docs/technical-brief.md).
