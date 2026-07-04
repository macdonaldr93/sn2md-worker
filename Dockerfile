# syntax=docker/dockerfile:1.7

FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:${PATH}" \
    PUID=1000 \
    PGID=1000 \
    UMASK=022 \
    TZ=Etc/UTC \
    SN2MD_WORKER__DATABASE__URL="sqlite:////data/sn2md-worker.sqlite" \
    SN2MD_WORKER__VAULT__ROOT_PATH="/vault" \
    SN2MD_WORKER__GOOGLE__APPLICATION_CREDENTIALS="/secrets/service-account.json"

# System deps:
#   - gosu — drop from root to `app` at container start
#   - tzdata — resolve TZ env var to /etc/localtime
# uv installs into system Python.
RUN apt-get update \
 && apt-get install -y --no-install-recommends gosu tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir uv \
 && useradd --create-home --uid 1000 --home-dir /home/app app \
 && mkdir -p /app /data /vault /secrets \
 && chown -R app:app /app /data /vault

WORKDIR /app

# Metadata + sources copied before install so uv can build the local package.
COPY --chown=app:app pyproject.toml uv.lock README.md ./
COPY --chown=app:app src/ src/
COPY --chown=app:app config.example.toml ./

# Install deps + the local package as `app` — venv lands owned by `app` so
# the runtime user can invoke `uv run` without further chown.
RUN gosu app:app uv sync --frozen --no-dev

# Entrypoint script handles PUID/PGID/TZ/UMASK linuxserver-style, then execs
# the CMD as `app`. Kept under docker/ so the source is easy to review.
COPY docker/entrypoint.sh /usr/local/bin/sn2md-entrypoint
RUN chmod +x /usr/local/bin/sn2md-entrypoint

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import sys, urllib.request as u; sys.exit(0 if u.urlopen('http://localhost:8080/healthz').status == 200 else 1)"

# ENTRYPOINT runs as root, does the PUID/PGID switch, then drops to `app`
# to exec the CMD. Invoke python from the pre-built venv directly so `uv
# run` doesn't try to reconcile the environment at every start (which
# would pull dev deps back in).
ENTRYPOINT ["/usr/local/bin/sn2md-entrypoint"]
CMD ["/app/.venv/bin/python", "-m", "sn2md_worker"]
