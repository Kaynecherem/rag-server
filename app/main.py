"""
Insurance RAG API - Main application entry point.

UPDATED with hardening:
- Structured JSON logging
- Request ID tracking
- Request logging with timing
- Rate limiting (Redis-backed)
- Global exception handlers
- Deep health check (DB + Redis + Pinecone)
"""

import time
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.api.routes import policies, communications, query, auth, widget, batch_upload, history
from app.db.session import engine, Base
from app.api.routes.tenant_info import router as tenant_info_router
from app.api.routes.admin_management import router as admin_mgmt_router


# ── Hardening imports ────────────────────────────────────────────────
from app.utils.logging import setup_logging
from app.middleware.request_id import RequestIDMiddleware
from app.middleware.logging_mw import RequestLoggingMiddleware
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.tenant_guard import TenantGuardMiddleware
from app.middleware.error_handler import register_exception_handlers

settings = get_settings()

# Initialize structured logging BEFORE anything else
setup_logging(debug=settings.debug)
logger = logging.getLogger("api.main")


# ── Lifespan ─────────────────────────────────────────────────────────

async def init_db():
    """Create tables if they don't exist."""
    from sqlalchemy.ext.asyncio import AsyncEngine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables initialized")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    logger.info("Starting Insurance RAG API", extra={
        "debug": settings.debug,
        "llm_model": getattr(settings, "llm_model", "unknown"),
    })
    await init_db()
    yield
    logger.info("Shutting down Insurance RAG API")


# ── App ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="Insurance RAG API",
    version="1.1.0",
    lifespan=lifespan,
    # Don't show docs in production
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
)

# ── Middleware (order matters: first added = outermost) ──────────────

# 1. Request ID - outermost, assigns ID before anything else
app.add_middleware(RequestIDMiddleware)

# 2. Request logging - logs every request with timing
app.add_middleware(RequestLoggingMiddleware)

# Tenant guard (checks suspension + usage limits)
app.add_middleware(TenantGuardMiddleware)

# 3. Rate limiting BEFORE CORS (so CORS wraps it)
app.add_middleware(RateLimitMiddleware)

# 4. CORS - LAST added = outermost = runs first
#    This ensures ALL responses (including 429s) get CORS headers
origins = settings.cors_origin_list
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials="*" not in origins,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "X-RateLimit-Limit", "X-RateLimit-Remaining"],
)

# Rate limiting (uses in-memory fallback if Redis unavailable)
app.add_middleware(RateLimitMiddleware)

# ── Exception handlers ───────────────────────────────────────────────
register_exception_handlers(app)

# ── Routes ───────────────────────────────────────────────────────────

app.include_router(auth.router, prefix="/api/v1/auth", tags=["auth"])
app.include_router(policies.router, prefix="/api/v1/policies", tags=["policies"])
app.include_router(communications.router, prefix="/api/v1/communications", tags=["communications"])
app.include_router(query.router, prefix="/api/v1", tags=["query"])
app.include_router(widget.router, prefix="/widget", tags=["widget"])
app.include_router(batch_upload.router, prefix="/api/v1", tags=["batch-upload"])
app.include_router(history.router, prefix="/api/v1/history", tags=["history"])
app.include_router(tenant_info_router, prefix="/api/v1/tenant", tags=["tenant-info"])
app.include_router(admin_mgmt_router, prefix="/api/v1/admin", tags=["admin-management"])
# ── Health Check ─────────────────────────────────────────────────────

@app.get("/health")
async def health_check():
    """Basic health check - fast, for load balancers."""
    return {"status": "healthy", "service": "insurance-rag"}


@app.get("/health/deep")
async def deep_health_check():
    """
    Deep health check - verifies all dependencies.
    Use for monitoring, not for load balancer probes.
    """
    checks = {}
    overall = "healthy"

    # Database
    try:
        from sqlalchemy import text
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = {"status": "healthy"}
    except Exception as e:
        checks["database"] = {"status": "unhealthy", "error": str(e)}
        overall = "degraded"

    # Redis
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.redis_url)
        await r.ping()
        await r.close()
        checks["redis"] = {"status": "healthy"}
    except Exception as e:
        checks["redis"] = {"status": "unhealthy", "error": str(e)}
        overall = "degraded"

    # Pinecone
    try:
        from pinecone import Pinecone
        pc = Pinecone(api_key=settings.pinecone_api_key)
        index = pc.Index(settings.pinecone_index_name)
        stats = index.describe_index_stats()
        checks["pinecone"] = {
            "status": "healthy",
            "total_vectors": stats.get("total_vector_count", 0),
        }
    except Exception as e:
        checks["pinecone"] = {"status": "unhealthy", "error": str(e)}
        overall = "degraded"

    # External APIs (just verify keys are set, don't burn credits)
    checks["openai"] = {"status": "configured" if settings.openai_api_key else "missing"}
    checks["anthropic"] = {"status": "configured" if settings.anthropic_api_key else "missing"}

    status_code = 200 if overall == "healthy" else 503
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=status_code,
        content={
            "status": overall,
            "service": "insurance-rag",
            "checks": checks,
        },
    )
