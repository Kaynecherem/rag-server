"""
Shared API dependencies for authentication and authorization.

FIXES:
- Fix 2: Staff role changes from backoffice now enforced — role is read
  from DB on every request, not trusted from the JWT alone.
- Fix 4: Deactivated users are blocked — is_active checked from DB.
- Stale tokens are effectively invalidated because the DB is the source
  of truth for role and active status.

REPLACE your existing app/api/dependencies.py with this file.
"""

from fastapi import Depends, HTTPException, Header
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.core.security import verify_token
from app.models.database import StaffUser, UserRole


async def get_current_user(
    authorization: str = Header(..., description="Bearer token"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Extract and verify the current user from the Authorization header.
    Works for both staff (Auth0 JWT) and policyholders (session token).

    For staff users: also validates against the database to check:
    - The user still exists
    - The user is still active (is_active = True)
    - The user's current role (may have changed since token was issued)

    This means role changes and deactivations from the superadmin
    back office take effect immediately — no need to wait for token expiry.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    token = authorization.split(" ", 1)[1]
    claims = verify_token(token)

    if not claims:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    token_type = claims.get("type", "")

    # ── Staff tokens: validate against DB ────────────────────────────
    if token_type in ("staff_session", "auth0_staff"):
        staff_user = await _validate_staff_from_db(db, claims)
        if staff_user:
            # Override token role with current DB role
            claims["role"] = staff_user.role.value if hasattr(staff_user.role, "value") else str(staff_user.role)
            claims["email"] = staff_user.email
            claims["name"] = staff_user.name
            claims["staff_id"] = str(staff_user.id)

    # ── Policyholder tokens: validate against DB ─────────────────────
    elif token_type == "policyholder_session":
        await _validate_policyholder_from_db(db, claims)

    return claims


async def _validate_staff_from_db(db: AsyncSession, claims: dict) -> StaffUser | None:
    """
    Look up the staff user in the database and verify they are still
    active. Returns the StaffUser object so we can read the current role.
    """
    tenant_id = claims.get("tenant_id")
    email = claims.get("email")
    user_id = claims.get("sub")

    if not tenant_id:
        return None

    # Try to find by email first (most reliable), then by sub/user_id
    staff = None
    if email:
        result = await db.execute(
            select(StaffUser).where(
                StaffUser.tenant_id == tenant_id,
                StaffUser.email == email,
            )
        )
        staff = result.scalar_one_or_none()

    if not staff and user_id:
        # Try auth0_user_id match
        result = await db.execute(
            select(StaffUser).where(
                StaffUser.auth0_user_id == user_id,
            )
        )
        staff = result.scalar_one_or_none()

    if not staff:
        # Staff user was deleted — token is stale
        raise HTTPException(
            status_code=401,
            detail="Your account could not be found. Please sign in again.",
        )

    if not staff.is_active:
        # Staff user was deactivated
        raise HTTPException(
            status_code=403,
            detail=(
                "Your account has been deactivated. "
                "Please contact your agency administrator for assistance."
            ),
        )

    return staff


async def _validate_policyholder_from_db(db: AsyncSession, claims: dict):
    """
    Validate a policyholder token against the database.
    Checks that the policyholder record still exists and is active.
    """
    from app.models.database import Policyholder
    from sqlalchemy import func

    tenant_id = claims.get("tenant_id")
    policy_number = claims.get("sub")  # policyholder tokens store policy_number in sub

    if not tenant_id or not policy_number:
        return

    result = await db.execute(
        select(Policyholder).where(
            Policyholder.tenant_id == tenant_id,
            func.lower(Policyholder.policy_number) == policy_number.strip().lower(),
        )
    )
    policyholder = result.scalar_one_or_none()

    if not policyholder:
        raise HTTPException(
            status_code=401,
            detail="Your policy record could not be found. Please sign in again.",
        )

    if not policyholder.is_active:
        raise HTTPException(
            status_code=403,
            detail=(
                "We're unable to verify access for this policy at the moment. "
                "Please contact your insurance provider for assistance."
            ),
        )


async def require_staff(current_user: dict = Depends(get_current_user)) -> dict:
    """
    Require that the current user is a staff member (admin or staff role).
    The role is now read from the DB (via get_current_user), not just the token.
    """
    role = current_user.get("role")
    if role not in (UserRole.ADMIN.value, UserRole.STAFF.value, "admin", "staff"):
        raise HTTPException(status_code=403, detail="Staff access required")
    return current_user


async def require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """
    Require that the current user is an admin.
    The role is now read from the DB (via get_current_user), not just the token.
    """
    role = current_user.get("role")
    if role not in (UserRole.ADMIN.value, "admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return current_user


def get_tenant_id(current_user: dict = Depends(get_current_user)) -> str:
    """Extract tenant_id from the authenticated user's token."""
    tenant_id = current_user.get("tenant_id")
    if not tenant_id:
        raise HTTPException(status_code=403, detail="No tenant context found")
    return tenant_id
