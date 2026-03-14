"""
Tenant Admin Management routes — staff and policyholder CRUD for admin-role users.

FIX 3: Admins cannot update or toggle their own records at all.
       The check uses staff_id from the DB lookup (set by the updated dependencies.py).

REPLACE your existing app/api/routes/admin_management.py with this file.
"""

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.api.dependencies import require_admin, get_tenant_id
from app.models.database import StaffUser, Policyholder, UserRole

from app.services.auth0_mgmt import Auth0ManagementService

logger = logging.getLogger("api.admin_management")
router = APIRouter()


def _is_self(admin: dict, staff_id: str) -> bool:
    """Check if the admin is trying to modify their own record."""
    # staff_id is set by the updated dependencies.py during DB validation
    return admin.get("staff_id") == staff_id


# ═══════════════════════════════════════════════════════════════════════════
# Staff Management (tenant-scoped, admin only)
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/staff")
async def list_staff(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: Optional[str] = None,
    admin: dict = Depends(require_admin),
    tenant_id: str = Depends(get_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """List staff users in the admin's tenant."""
    filters = [StaffUser.tenant_id == tenant_id]
    if search:
        filters.append(
            (StaffUser.email.ilike(f"%{search}%")) | (StaffUser.name.ilike(f"%{search}%"))
        )

    total = (await db.execute(select(func.count(StaffUser.id)).where(*filters))).scalar() or 0

    query = (
        select(StaffUser)
        .where(*filters)
        .order_by(desc(StaffUser.created_at))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await db.execute(query)
    staff_list = result.scalars().all()

    return {
        "staff": [
            {
                "id": str(s.id),
                "email": s.email,
                "name": s.name,
                "role": s.role.value if hasattr(s.role, "value") else str(s.role),
                "is_active": s.is_active,
                "is_self": _is_self(admin, str(s.id)),
                "last_login_at": s.last_login_at.isoformat() if s.last_login_at else None,
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }
            for s in staff_list
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("/staff", status_code=201)
async def create_staff(
    body: dict,
    admin: dict = Depends(require_admin),
    tenant_id: str = Depends(get_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """Create a new staff user in the admin's tenant."""
    email = body.get("email", "").lower().strip()
    name = body.get("name", "").strip()
    role = body.get("role", "staff")

    if not email or not name:
        raise HTTPException(status_code=400, detail="Email and name are required")
    if role not in ("admin", "staff"):
        raise HTTPException(status_code=400, detail="Role must be 'admin' or 'staff'")

    existing = await db.execute(
        select(StaffUser).where(StaffUser.tenant_id == tenant_id, StaffUser.email == email)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Staff with email '{email}' already exists")
    # Auto-create user in Auth0
    auth0_svc = Auth0ManagementService()
    auth0_result = await auth0_svc.create_user(email=email, name=name)

    staff = StaffUser(
        tenant_id=tenant_id,
        email=email,
        name=name,
        role=UserRole(role),
        auth0_user_id=auth0_result["auth0_user_id"],
        is_active=True,
    )
    db.add(staff)
    await db.commit()
    await db.refresh(staff)

    return {
        "id": str(staff.id),
        "email": staff.email,
        "name": staff.name,
        "role": role,
        "is_active": True,
    }


@router.put("/staff/{staff_id}")
async def update_staff(
    staff_id: str,
    body: dict,
    admin: dict = Depends(require_admin),
    tenant_id: str = Depends(get_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """Update a staff user in the admin's tenant."""
    # Block self-edit entirely
    if _is_self(admin, staff_id):
        raise HTTPException(
            status_code=403,
            detail="You cannot modify your own account. Ask another administrator to make changes.",
        )

    result = await db.execute(
        select(StaffUser).where(StaffUser.id == staff_id, StaffUser.tenant_id == tenant_id)
    )
    staff = result.scalar_one_or_none()
    if not staff:
        raise HTTPException(status_code=404, detail="Staff user not found")

    if "name" in body and body["name"] and body["name"].strip():
        staff.name = body["name"].strip()
    if "role" in body and body["role"] in ("admin", "staff"):
        staff.role = UserRole(body["role"])
    if "email" in body and body["email"] and body["email"].strip():
        new_email = body["email"].lower().strip()
        dup = await db.execute(
            select(StaffUser).where(
                StaffUser.tenant_id == tenant_id, StaffUser.email == new_email, StaffUser.id != staff_id
            )
        )
        if dup.scalar_one_or_none():
            raise HTTPException(status_code=409, detail=f"Email '{new_email}' already in use")
        staff.email = new_email

    await db.commit()
    await db.refresh(staff)

    # Sync changes to Auth0
    if not staff.auth0_user_id.startswith("pending|"):
        from app.services.auth0_mgmt import Auth0ManagementService
        auth0_svc = Auth0ManagementService()
        await auth0_svc.update_user(
            auth0_user_id=staff.auth0_user_id,
            name=staff.name,
            email=staff.email,
        )

    return {
        "id": str(staff.id),
        "email": staff.email,
        "name": staff.name,
        "role": staff.role.value if hasattr(staff.role, "value") else str(staff.role),
        "is_active": staff.is_active,
    }


@router.patch("/staff/{staff_id}/status")
async def toggle_staff_status(
    staff_id: str,
    body: dict,
    admin: dict = Depends(require_admin),
    tenant_id: str = Depends(get_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """Activate or deactivate a staff user."""
    # Block self-deactivation
    if _is_self(admin, staff_id):
        raise HTTPException(
            status_code=403,
            detail="You cannot deactivate your own account. Ask another administrator.",
        )

    result = await db.execute(
        select(StaffUser).where(StaffUser.id == staff_id, StaffUser.tenant_id == tenant_id)
    )
    staff = result.scalar_one_or_none()
    if not staff:
        raise HTTPException(status_code=404, detail="Staff user not found")

    staff.is_active = body.get("is_active", not staff.is_active)
    await db.commit()

    return {"id": str(staff.id), "email": staff.email, "is_active": staff.is_active}


# ═══════════════════════════════════════════════════════════════════════════
# Policyholder Management (tenant-scoped, admin only)
# ═══════════════════════════════════════════════════════════════════════════

@router.get("/policyholders")
async def list_policyholders(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: Optional[str] = None,
    admin: dict = Depends(require_admin),
    tenant_id: str = Depends(get_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """List policyholders in the admin's tenant."""
    filters = [Policyholder.tenant_id == tenant_id]
    if search:
        filters.append(
            (Policyholder.policy_number.ilike(f"%{search}%"))
            | (Policyholder.last_name.ilike(f"%{search}%"))
            | (Policyholder.company_name.ilike(f"%{search}%"))
        )

    total = (await db.execute(select(func.count(Policyholder.id)).where(*filters))).scalar() or 0

    query = (
        select(Policyholder)
        .where(*filters)
        .order_by(desc(Policyholder.created_at))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    result = await db.execute(query)
    ph_list = result.scalars().all()

    return {
        "policyholders": [
            {
                "id": str(ph.id),
                "policy_number": ph.policy_number,
                "last_name": ph.last_name,
                "company_name": ph.company_name,
                "is_active": ph.is_active,
                "created_at": ph.created_at.isoformat() if ph.created_at else None,
            }
            for ph in ph_list
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("/policyholders", status_code=201)
async def create_policyholder(
    body: dict,
    admin: dict = Depends(require_admin),
    tenant_id: str = Depends(get_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """Create a new policyholder in the admin's tenant."""
    policy_number = body.get("policy_number", "").strip()
    last_name = body.get("last_name", "").strip() or None
    company_name = body.get("company_name", "").strip() or None

    if not policy_number:
        raise HTTPException(status_code=400, detail="Policy number is required")
    if not last_name and not company_name:
        raise HTTPException(status_code=400, detail="At least one of last name or company name is required")

    existing = await db.execute(
        select(Policyholder).where(
            Policyholder.tenant_id == tenant_id,
            func.lower(Policyholder.policy_number) == policy_number.lower(),
        )
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Policyholder with policy '{policy_number}' already exists")

    ph = Policyholder(
        tenant_id=tenant_id,
        policy_number=policy_number,
        last_name=last_name,
        company_name=company_name,
        is_active=True,
    )
    db.add(ph)
    await db.commit()
    await db.refresh(ph)

    return {
        "id": str(ph.id),
        "policy_number": ph.policy_number,
        "last_name": ph.last_name,
        "company_name": ph.company_name,
        "is_active": True,
    }


@router.put("/policyholders/{ph_id}")
async def update_policyholder(
    ph_id: str,
    body: dict,
    admin: dict = Depends(require_admin),
    tenant_id: str = Depends(get_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """Update a policyholder in the admin's tenant."""
    result = await db.execute(
        select(Policyholder).where(Policyholder.id == ph_id, Policyholder.tenant_id == tenant_id)
    )
    ph = result.scalar_one_or_none()
    if not ph:
        raise HTTPException(status_code=404, detail="Policyholder not found")

    if "policy_number" in body and body["policy_number"]:
        ph.policy_number = body["policy_number"].strip()
    if "last_name" in body:
        ph.last_name = body["last_name"].strip() if body["last_name"] else None
    if "company_name" in body:
        ph.company_name = body["company_name"].strip() if body["company_name"] else None

    await db.commit()
    await db.refresh(ph)

    return {
        "id": str(ph.id),
        "policy_number": ph.policy_number,
        "last_name": ph.last_name,
        "company_name": ph.company_name,
        "is_active": ph.is_active,
    }


@router.patch("/policyholders/{ph_id}/status")
async def toggle_policyholder_status(
    ph_id: str,
    body: dict,
    admin: dict = Depends(require_admin),
    tenant_id: str = Depends(get_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """Activate or deactivate a policyholder."""
    result = await db.execute(
        select(Policyholder).where(Policyholder.id == ph_id, Policyholder.tenant_id == tenant_id)
    )
    ph = result.scalar_one_or_none()
    if not ph:
        raise HTTPException(status_code=404, detail="Policyholder not found")

    ph.is_active = body.get("is_active", not ph.is_active)
    await db.commit()

    return {"id": str(ph.id), "policy_number": ph.policy_number, "is_active": ph.is_active}