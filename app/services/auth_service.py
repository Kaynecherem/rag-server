"""Auth Service - Policyholder verification and staff authentication."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from app.models.database import Policyholder, StaffUser, Tenant
from app.core.security import create_policyholder_token, create_staff_token
from app.core.exceptions import PolicyholderVerificationError, TenantNotFoundError

logger = structlog.get_logger()


class AuthService:
    """Handles authentication for both policyholders and staff."""

    async def verify_policyholder(
        self,
        db: AsyncSession,
        tenant_id: str,
        policy_number: str,
        last_name: str | None = None,
        company_name: str | None = None,
    ) -> dict:
        """
        Verify a policyholder by Policy ID + Last Name or Company Name.
        Returns a session token if verified.
        """
        # Verify tenant exists
        tenant = await db.execute(
            select(Tenant).where(Tenant.id == tenant_id)
        )
        tenant = tenant.scalar_one_or_none()
        if not tenant:
            raise TenantNotFoundError(tenant_id)

        # Build query
        query = select(Policyholder).where(
            Policyholder.tenant_id == tenant_id,
            Policyholder.policy_number == policy_number,
            Policyholder.is_active == True,
        )

        if last_name:
            query = query.where(
                Policyholder.last_name.ilike(last_name.strip())
            )
        elif company_name:
            query = query.where(
                Policyholder.company_name.ilike(f"%{company_name.strip()}%")
            )
        else:
            raise PolicyholderVerificationError()

        result = await db.execute(query)
        holder = result.scalar_one_or_none()

        if not holder:
            logger.warning(
                "Policyholder verification failed",
                tenant_id=tenant_id,
                policy_number=policy_number,
            )
            raise PolicyholderVerificationError()

        # Create session token
        token = create_policyholder_token(
            tenant_id=str(tenant_id),
            policy_number=policy_number,
        )

        logger.info(
            "Policyholder verified",
            tenant_id=tenant_id,
            policy_number=policy_number,
        )

        return {
            "verified": True,
            "token": token,
            "policy_number": policy_number,
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
