"""Shared API dependencies for authentication and authorization."""

from fastapi import Depends, HTTPException, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.core.security import verify_token
from app.models.database import UserRole


async def get_current_user(
    authorization: str = Header(..., description="Bearer token"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """
    Extract and verify the current user from the Authorization header.
    Works for both staff (Auth0 JWT) and policyholders (session token).
    Returns the decoded token claims.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    token = authorization.split(" ", 1)[1]
    claims = verify_token(token)

    if not claims:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    return claims


async def require_staff(current_user: dict = Depends(get_current_user)) -> dict:
    """Require that the current user is a staff member (admin or staff role)."""
    role = current_user.get("role")
    if role not in (UserRole.ADMIN.value, UserRole.STAFF.value, "admin", "staff"):
        raise HTTPException(status_code=403, detail="Staff access required")
    return current_user


async def require_admin(current_user: dict = Depends(get_current_user)) -> dict:
    """Require that the current user is an admin."""
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
