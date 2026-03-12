"""Auth Service - Policyholder verification and staff authentication."""

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from app.models.database import Policyholder, StaffUser, Tenant
from app.core.security import create_policyholder_token, create_staff_token
from app.core.exceptions import PolicyholderVerificationError, TenantNotFoundError

logger = structlog.get_logger()


class AuthService:
    """Handles authentication for both policyholders and staff."""

    async def verify_policyholder(
            self, db, tenant_id: str, policy_number: str,
            last_name: str = None, company_name: str = None,
    ):
        """
        Verify a policyholder by Policy ID + Last Name or Company Name.
        All matching is case-insensitive.

        Returns a dict with token on success.
        Raises PolicyholderVerificationError on not found.
        Returns a dict with error_code="inactive" if the policyholder is deactivated.
        """
        # Verify tenant exists
        tenant = await db.execute(
            select(Tenant).where(Tenant.id == tenant_id)
        )
        tenant = tenant.scalar_one_or_none()
        if not tenant:
            raise TenantNotFoundError(tenant_id)

        if not last_name and not company_name:
            raise PolicyholderVerificationError()

        # Step 1: Find the policyholder WITHOUT filtering by is_active
        # so we can distinguish "doesn't exist" from "deactivated"
        filters = [
            Policyholder.tenant_id == tenant_id,
            func.lower(Policyholder.policy_number) == policy_number.strip().lower(),
        ]

        if last_name:
            filters.append(Policyholder.last_name.ilike(last_name.strip()))
        elif company_name:
            filters.append(Policyholder.company_name.ilike(f"%{company_name.strip()}%"))

        result = await db.execute(select(Policyholder).where(*filters))
        holder = result.scalar_one_or_none()

        # Step 2: Not found at all
        if not holder:
            logger.warning(
                "Policyholder verification failed",
                tenant_id=tenant_id,
                policy_number=policy_number,
            )
            raise PolicyholderVerificationError()

        # Step 3: Found but deactivated — return a distinct response
        # instead of raising, so the route can return a friendly message
        if not holder.is_active:
            logger.warning(
                "Deactivated policyholder attempted login",
                tenant_id=tenant_id,
                policy_number=policy_number,
            )
            return {
                "verified": False,
                "error_code": "inactive",
            }

        # Step 4: Active — issue token
        canonical_policy_number = holder.policy_number

        token = create_policyholder_token(
            tenant_id=str(tenant_id),
            policy_number=canonical_policy_number,
        )

        logger.info(
            "Policyholder verified",
            tenant_id=tenant_id,
            policy_number=canonical_policy_number,
        )

        return {
            "verified": True,
            "token": token,
            "policy_number": canonical_policy_number,
        }

    async def get_staff_user(self, db: AsyncSession, auth0_user_id: str) -> StaffUser | None:
        """Look up a staff user by Auth0 user ID."""
        result = await db.execute(
            select(StaffUser).where(
                StaffUser.auth0_user_id == auth0_user_id,
                StaffUser.is_active == True,
            )
        )
        return result.scalar_one_or_none()