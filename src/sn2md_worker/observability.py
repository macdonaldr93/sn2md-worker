from __future__ import annotations

from fastapi import APIRouter, status
from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: str


router = APIRouter(tags=["observability"])


@router.get("/healthz", status_code=status.HTTP_200_OK, response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/readyz", response_model=HealthResponse)
async def readyz() -> HealthResponse:
    # Real readiness checks (DBOS init, active watch channel) land in M3.
    return HealthResponse(status="ok")


__all__ = ["router"]
