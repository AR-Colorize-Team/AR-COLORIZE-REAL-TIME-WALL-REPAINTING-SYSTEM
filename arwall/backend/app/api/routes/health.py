"""Liveness / readiness probes for the ALB target group and ECS."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.db.session import SessionLocal

router = APIRouter(tags=["health"], include_in_schema=False)


@router.get("/healthz")
async def liveness():
    """Process is up. Never touches dependencies (avoid restart storms on DB blips)."""
    return {"status": "ok"}


@router.get("/readyz")
async def readiness():
    """Ready to receive traffic: DB reachable."""
    try:
        async with SessionLocal() as s:
            await s.execute(text("SELECT 1"))
        return {"status": "ready"}
    except Exception:  # noqa: BLE001
        return JSONResponse({"status": "unavailable"}, status_code=503)
