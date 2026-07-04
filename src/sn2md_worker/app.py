from __future__ import annotations

from fastapi import FastAPI

from sn2md_worker import __version__
from sn2md_worker.observability import router as observability_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="sn2md-worker",
        version=__version__,
        docs_url=None,
        redoc_url=None,
    )
    app.include_router(observability_router)
    return app


__all__ = ["create_app"]
