"""One error type for the whole API plus handlers that render it.

The signed-off frontend shows the response body text verbatim (`res.text()`), so by default errors are
plain text. Set ERROR_FORMAT=json for {"detail": ...} if you prefer that and update http.ts to match."""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings


class ApiError(Exception):
    def __init__(self, status: int, message: str, *, extra: dict[str, Any] | None = None, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status, self.message, self.extra, self.headers = status, message, extra or {}, headers or {}


def bad_request(msg: str) -> ApiError: return ApiError(400, msg)
def unauthorized(msg: str = "Not signed in") -> ApiError: return ApiError(401, msg, headers={"WWW-Authenticate": "Bearer"})
def forbidden(msg: str = "Your role cannot do that.") -> ApiError: return ApiError(403, msg)
def not_found(msg: str = "Not found") -> ApiError: return ApiError(404, msg)
def conflict(msg: str) -> ApiError: return ApiError(409, msg)
def unprocessable(msg: str, **extra: Any) -> ApiError: return ApiError(422, msg, extra=extra)


def _render(status: int, message: str, headers: dict[str, str] | None = None, extra: dict[str, Any] | None = None):
    if get_settings().error_format == "json":
        return JSONResponse({"detail": message, **(extra or {})}, status_code=status, headers=headers)
    return PlainTextResponse(message, status_code=status, headers=headers)


def _validation_message(exc: RequestValidationError) -> str:
    parts = []
    for e in exc.errors()[:5]:
        loc = ".".join(str(p) for p in e["loc"] if p not in ("body", "query", "path"))
        msg = str(e["msg"]).removeprefix("Value error, ")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts) or "Invalid request"


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api(_: Request, exc: ApiError):
        return _render(exc.status, exc.message, exc.headers, exc.extra)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException):
        return _render(exc.status_code, str(exc.detail), dict(exc.headers or {}))

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        return _render(422, _validation_message(exc))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):  # never leak internals
        import logging
        logging.getLogger("calibrex").exception("unhandled error", extra={"path": request.url.path})
        return _render(500, "Something went wrong on the server.")
