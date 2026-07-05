# syntax=docker/dockerfile:1.7

FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_COMPILE_BYTECODE=1 \
    PATH="/app/.venv/bin:${PATH}" \
    PUID=1000 \
    PGID=1000 \
    UMASK=022 \
    TZ=Etc/UTC \
    SN2MD_WORKER__DATABASE__URL="sqlite:////data/sn2md-worker.sqlite" \
    SN2MD_WORKER__VAULT__ROOT_PATH="/vault" \
    SN2MD_WORKER__GOOGLE__APPLICATION_CREDENTIALS="/secrets/service-account.json"

# gosu drops root → `app` at container start; tzdata resolves TZ.
# Removing docker-clean lets the apt cache mount actually populate.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
 && apt-get update \
 && apt-get install -y --no-install-recommends gosu tzdata \
 && useradd --create-home --uid 1000 --home-dir /home/app app \
 && mkdir -p /app /data /vault /secrets \
 && chown -R app:app /app /data /vault /home/app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# Split the sync in two so source edits skip the wheel-install layer:
# metadata → deps-only sync → src/ → project-only sync.
COPY --chown=app:app pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/home/app/.cache/uv,uid=1000,gid=1000 \
    gosu app:app uv sync --frozen --no-dev --no-install-project

COPY --chown=app:app src/ src/
RUN --mount=type=cache,target=/home/app/.cache/uv,uid=1000,gid=1000 \
    gosu app:app uv sync --frozen --no-dev

COPY docker/entrypoint.sh /usr/local/bin/sn2md-entrypoint
RUN chmod +x /usr/local/bin/sn2md-entrypoint

EXPOSE 8080

# `timeout=3` on urlopen — without it the socket can hang past Docker's
# 5s HEALTHCHECK timeout and leak until the process is killed.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import sys, urllib.request as u; sys.exit(0 if u.urlopen('http://localhost:8080/healthz', timeout=3).status == 200 else 1)"

# Invoke python directly from the venv — `uv run` would re-sync at every
# start and pull dev deps. CMD must stay single-process: DBOS + SQLite
# state is in-process and multi-worker would race the singletons.
ENTRYPOINT ["/usr/local/bin/sn2md-entrypoint"]
CMD ["/app/.venv/bin/python", "-m", "sn2md_worker"]
