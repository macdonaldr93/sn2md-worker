from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import uuid4

import structlog
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from sn2md_worker import __version__
from sn2md_worker.drive.webhook import router as drive_webhook_router
from sn2md_worker.observability import router as observability_router

__all__ = ["REQUEST_ID_HEADER", "RequestIdMiddleware", "create_app"]

REQUEST_ID_HEADER = "X-Request-Id"


def create_app() -> FastAPI:
    app = FastAPI(
        title="sn2md-worker",
        version=__version__,
        docs_url=None,
        redoc_url=None,
    )
    app.add_middleware(RequestIdMiddleware)
    app.include_router(observability_router)
    app.include_router(drive_webhook_router)
    return app


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Bind a per-request id into structlog's contextvars.

    `request_id` is scoped to one inbound HTTP request: every log line
    emitted during the request lifecycle picks up `request_id`, `method`,
    and `path`, greppable from Docker logs without touching the
    individual log call sites. The id is echoed back in the response
    header so upstream callers can correlate. Work that outlives the
    request (DBOS enqueues) carries a separate `correlation_id` instead
    (see `sn2md_worker.correlation`).
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid4().hex[:16]
        with structlog.contextvars.bound_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        ):
            response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
