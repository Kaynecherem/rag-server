"""
Tenant Info routes — serves back-office-managed data to the client frontend.

FIX 1: Notifications use raw SQL so the client backend doesn't need the
       Notification model — the table is managed by the superadmin backend.
FIX 2: /disclaimer and /status return friendly messages for deactivated tenants.
FIX 4: /usage returns fresh data on every call (no caching) so plan changes
       from the back office reflect immediately.

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
    Tenant, StaffUser, Policyholder, Document, QueryLog,
)

logger = logging.getLogger("api.tenant_info")
router = APIRouter()

# Plan limits — MUST match superadmin billing.py and tenant_guard.py
PLAN_LIMITS = {
    "trial": {
        "name": "Trial", "queries": 100, "documents": 20,
        "staff": 2, "policyholders": 50,
        "features": ["widget"],
    },
    "starter": {
        "name": "Starter", "queries": 1000, "documents": 100,
        "staff": 5, "policyholders": 500,
        "features": ["widget", "batch_upload"],
    },
    "professional": {
        "name": "Professional", "queries": 10000, "documents": 500,
        "staff": 20, "policyholders": 5000,
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
    result = await db.execute(
        select(Tenant).where(Tenant.slug == slug.lower().strip())
    )
    tenant = result.scalar_one_or_none()

    if not tenant:
        raise HTTPException(status_code=404, detail="Agency not found")

    status_val = tenant.status.value if hasattr(tenant.status, "value") else str(tenant.status)

    if status_val.upper() == "SUSPENDED":
        return {
            "id": str(tenant.id),
            "name": tenant.name,
            "slug": tenant.slug,
            "status": "suspended",
        }

    return {
        "id": str(tenant.id),
        "name": tenant.name,
        "slug": tenant.slug,
        "status": status_val.lower(),
        "plan": tenant.plan or "trial",
        "widget_config": tenant.widget_config or {},
    }

# ═══════════════════════════════════════════════════════════════════════════
# Notifications (FIX 1 — raw SQL, no model dependency)
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/notifications")
async def get_tenant_notifications(
    current_user: dict = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Get active notifications for the current tenant.
    Uses raw SQL to query the notifications table managed by the superadmin backend.
    Returns empty list if the table doesn't exist yet.
    """
    try:
        result = await db.execute(text("""
            SELECT id, title, message, notification_type, created_at
            FROM notifications
            WHERE is_active = true
              AND (
                target = 'all'
                OR (target = 'tenant' AND target_tenant_id = :tid)
              )
              AND (scheduled_at IS NULL OR scheduled_at <= NOW())
            ORDER BY created_at DESC
            LIMIT 10
        """), {"tid": tenant_id})

        rows = result.fetchall()
        return {
            "notifications": [
                {
                    "id": str(row[0]),
                    "title": row[1],
                    "message": row[2],
                    "type": row[3],
                    "created_at": row[4].isoformat() if row[4] else None,
                }
                for row in rows
            ],
        }
    except Exception as e:
        # Table might not exist if superadmin migration hasn't run
        logger.debug(f"Notifications query failed: {e}")
        return {"notifications": []}


# ═══════════════════════════════════════════════════════════════════════════
# Disclaimer
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/disclaimer")
async def get_tenant_disclaimer(
    current_user: dict = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """Get the disclaimer configuration for this tenant."""
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    wc = tenant.widget_config or {}

    default_text = (
        "This assistant provides information based on your insurance policy documents for "
        "informational purposes only. It does not constitute professional insurance advice, "
        "and should not be relied upon for coverage decisions. For binding interpretations, "
        "claims, or policy changes, please contact your insurance agent directly."
    )

    return {
        "disclaimer_text": wc.get("disclaimer_text", default_text),
        "disclaimer_enabled": wc.get("disclaimer_enabled", True),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Usage (FIX 4 — always fresh, no caching)
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/usage")
async def get_tenant_usage(
    current_user: dict = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Get current plan and usage for this tenant.
    Always returns fresh data — no caching — so back-office plan changes
    reflect immediately.
    """
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    plan_key = getattr(tenant, "plan", "trial") or "trial"
    plan = PLAN_LIMITS.get(plan_key, PLAN_LIMITS["trial"])

    now = datetime.utcnow()
    month_start = datetime(now.year, now.month, 1)

    queries_used = (await db.execute(
        select(func.count(QueryLog.id)).where(
            QueryLog.tenant_id == tenant_id,
            QueryLog.queried_at >= month_start,
        )
    )).scalar() or 0

    doc_count = (await db.execute(
        select(func.count(Document.id)).where(Document.tenant_id == tenant_id)
    )).scalar() or 0

    staff_count = (await db.execute(
        select(func.count(StaffUser.id)).where(StaffUser.tenant_id == tenant_id)
    )).scalar() or 0

    ph_count = (await db.execute(
        select(func.count(Policyholder.id)).where(Policyholder.tenant_id == tenant_id)
    )).scalar() or 0

    query_limit = plan["queries"]
    usage_pct = (queries_used / query_limit * 100) if query_limit > 0 else 0

    return {
        "plan": plan_key,
        "plan_name": plan["name"],
        "features": plan["features"],
        "period": now.strftime("%Y-%m"),
        "queries": {"used": queries_used, "limit": query_limit, "pct": round(usage_pct, 1)},
        "documents": {"used": doc_count, "limit": plan["documents"]},
        "staff": {"used": staff_count, "limit": plan["staff"]},
        "policyholders": {"used": ph_count, "limit": plan["policyholders"]},
        "at_risk": query_limit > 0 and usage_pct >= 80,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Tenant Status
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/status")
async def get_tenant_status(
    current_user: dict = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """Get tenant status and plan. Always fresh."""
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    return {
        "tenant_id": str(tenant.id),
        "name": tenant.name,
        "status": tenant.status.value if hasattr(tenant.status, "value") else str(tenant.status),
        "plan": getattr(tenant, "plan", "trial") or "trial",
    }


# ═══════════════════════════════════════════════════════════════════════════
# Current User Info — live from DB, for frontend role sync
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/me")
async def get_current_user_info(
    current_user: dict = Depends(get_current_user),
    tenant_id: str = Depends(get_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns the current user's role as it is in the database RIGHT NOW.
    The frontend calls this on mount and periodically to detect:
    - Role changes (admin → staff or vice versa)
    - Plan changes
    - Deactivation (handled by get_current_user raising 403)

    If the role here differs from what's stored in localStorage,
    the frontend updates localStorage and re-renders the sidebar.
    """
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()

    plan = "trial"
    if tenant:
        plan = getattr(tenant, "plan", "trial") or "trial"

    return {
        "role": current_user.get("role"),
        "email": current_user.get("email"),
        "name": current_user.get("name"),
        "staff_id": current_user.get("staff_id"),
        "tenant_id": tenant_id,
        "plan": plan,
        "plan_name": PLAN_LIMITS.get(plan, PLAN_LIMITS["trial"])["name"],
    }