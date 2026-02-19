"""
Global exception handlers for the FastAPI application.
Catches unhandled exceptions and returns consistent JSON error responses.
Never leaks stack traces or internal details to clients.
"""

import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("api.errors")


def register_exception_handlers(app: FastAPI) -> None:
    """Register all exception handlers on the app."""

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        """Handle HTTP exceptions (404, 403, etc.)."""
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.detail or "Request failed"},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        """Handle request validation errors with readable messages."""
        errors = []
        for err in exc.errors():
            loc = " → ".join(str(l) for l in err.get("loc", []))
            msg = err.get("msg", "Invalid value")
            errors.append(f"{loc}: {msg}")

        logger.warning(
            f"Validation error on {request.method} {request.url.path}: {errors}",
            extra={"path": request.url.path, "error_type": "ValidationError"},
        )

        return JSONResponse(
            status_code=422,
            content={
                "error": "Invalid request",
                "details": errors,
            },
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError):
        """Handle ValueError (bad input data)."""
        logger.warning(
            f"ValueError on {request.method} {request.url.path}: {exc}",
            extra={"path": request.url.path, "error_type": "ValueError"},
        )
        return JSONResponse(
            status_code=400,
            content={"error": str(exc)},
        )

    @app.exception_handler(PermissionError)
    async def permission_error_handler(request: Request, exc: PermissionError):
        """Handle PermissionError."""
        return JSONResponse(
            status_code=403,
            content={"error": str(exc) or "Permission denied"},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        """
        Catch-all for unhandled exceptions.
        Logs full details but returns a generic error to the client.
        """
        req_id = getattr(request.state, "request_id", "unknown")

        logger.error(
            f"Unhandled {type(exc).__name__} on {request.method} {request.url.path}: {exc}",
            extra={
                "path": request.url.path,
                "error_type": type(exc).__name__,
            },
            exc_info=True,
        )

        return JSONResponse(
            status_code=500,
            content={
                "error": "An internal error occurred. Please try again.",
                "request_id": req_id,
            },
        )
