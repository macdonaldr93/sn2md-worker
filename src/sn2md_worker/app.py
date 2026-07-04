from __future__ import annotations

from fastapi import FastAPI

from sn2md_worker import __version__
from sn2md_worker.drive.webhook import router as drive_webhook_router
from sn2md_worker.observability import router as observability_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="sn2md-worker",
        version=__version__,
        docs_url=None,
        redoc_url=None,
    )
    app.include_router(observability_router)
    app.include_router(drive_webhook_router)
    return app


__all__ = ["create_app"]
