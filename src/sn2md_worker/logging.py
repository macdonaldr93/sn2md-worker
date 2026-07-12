from __future__ import annotations

import logging
import sys
from typing import cast

import structlog

from sn2md_worker.config import LogLevel

__all__ = ["HealthProbeAccessFilter", "configure_logging", "get_logger"]

_PROBE_PATHS = frozenset({"/healthz", "/readyz"})
_ACCESS_LOG_ARG_COUNT = 5
_FIRST_ERROR_STATUS = 400


class HealthProbeAccessFilter(logging.Filter):
    """Drop uvicorn.access records for successful health-probe requests.

    Uvicorn (0.50.0, `protocols/http/h11_impl.py`) emits access lines as
    `access_logger.info('%s - "%s %s HTTP/%s" %d', client_addr, method,
    path_with_query_string, http_version, status_code)`. Container
    healthchecks hit `/healthz` every 30s, flooding the log with
    successful probe lines; this filter drops GET requests to exactly
    `/healthz` or `/readyz` when the status is below 400. Failed probes,
    `/status`, and all other traffic keep logging. Any record whose args
    don't match that five-tuple shape is kept (fail open) so a uvicorn
    upgrade can never silently eat access logs.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) != _ACCESS_LOG_ARG_COUNT:
            return True
        _, method, path_with_query, _, status_code = args
        if method != "GET" or not isinstance(path_with_query, str):
            return True
        if not isinstance(status_code, int) or status_code >= _FIRST_ERROR_STATUS:
            return True
        path = path_with_query.partition("?")[0]
        return path not in _PROBE_PATHS


def configure_logging(level: LogLevel) -> None:
    numeric_level = getattr(logging, level)

    logging.basicConfig(
        level=numeric_level,
        format="%(message)s",
        stream=sys.stdout,
        force=True,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    _install_health_probe_filter()


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return cast("structlog.stdlib.BoundLogger", structlog.get_logger(name))


def _install_health_probe_filter() -> None:
    """Attach the probe filter to uvicorn's access logger, at most once.

    `__main__` runs uvicorn with `log_config=None`, so uvicorn never
    reconfigures logging: `uvicorn.access` has no handlers of its own
    and propagates to the root handler installed by `basicConfig`
    above. Logger-level filters run before propagation, so attaching
    here is sufficient for the app's actual launch path.
    """
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, HealthProbeAccessFilter) for f in access_logger.filters):
        access_logger.addFilter(HealthProbeAccessFilter())
