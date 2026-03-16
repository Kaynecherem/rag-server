"""
Authentication routes — Auth0 staff login + policyholder verification.

CHANGES (multi-tenant fix):
  - staff-login now accepts optional tenant_id/slug for scoping
  - If same email in multiple tenants and no scope → 409 with tenant list
  - Login query excludes soft-deleted staff (deleted_at IS NOT NULL)
  - Auth0 user linking is per-tenant (no global unique)

Auth0 flow:
1. Frontend opens Auth0 login popup via @auth0/auth0-react SDK
2. User authenticates with Auth0 (email/password, Google SSO, etc.)
3. Frontend receives Auth0 access token
4. Frontend calls POST /api/v1/auth/staff-login with the token
5. Backend verifies token, looks up staff user, returns our own JWT

Policyholder flow (unchanged):
1. Frontend calls POST /api/v1/auth/verify-policyholder
2. Backend verifies policy_number + last_name/company_name
3. Returns a session token

REPLACE: app/api/routes/auth.py
"""

import uuid
import logging
from datetime import datetime, timedelta
from typing import Optional, List

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.db.session import get_db
from app.models.database import Tenant, TenantStatus, Policyholder, StaffUser, UserRole
from app.models.schemas import PolicyholderVerifyRequest, PolicyholderVerifyResponse
from app.services.auth_service import AuthService
from app.core.security import create_staff_token, verify_auth0_token

router = APIRouter()
auth_service = AuthService()
settings = get_settings()
logger = logging.getLogger("api.auth")


# ═══════════════════════════════════════════════════════════════════════════
# Auth0 Staff Login
# ═══════════════════════════════════════════════════════════════════════════

class Auth0StaffLoginRequest(BaseModel):
    access_token: str
    email: Optional[str] = None
    tenant_id: Optional[str] = None
    slug: Optional[str] = None


@router.post("/staff-login")
async def staff_auth0_login(
    body: Auth0StaffLoginRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Exchange an Auth0 access token for our own staff JWT.

    Multi-tenant handling:
    - Single tenant match → login succeeds immediately.
    - Multiple tenants + tenant_id/slug provided → scoped lookup.
    - Multiple tenants + no scope → 409 with tenant list for frontend picker.
    """
    # Step 1: Verify the Auth0 token
    auth0_claims = verify_auth0_token(body.access_token)

    email = None
    auth0_sub = None

    if auth0_claims:
        email = auth0_claims.get("email")
        auth0_sub = auth0_claims.get("sub")
    else:
        # Token might be opaque — fetch userinfo
        try:
            resp = httpx.get(
                f"https://{settings.auth0_domain}/userinfo",
                headers={"Authorization": f"Bearer {body.access_token}"},
                timeout=10,
            )
            if resp.status_code == 200:
                userinfo = resp.json()
                email = userinfo.get("email")
                auth0_sub = userinfo.get("sub")
            else:
                logger.warning(f"Auth0 userinfo failed: {resp.status_code}")
        except Exception as e:
            logger.error(f"Auth0 userinfo request failed: {e}")

    # Use provided email as fallback
    if not email and body.email:
        email = body.email.lower().strip()

    if not email:
        raise HTTPException(
            status_code=401,
            detail="Could not determine your email from Auth0. Please try again.",
        )

    email = email.lower().strip()

    # Step 2: Resolve tenant scope if provided
    scoped_tenant_id = None

    if body.tenant_id:
        scoped_tenant_id = body.tenant_id
    elif body.slug:
        tenant_result = await db.execute(
            select(Tenant).where(Tenant.slug == body.slug.lower().strip())
        )
        tenant = tenant_result.scalar_one_or_none()
        if tenant:
            scoped_tenant_id = str(tenant.id)

    # Step 3: Look up staff — EXCLUDE soft-deleted
    filters = [
        StaffUser.email == email,
        StaffUser.deleted_at.is_(None),
    ]

    if scoped_tenant_id:
        filters.append(StaffUser.tenant_id == scoped_tenant_id)

    result = await db.execute(
        select(StaffUser)
        .where(*filters)
        .options(selectinload(StaffUser.tenant))
    )
    staff_matches: List[StaffUser] = list(result.scalars().all())

    # No matches
    if not staff_matches:
        raise HTTPException(
            status_code=403,
            detail=(
                "No staff account found for this email address. "
                "Please contact your agency administrator to set up your account."
            ),
        )

    # Multiple tenants, no scope → ask frontend to pick
    if len(staff_matches) > 1 and not scoped_tenant_id:
        tenant_options = []
        for s in staff_matches:
            if s.is_active and s.tenant:
                tenant_options.append({
                    "tenant_id": str(s.tenant_id),
                    "tenant_name": s.tenant.name,
                    "slug": s.tenant.slug,
                    "role": s.role.value if hasattr(s.role, "value") else str(s.role),
                })

        if not tenant_options:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Your account has been deactivated in all agencies. "
                    "Please contact your agency administrator for assistance."
                ),
            )

        # If only one is active, just use it
        if len(tenant_options) == 1:
            scoped_tenant_id = tenant_options[0]["tenant_id"]
            staff_matches = [
                s for s in staff_matches
                if str(s.tenant_id) == scoped_tenant_id
            ]
        else:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "multiple_tenants",
                    "message": "Your email is associated with multiple agencies. Please select one.",
                    "tenants": tenant_options,
                },
            )

    # Single match (or scoped)
    staff = staff_matches[0]

    if not staff.is_active:
        raise HTTPException(
            status_code=403,
            detail=(
                "Your account has been deactivated. "
                "Please contact your agency administrator for assistance."
            ),
        )

    # Step 4: Link Auth0 user ID on first login (per-tenant)
    if auth0_sub and (
        not staff.auth0_user_id
        or staff.auth0_user_id.startswith("pending|")
        or staff.auth0_user_id.startswith("test|")
    ):
        staff.auth0_user_id = auth0_sub
        logger.info(f"Linked Auth0 user {auth0_sub} to staff {staff.email} in tenant {staff.tenant_id}")

    staff.last_login_at = datetime.utcnow()
    await db.commit()

    # Step 5: Create our own JWT
    token = create_staff_token(
        tenant_id=str(staff.tenant_id),
        user_id=str(staff.id),
        email=staff.email,
        role=staff.role.value if hasattr(staff.role, "value") else str(staff.role),
    )

    logger.info(f"Staff Auth0 login: {staff.email} ({staff.role}) → tenant {staff.tenant_id}")

    return {
        "token": token,
        "tenant_id": str(staff.tenant_id),
        "email": staff.email,
        "name": staff.name,
        "role": staff.role.value if hasattr(staff.role, "value") else str(staff.role),
        "message": "Login successful.",
    }


# ═══════════════════════════════════════════════════════════════════════════
# Policyholder Verification (unchanged)
# ═══════════════════════════════════════════════════════════════════════════
@router.post("/verify-policyholder", response_model=PolicyholderVerifyResponse)
async def verify_policyholder(
    request: PolicyholderVerifyRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Verify a policyholder's identity using Policy ID + Last Name or Company Name.
    Accepts either tenant_id (UUID) or slug (from subdomain) to identify the tenant.
    """
    # Resolve tenant_id from slug if needed
    tenant_id = request.tenant_id
    if not tenant_id and request.slug:
        result = await db.execute(
            select(Tenant).where(Tenant.slug == request.slug.lower().strip())
        )
        tenant = result.scalar_one_or_none()
        if not tenant:
            raise HTTPException(status_code=404, detail="Agency not found")
        tenant_id = str(tenant.id)

    if not tenant_id:
        raise HTTPException(status_code=400, detail="Either tenant_id or slug is required")

    result = await auth_service.verify_policyholder(
        db=db,
        tenant_id=tenant_id,
        policy_number=request.policy_number,
        last_name=request.last_name,
        company_name=request.company_name,
    )

    if result.get("error_code") == "inactive":
        raise HTTPException(
            status_code=403,
            detail=(
                "We're unable to verify access for this policy at the moment. "
                "Please contact your insurance provider for assistance."
            ),
        )

    if not result.get("verified"):
        raise HTTPException(
            status_code=401,
            detail="Verification failed. Please check your Policy ID and last name or company name.",
        )

    return PolicyholderVerifyResponse(
        verified=True,
        token=result["token"],
        policy_number=result["policy_number"],
    )
