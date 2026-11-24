"""
AR Wall Painting SaaS — API entrypoint.

Run locally:   uvicorn app.main:app --reload
Production:    gunicorn app.main:app -k uvicorn.workers.UvicornWorker -w 4 \
                   --bind 0.0.0.0:8000 --forwarded-allow-ips="*"   (behind ALB)
"""
from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from app.api.routes import admin, ai, auth, billing, catalog, health, me, palettes, projects
from app.core.config import get_settings
from app.core.errors import install_error_handlers
from app.db.session import engine


class JsonFormatter(logging.Formatter):
    """One JSON object per line → CloudWatch Logs Insights / Datadog friendly."""
    _skip = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        payload = {"ts": self.formatTime(record), "level": record.levelname,
                   "logger": record.name, "msg": record.getMessage()}
        payload.update({k: v for k, v in vars(record).items() if k not in self._skip})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await engine.dispose()


def create_app() -> FastAPI:
    s = get_settings()
    configure_logging(s.log_level)
    log = logging.getLogger("arwall.http")

    app = FastAPI(
        title=s.app_name,
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None if s.environment == "production" else "/docs",
        redoc_url=None,
        openapi_url=None if s.environment == "production" else "/openapi.json",
    )

    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.cors_origins,          # mobile apps don't need CORS; this is for the web dashboard
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
        max_age=600,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        request.state.request_id = request_id
        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        if request.url.path not in ("/healthz", "/readyz"):
            log.info("request", extra={
                "request_id": request_id, "method": request.method, "path": request.url.path,
                "status": response.status_code, "duration_ms": elapsed_ms,
                "user_id": str(getattr(request.state, "user_id", "")) or None,
            })
        return response

    install_error_handlers(app)

    app.include_router(health.router)
    p = s.api_prefix
    app.include_router(auth.router, prefix=p)
    app.include_router(me.router, prefix=p)
    app.include_router(projects.router, prefix=p)
    app.include_router(projects.assets_router, prefix=p)
    app.include_router(palettes.router, prefix=p)
    app.include_router(catalog.router, prefix=p)
    app.include_router(ai.router, prefix=p)
    app.include_router(ai.internal_router, prefix=p)
    app.include_router(billing.router, prefix=p)
    app.include_router(admin.router, prefix=p)
    return app


app = create_app()
