"""
Uniform error model (RFC 9457 "Problem Details").

Every error the API returns has the same shape, so the Flutter client can
switch on a stable machine-readable `code` instead of parsing messages:

    {"type": "about:blank", "title": "Quota exceeded", "status": 402,
     "code": "quota_exceeded", "detail": "...", "request_id": "..."}
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

import logging

log = logging.getLogger("arwall.errors")


class AppError(Exception):
    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "bad_request"
    title: str = "Bad request"

    def __init__(self, detail: str | None = None, *, extra: dict[str, Any] | None = None):
        super().__init__(detail or self.title)
        self.detail = detail or self.title
        self.extra = extra or {}


class Unauthorized(AppError):
    status_code, code, title = 401, "unauthorized", "Authentication required"


class Forbidden(AppError):
    status_code, code, title = 403, "forbidden", "Not allowed"


class NotFound(AppError):
    status_code, code, title = 404, "not_found", "Not found"


class Conflict(AppError):
    status_code, code, title = 409, "conflict", "Conflict"


class QuotaExceeded(AppError):
    # 402 tells the client to show the upgrade paywall.
    status_code, code, title = 402, "quota_exceeded", "Plan limit reached"


class FeatureNotInPlan(AppError):
    status_code, code, title = 402, "upgrade_required", "Upgrade required"


class AccountLocked(AppError):
    status_code, code, title = 423, "account_locked", "Account temporarily locked"


class ServiceUnavailable(AppError):
    status_code, code, title = 503, "service_unavailable", "Dependency unavailable"


def _problem(request: Request, status_code: int, code: str, title: str,
             detail: str, extra: dict | None = None) -> JSONResponse:
    body = {
        "type": "about:blank",
        "title": title,
        "status": status_code,
        "code": code,
        "detail": detail,
        "request_id": getattr(request.state, "request_id", None),
        **(extra or {}),
    }
    return JSONResponse(body, status_code=status_code, media_type="application/problem+json")


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError):
        return _problem(request, exc.status_code, exc.code, exc.title, exc.detail, exc.extra)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        return _problem(request, 422, "validation_error", "Invalid request",
                        "One or more fields are invalid", {"errors": errors})

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        return _problem(request, exc.status_code, "http_error", str(exc.detail), str(exc.detail))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # Never leak internals; full trace goes to logs/Sentry with the request id.
        log.exception("unhandled_error", extra={"request_id": getattr(request.state, "request_id", None)})
        return _problem(request, 500, "internal_error", "Internal server error",
                        "An unexpected error occurred")
