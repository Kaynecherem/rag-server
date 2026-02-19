"""Custom exceptions and FastAPI exception handlers."""

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import structlog

logger = structlog.get_logger()


class RAGException(Exception):
    """Base exception for the RAG system."""
    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class DocumentNotFoundError(RAGException):
    def __init__(self, identifier: str):
        super().__init__(f"Document not found: {identifier}", status_code=404)


class PolicyNotIndexedError(RAGException):
    def __init__(self, policy_number: str):
        super().__init__(f"Policy {policy_number} is not yet indexed", status_code=404)


class TenantNotFoundError(RAGException):
    def __init__(self, tenant_id: str):
        super().__init__(f"Tenant not found: {tenant_id}", status_code=404)


class PolicyholderVerificationError(RAGException):
    def __init__(self):
        super().__init__("Policy verification failed. Check your Policy ID and Last Name or Company Name.", status_code=401)


class AccessDeniedError(RAGException):
    def __init__(self, message: str = "Access denied"):
        super().__init__(message, status_code=403)


class DocumentProcessingError(RAGException):
    def __init__(self, message: str):
        super().__init__(f"Document processing failed: {message}", status_code=500)


class RetrievalError(RAGException):
    def __init__(self, message: str):
        super().__init__(f"Retrieval failed: {message}", status_code=500)


def register_exception_handlers(app: FastAPI):
    """Register custom exception handlers on the FastAPI app."""

    @app.exception_handler(RAGException)
    async def rag_exception_handler(request: Request, exc: RAGException):
        logger.error("RAG error", error=exc.message, status=exc.status_code, path=request.url.path)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.message, "type": type(exc).__name__}
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.detail}
        )

    @app.exception_handler(Exception)
    async def general_exception_handler(request: Request, exc: Exception):
        logger.exception("Unhandled exception", error=str(exc), path=request.url.path)
        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error"}
        )
