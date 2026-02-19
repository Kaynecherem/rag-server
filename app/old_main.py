"""Insurance RAG System - FastAPI Application."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import structlog

from app.config import get_settings
from app.api.routes import policies, communications, query, auth, widget
from app.db.session import init_db
from app.core.exceptions import register_exception_handlers

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    settings = get_settings()
    logger.info("Starting Insurance RAG System", env=settings.app_env)

    # Initialize database tables
    await init_db()
    logger.info("Database initialized")

    yield

    logger.info("Shutting down Insurance RAG System")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    settings = get_settings()

    app = FastAPI(
        title="Insurance Policy RAG API",
        description="AI-powered insurance policy question-answering system with citation support",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.debug else None,
        redoc_url="/redoc" if settings.debug else None,
    )

    # CORS
    origins = settings.cors_origin_list
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials="*" not in origins,  # Can't use credentials with wildcard
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Exception handlers
    register_exception_handlers(app)

    # Routes
    app.include_router(auth.router, prefix="/api/v1/auth", tags=["Authentication"])
    app.include_router(policies.router, prefix="/api/v1/policies", tags=["Policies"])
    app.include_router(communications.router, prefix="/api/v1/communications", tags=["Communications"])
    app.include_router(query.router, prefix="/api/v1", tags=["Query"])
    app.include_router(widget.router, prefix="/api/v1/widget", tags=["Widget"])

    @app.get("/health")
    async def health_check():
        return {"status": "healthy", "service": "insurance-rag"}

    return app


app = create_app()
