"""
Tenant Guard middleware — validates tenant status, plan limits, feature gates.

UPDATED:
  - Policy documents are UNLIMITED regardless of plan (document_limit
    only applies to communication documents)
  - batch_upload is always available for policy document uploads
    (only gated for communication batch uploads on lower plans)

FIX 4: Reduced cache TTL to 30 seconds so plan changes from the back office
       reflect faster. Added plan feature gating (batch_upload, api_access).

Place AFTER CORS and rate limiting but BEFORE routes:
    from app.middleware.tenant_guard import TenantGuardMiddleware
    app.add_middleware(TenantGuardMiddleware)
"""

import logging
import time
import json
import base64
from datetime import datetime

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from sqlalchemy import select, func

logger = logging.getLogger("api.tenant_guard")

# Plan limits — MUST match superadmin billing.py and tenant_info.py
PLAN_LIMITS = {
    "trial": {"queries": 100, "documents": 20, "staff": 2, "policyholders": 0, "features": ["widget"]},
    "starter": {"queries": 1000, "documents": 100, "staff": 5, "policyholders": 0, "features": ["widget", "batch_upload"]},
    "professional": {"queries": 10000, "documents": 500, "staff": 20, "policyholders": 0, "features": ["widget", "batch_upload", "api_access"]},
    "enterprise": {"queries": 0, "documents": 0, "staff": 0, "policyholders": 0, "features": ["widget", "batch_upload", "api_access", "custom_model"]},
}

# Feature → path mapping for gating
# NOTE: batch_upload for policies is always allowed — only communication batch is gated
FEATURE_GATES = {
    "batch_upload": ["/api/v1/communications/upload-batch"],
}

GUARDED_PATHS = ["/api/v1/policies/", "/api/v1/communications/", "/api/v1/history/"]
QUERY_PATHS = ["/api/v1/policies/", "/api/v1/communications/query"]
# Only communication uploads count against document limit
COMM_UPLOAD_PATHS = ["/api/v1/communications/upload"]
SKIP_PATHS = ["/health", "/docs", "/redoc", "/openapi.json", "/api/v1/auth/", "/api/v1/tenant/", "/widget/"]

# Cache tenant status — reduced to 30 seconds so plan changes reflect faster
_tenant_cache: dict[str, dict] = {}
CACHE_TTL = 30


class TenantGuardMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path

        if any(path.startswith(skip) for skip in SKIP_PATHS):
            return await call_next(request)

        if not any(path.startswith(guarded) for guarded in GUARDED_PATHS):
            return await call_next(request)

        tenant_id = self._extract_tenant_id(request)
        if not tenant_id:
            return await call_next(request)

        tenant_info = await self._get_tenant_info(tenant_id)
        if not tenant_info:
            return await call_next(request)

        # Block suspended tenants
        if tenant_info.get("status") == "suspended":
            logger.warning(f"Blocked request from suspended tenant {tenant_id}")
            return JSONResponse(
                status_code=403,
                content={
                    "error": "Account suspended",
                    "detail": "Your agency account has been suspended. "
                              "Please contact support.",
                },
            )

        plan_key = tenant_info.get("plan", "trial")
        plan = PLAN_LIMITS.get(plan_key, PLAN_LIMITS["trial"])

        # Feature gating — check if the plan allows this endpoint
        # NOTE: Policy uploads and policy batch uploads are always allowed
        is_policy_path = path.startswith("/api/v1/policies/")
        for feature, paths in FEATURE_GATES.items():
            if any(path.startswith(p) for p in paths) and feature not in plan["features"]:
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": "Feature not available",
                        "detail": f"Your current plan ({plan_key}) does not include this feature. "
                                  f"Please contact your administrator to upgrade.",
                        "required_feature": feature,
                        "current_plan": plan_key,
                    },
                )

        # Check query limits
        if any(path.startswith(qp) for qp in QUERY_PATHS) and request.method == "POST" and "query" in path:
            limit_response = await self._check_query_limit(tenant_id, plan)
            if limit_response:
                return limit_response

        # Check document limits — ONLY for communication uploads, NOT policy uploads
        if any(path.startswith(up) for up in COMM_UPLOAD_PATHS) and request.method == "POST":
            limit_response = await self._check_document_limit(tenant_id, plan)
            if limit_response:
                return limit_response

        # Policy uploads are ALWAYS allowed (no document limit check)

        return await call_next(request)

    def _extract_tenant_id(self, request: Request) -> str | None:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        try:
            token = auth.split(" ", 1)[1]
            payload = token.split(".")[1]
            # Add padding
            payload += "=" * (4 - len(payload) % 4)
            decoded = json.loads(base64.urlsafe_b64decode(payload))
            return decoded.get("tenant_id")
        except Exception:
            return None

    async def _get_tenant_info(self, tenant_id: str) -> dict | None:
        cached = _tenant_cache.get(tenant_id)
        if cached and time.time() - cached["_ts"] < CACHE_TTL:
            return cached

        try:
            from app.db.session import async_session
            async with async_session() as db:
                from app.models.database import Tenant
                result = await db.execute(
                    select(Tenant).where(Tenant.id == tenant_id)
                )
                tenant = result.scalar_one_or_none()
                if not tenant:
                    return None

                info = {
                    "status": tenant.status.value if hasattr(tenant.status, "value") else str(tenant.status),
                    "plan": getattr(tenant, "plan", None) or "trial",
                    "_ts": time.time(),
                }
                _tenant_cache[tenant_id] = info
                return info
        except Exception as e:
            logger.error(f"Tenant lookup failed for {tenant_id}: {e}")
            return None

    async def _check_query_limit(self, tenant_id: str, plan: dict) -> JSONResponse | None:
        limit = plan["queries"]
        if limit == 0:  # unlimited
            return None

        try:
            from app.db.session import async_session
            async with async_session() as db:
                from app.models.database import QueryLog
                now = datetime.utcnow()
                start = datetime(now.year, now.month, 1)

                count = (await db.execute(
                    select(func.count(QueryLog.id)).where(
                        QueryLog.tenant_id == tenant_id,
                        QueryLog.queried_at >= start,
                    )
                )).scalar() or 0

                if count >= limit:
                    return JSONResponse(
                        status_code=429,
                        content={
                            "error": "Query limit reached",
                            "detail": f"Monthly query limit ({limit}) reached. "
                                      "Please contact your administrator to upgrade your plan.",
                            "queries_used": count,
                            "queries_limit": limit,
                        },
                    )
        except Exception as e:
            logger.error(f"Query limit check failed: {e}")

        return None

    async def _check_document_limit(self, tenant_id: str, plan: dict) -> JSONResponse | None:
        """Check communication document limit only (policy docs are unlimited)."""
        limit = plan["documents"]
        if limit == 0:  # unlimited
            return None

        try:
            from app.db.session import async_session
            async with async_session() as db:
                from app.models.database import Document, DocumentType

                # Only count communication documents against the limit
                count = (await db.execute(
                    select(func.count(Document.id)).where(
                        Document.tenant_id == tenant_id,
                        Document.document_type == DocumentType.COMMUNICATION,
                    )
                )).scalar() or 0

                if count >= limit:
                    return JSONResponse(
                        status_code=429,
                        content={
                            "error": "Document limit reached",
                            "detail": f"Communication document limit ({limit}) reached. "
                                      "Policy documents are unlimited. "
                                      "Please contact your administrator to upgrade.",
                            "documents_used": count,
                            "documents_limit": limit,
                        },
                    )
        except Exception as e:
            logger.error(f"Document limit check failed: {e}")

        return None