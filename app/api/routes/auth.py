"""Authentication routes - policyholder verification and token management."""

import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.session import get_db
from app.models.database import Tenant, TenantStatus, Policyholder, StaffUser, UserRole
from app.models.schemas import PolicyholderVerifyRequest, PolicyholderVerifyResponse
from app.services.auth_service import AuthService
from app.core.security import create_staff_token

router = APIRouter()
auth_service = AuthService()
settings = get_settings()


@router.post("/verify-policyholder", response_model=PolicyholderVerifyResponse)
async def verify_policyholder(
    request: PolicyholderVerifyRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Verify a policyholder's identity using Policy ID + Last Name or Company Name.

    No account creation needed. Returns a session token valid for 24 hours.
    The token grants read-only access to the specified policy.
    """
    result = await auth_service.verify_policyholder(
        db=db,
        tenant_id=request.tenant_id,
        policy_number=request.policy_number,
        last_name=request.last_name,
        company_name=request.company_name,
    )

    return PolicyholderVerifyResponse(
        verified=result["verified"],
        token=result["token"],
        policy_number=result["policy_number"],
        expires_at=datetime.utcnow() + timedelta(hours=24),
        message="Verification successful. You can now query your policy.",
    )


@router.post("/test-setup")
async def test_setup(db: AsyncSession = Depends(get_db)):
    """
    DEV ONLY: Create a test tenant, staff user, and sample policyholders.
    This endpoint should be disabled in production.
    """
    if not settings.debug:
        raise HTTPException(status_code=403, detail="Only available in development mode")

    # Check if test tenant already exists
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

    # Create staff user if not exists
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

    # Create sample policyholders
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

    # Generate a staff token
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
        "message": "Test data created. Use the staff_token for authenticated requests.",
        "sample_policyholders": [
            {"policy": "POL-2024-HO-001", "verify_with": "last_name: Smith"},
            {"policy": "POL-2024-AU-002", "verify_with": "last_name: Rodriguez"},
            {"policy": "POL-2024-CGL-003", "verify_with": "company_name: Springfield Hardware & Supply"},
        ],
    }
