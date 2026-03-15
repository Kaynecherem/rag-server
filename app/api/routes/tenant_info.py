"""
Tenant Info routes — serves back-office-managed data to the client frontend.

UPDATED:
  - Plan limits now distinguish between policy docs (unlimited) and
    communication docs (limited by plan).
  - /usage endpoint returns separate counts for policy vs comm docs.

Register in main.py:
    from app.api.routes.tenant_info import router as tenant_info_router
    app.include_router(tenant_info_router, prefix="/api/v1/tenant", tags=["tenant-info"])
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.api.dependencies import get_current_user, get_tenant_id
from app.models.database import (
    Tenant, StaffUser, Policyholder, Document, DocumentType, QueryLog,
)

logger = logging.getLogger("api.tenant_info")
router = APIRouter()

# Plan limits — MUST match superadmin billing.py and tenant_guard.py
# NOTE: document limits apply ONLY to communication documents.
#       Policy documents and policyholders are ALWAYS unlimited.
PLAN_LIMITS = {
    "trial": {
        "name": "Trial", "queries": 100, "documents": 20,
        "staff": 2, "policyholders": 0,
        "features": ["widget"],
    },
    "starter": {
        "name": "Starter", "queries": 1000, "documents": 100,
        "staff": 5, "policyholders": 0,
        "features": ["widget", "batch_upload"],
    },
    "professional": {
        "name": "Professional", "queries": 10000, "documents": 500,
        "staff": 20, "policyholders": 0,
        "features": ["widget", "batch_upload", "api_access"],
    },
    "enterprise": {
        "name": "Enterprise", "queries": 0, "documents": 0,
        "staff": 0, "policyholders": 0,
        "features": ["widget", "batch_upload", "api_access", "custom_model"],
    },
}

# ═══════════════════════════════════════════════════════════════════════════
# Tenant Lookup by Slug (for subdomain resolution)
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/by-slug/{slug}")
async def get_tenant_by_slug(
    slug: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Public endpoint — returns tenant public info by slug.
    Used by the frontend to resolve a subdomain to a tenant.
    No auth required.
    """
    result = await db.execute(select(Tenant).where(Tenant.slug == slug))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Agency not found")

    status = tenant.status.value if hasattr(tenant.status, "value") else str(tenant.status)

    return {
        "tenant_id": str(tenant.id),
        "name": tenant.name,
        "slug": tenant.slug,
        "status": status,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Tenant Status
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/status")
async def get_tenant_status(
    user: dict = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """Returns the tenant's current status (active, suspended, trial)."""
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    status = tenant.status.value if hasattr(tenant.status, "value") else str(tenant.status)

    return {
        "tenant_id": str(tenant.id),
        "name": tenant.name,
        "status": status,
        "plan": getattr(tenant, "plan", None) or "trial",
    }


# ═══════════════════════════════════════════════════════════════════════════
# Usage & Limits
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/usage")
async def get_usage(
    user: dict = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns current usage vs plan limits.
    Policy documents are unlimited — document limits apply only to communications.
    Always fresh (no caching) so plan changes from the back office reflect immediately.
    """
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    plan_key = getattr(tenant, "plan", None) or "trial"
    plan = PLAN_LIMITS.get(plan_key, PLAN_LIMITS["trial"])

    now = datetime.utcnow()
    start = datetime(now.year, now.month, 1)

    queries_used = (await db.execute(
        select(func.count(QueryLog.id)).where(
            QueryLog.tenant_id == tenant_id,
            QueryLog.queried_at >= start,
        )
    )).scalar() or 0

    # Count policy docs and communication docs separately
    total_doc_count = (await db.execute(
        select(func.count(Document.id)).where(Document.tenant_id == tenant_id)
    )).scalar() or 0

    policy_doc_count = (await db.execute(
        select(func.count(Document.id)).where(
            Document.tenant_id == tenant_id,
            Document.document_type == DocumentType.POLICY,
        )
    )).scalar() or 0

    comm_doc_count = total_doc_count - policy_doc_count

    staff_count = (await db.execute(
        select(func.count(StaffUser.id)).where(StaffUser.tenant_id == tenant_id)
    )).scalar() or 0

    ph_count = (await db.execute(
        select(func.count(Policyholder.id)).where(Policyholder.tenant_id == tenant_id)
    )).scalar() or 0

    query_limit = plan["queries"]
    doc_limit = plan["documents"]
    staff_limit = plan["staff"]
    ph_limit = plan["policyholders"]

    return {
        "plan": plan_key,
        "plan_name": plan["name"],
        "features": plan["features"],
        "queries": {
            "used": queries_used,
            "limit": query_limit,
            "unlimited": query_limit == 0,
        },
        "policy_documents": {
            "count": policy_doc_count,
            "unlimited": True,
        },
        "communication_documents": {
            "count": comm_doc_count,
            "limit": doc_limit,
            "unlimited": doc_limit == 0,
        },
        "staff": {
            "count": staff_count,
            "limit": staff_limit,
            "unlimited": staff_limit == 0,
        },
        "policyholders": {
            "count": ph_count,
            "limit": ph_limit,
            "unlimited": ph_limit == 0,
        },
    }


# ═══════════════════════════════════════════════════════════════════════════
# Notifications (from back office)
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/notifications")
async def get_notifications(
    user: dict = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns active notifications for this tenant.
    Uses raw SQL so the client backend doesn't need the Notification model.
    """
    now = datetime.utcnow().isoformat()
    sql = text("""
        SELECT id, title, message, notification_type, created_at
        FROM notifications
        WHERE is_active = true
          AND (scheduled_at IS NULL OR scheduled_at <= :now)
          AND (target = 'all' OR (target = 'tenant' AND target_tenant_id = :tid))
        ORDER BY created_at DESC
        LIMIT 10
    """)

    try:
        result = await db.execute(sql, {"now": now, "tid": tenant_id})
        rows = result.fetchall()
        return {
            "notifications": [
                {
                    "id": str(r[0]),
                    "title": r[1],
                    "message": r[2],
                    "type": r[3],
                    "created_at": r[4].isoformat() if r[4] else None,
                }
                for r in rows
            ]
        }
    except Exception as e:
        logger.warning(f"Notifications query failed (table may not exist yet): {e}")
        return {"notifications": []}


# ═══════════════════════════════════════════════════════════════════════════
# Disclaimer
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/disclaimer")
async def get_disclaimer(
    tenant_id: str = Depends(get_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """Returns the disclaimer config for this tenant."""
    sql = text("""
        SELECT disclaimer_text, disclaimer_enabled
        FROM tenant_disclaimers
        WHERE tenant_id = :tid
        LIMIT 1
    """)

    try:
        result = await db.execute(sql, {"tid": tenant_id})
        row = result.fetchone()
        if row:
            return {
                "disclaimer_text": row[0],
                "disclaimer_enabled": row[1],
            }
    except Exception as e:
        logger.debug(f"Disclaimer query failed: {e}")

    return {
        "disclaimer_text": "",
        "disclaimer_enabled": False,
    }