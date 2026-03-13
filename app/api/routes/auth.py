"""
Authentication routes — Auth0 staff login + policyholder verification.

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
"""

import uuid
import logging
from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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


@router.post("/staff-login")
async def staff_auth0_login(
    body: Auth0StaffLoginRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Exchange an Auth0 access token for our own staff JWT.

    Verifies the Auth0 token, looks up the staff user by email,
    checks active status and returns a JWT with the current DB role.
    """
    # Step 1: Verify the Auth0 token
    auth0_claims = verify_auth0_token(body.access_token)

    email = None
    auth0_sub = None

    if auth0_claims:
        email = auth0_claims.get("email")
        auth0_sub = auth0_claims.get("sub")
    else:
        # Token might be opaque or missing custom claims — fetch userinfo
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

    # Step 2: Look up staff user by email
    result = await db.execute(
        select(StaffUser).where(StaffUser.email == email)
    )
    staff = result.scalar_one_or_none()

    if not staff:
        raise HTTPException(
            status_code=403,
            detail=(
                "No staff account found for this email address. "
                "Please contact your agency administrator to set up your account."
            ),
        )

    if not staff.is_active:
        raise HTTPException(
            status_code=403,
            detail=(
                "Your account has been deactivated. "
                "Please contact your agency administrator for assistance."
            ),
        )

    # Step 3: Link Auth0 user ID on first login
    if auth0_sub and (
        not staff.auth0_user_id
        or staff.auth0_user_id.startswith("pending|")
        or staff.auth0_user_id.startswith("test|")
    ):
        staff.auth0_user_id = auth0_sub
        logger.info(f"Linked Auth0 user {auth0_sub} to staff {staff.email}")

    staff.last_login_at = datetime.utcnow()
    await db.commit()

    # Step 4: Create our own JWT
    token = create_staff_token(
        tenant_id=str(staff.tenant_id),
        user_id=str(staff.id),
        email=staff.email,
        role=staff.role.value if hasattr(staff.role, "value") else str(staff.role),
    )

    logger.info(f"Staff Auth0 login: {staff.email} ({staff.role})")

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
                "Please contact your insurance provider for assistance — "
                "they'll be happy to help you get connected."
            ),
        )

    return PolicyholderVerifyResponse(
        verified=result["verified"],
        token=result["token"],
        policy_number=result["policy_number"],
        expires_at=datetime.utcnow() + timedelta(hours=24),
        message="Verification successful. You can now query your policy.",
    )
# ═══════════════════════════════════════════════════════════════════════════
# Test Setup (DEV ONLY — kept for seeding data, not used by login page)
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/test-setup")
async def test_setup(db: AsyncSession = Depends(get_db)):
    """
    DEV ONLY: Create a test tenant, staff user, and sample policyholders.
    Disabled in production. No longer used by the login page — Auth0 handles
    staff login now. This is kept for seeding test data via scripts.
    """
    if not settings.debug:
        raise HTTPException(status_code=403, detail="Only available in development mode")

    result = await db.execute(select(Tenant).where(Tenant.slug == "sunshine-insurance"))
    existing = result.scalar_one_or_none()

    if existing:
        tenant = existing
    else:
        tenant = Tenant(
            name="Sunshine Insurance Group",
            slug="sunshine-insurance",
            status=TenantStatus.ACTIVE,
            widget_config={
                "theme": {"primary": "#1B4F72", "accent": "#F39C12"},
                "welcome_message": "Welcome to Sunshine Insurance! Ask anything about your policy.",
            },
        )
        db.add(tenant)
        await db.flush()

    tenant_id = str(tenant.id)

    result = await db.execute(
        select(StaffUser).where(StaffUser.tenant_id == tenant.id, StaffUser.email == "admin@sunshine.test")
    )
    if not result.scalar_one_or_none():
        staff = StaffUser(
            tenant_id=tenant.id,
            auth0_user_id=f"test|{uuid.uuid4().hex[:12]}",
            email="admin@sunshine.test",
            name="Test Admin",
            role=UserRole.ADMIN,
        )
        db.add(staff)

    policyholders_data = [
        {"policy_number": "POL-2024-HO-001", "last_name": "Smith", "company_name": None},
        {"policy_number": "POL-2024-AU-002", "last_name": "Rodriguez", "company_name": None},
        {"policy_number": "POL-2024-CGL-003", "last_name": None, "company_name": "Springfield Hardware & Supply"},
    ]

    for ph_data in policyholders_data:
        result = await db.execute(
            select(Policyholder).where(
                Policyholder.tenant_id == tenant.id,
                Policyholder.policy_number == ph_data["policy_number"],
            )
        )
        if not result.scalar_one_or_none():
            db.add(Policyholder(tenant_id=tenant.id, **ph_data))

    await db.flush()

    staff_token = create_staff_token(
        tenant_id=tenant_id,
        user_id=f"test-admin-{tenant_id[:8]}",
        email="admin@sunshine.test",
        role="admin",
    )

    return {
        "tenant_id": tenant_id,
        "tenant_name": "Sunshine Insurance Group",
        "staff_token": staff_token,
        "message": "Test data seeded. Staff login now uses Auth0.",
        "sample_policyholders": [
            {"policy": "POL-2024-HO-001", "verify_with": "last_name: Smith"},
            {"policy": "POL-2024-AU-002", "verify_with": "last_name: Rodriguez"},
            {"policy": "POL-2024-CGL-003", "verify_with": "company_name: Springfield Hardware & Supply"},
        ],
    }
