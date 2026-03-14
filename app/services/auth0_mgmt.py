"""
Auth0 Management API client — creates/manages Auth0 users automatically
when staff are created via the superadmin or tenant admin dashboards.

Setup:
  1. In Auth0 Dashboard → Applications → Create Application → Machine to Machine
  2. Name it "Insurance RAG Backend"
  3. Authorize it for the "Auth0 Management API" with these scopes:
     - create:users
     - read:users
     - update:users
     - delete:users
     - create:user_tickets
  4. Copy the Client ID and Client Secret
  5. Set these env vars:
     AUTH0_MGMT_CLIENT_ID=<client_id>
     AUTH0_MGMT_CLIENT_SECRET=<client_secret>

Usage:
    from app.services.auth0_mgmt import Auth0ManagementService

    auth0 = Auth0ManagementService()
    result = await auth0.create_user(email, name, password=None)
    # result = {"auth0_user_id": "auth0|abc123", "password_reset_url": "https://..."}
"""

import logging
import httpx
import time
from typing import Optional

from app.config import get_settings

logger = logging.getLogger("api.auth0_mgmt")
settings = get_settings()

# Cache the management token (valid for 24h typically)
_token_cache = {"token": None, "expires_at": 0}


class Auth0ManagementService:
    def __init__(self):
        self.domain = settings.auth0_domain
        self.client_id = getattr(settings, "auth0_mgmt_client_id", None)
        self.client_secret = getattr(settings, "auth0_mgmt_client_secret", None)
        self.base_url = f"https://{self.domain}"
        self.enabled = bool(self.client_id and self.client_secret)

        if not self.enabled:
            logger.warning(
                "Auth0 Management API not configured — staff users will be created "
                "with pending| auth0_user_id and must be created manually in Auth0."
            )

    async def _get_mgmt_token(self) -> Optional[str]:
        """Get a Management API access token, using cache if valid."""
        if not self.enabled:
            return None

        now = time.time()
        if _token_cache["token"] and _token_cache["expires_at"] > now + 60:
            return _token_cache["token"]

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/oauth/token",
                json={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "audience": f"{self.base_url}/api/v2/",
                    "grant_type": "client_credentials",
                },
                timeout=10,
            )

            if resp.status_code != 200:
                logger.error(f"Failed to get Auth0 mgmt token: {resp.status_code} {resp.text}")
                return None

            data = resp.json()
            _token_cache["token"] = data["access_token"]
            _token_cache["expires_at"] = now + data.get("expires_in", 86400)
            return _token_cache["token"]

    async def create_user(
        self,
        email: str,
        name: str,
        password: Optional[str] = None,
    ) -> dict:
        """
        Create a user in Auth0 and return their auth0_user_id.

        If Auth0 Management is not configured, returns a pending placeholder.

        Args:
            email: User's email address
            name: User's display name
            password: Optional password. If not provided, a password reset
                      email will be triggered so the user can set their own.

        Returns:
            {
                "auth0_user_id": "auth0|abc123" or "pending|...",
                "password_reset_url": "https://..." or None,
                "auto_created": True/False
            }
        """
        if not self.enabled:
            import uuid
            return {
                "auth0_user_id": f"pending|{uuid.uuid4().hex[:24]}",
                "password_reset_url": None,
                "auto_created": False,
            }

        token = await self._get_mgmt_token()
        if not token:
            import uuid
            logger.warning(f"Auth0 mgmt token unavailable, using pending ID for {email}")
            return {
                "auth0_user_id": f"pending|{uuid.uuid4().hex[:24]}",
                "password_reset_url": None,
                "auto_created": False,
            }

        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient() as client:
            # Check if user already exists in Auth0
            search_resp = await client.get(
                f"{self.base_url}/api/v2/users-by-email",
                params={"email": email.lower().strip()},
                headers=headers,
                timeout=10,
            )

            if search_resp.status_code == 200:
                existing_users = search_resp.json()
                if existing_users:
                    # User already exists in Auth0, just return their ID
                    auth0_id = existing_users[0]["user_id"]
                    logger.info(f"Auth0 user already exists for {email}: {auth0_id}")
                    return {
                        "auth0_user_id": auth0_id,
                        "password_reset_url": None,
                        "auto_created": False,
                    }

            # Create the user in Auth0
            import secrets
            temp_password = password or secrets.token_urlsafe(24) + "!A1"

            create_resp = await client.post(
                f"{self.base_url}/api/v2/users",
                headers=headers,
                json={
                    "connection": "Username-Password-Authentication",
                    "email": email.lower().strip(),
                    "name": name.strip(),
                    "password": temp_password,
                    "email_verified": False,
                },
                timeout=10,
            )

            if create_resp.status_code == 201:
                user_data = create_resp.json()
                auth0_user_id = user_data["user_id"]
                logger.info(f"Auth0 user created: {email} → {auth0_user_id}")

                # Send password reset email so user can set their own password
                password_reset_url = None
                if not password:
                    # 1. Mark email as verified first (required for some Auth0 configs)
                    await client.patch(
                        f"{self.base_url}/api/v2/users/{auth0_user_id}",
                        headers=headers,
                        json={"email_verified": True},
                        timeout=10,
                    )

                    # 2. Trigger Auth0's built-in password reset email
                    reset_resp = await client.post(
                        f"{self.base_url}/dbconnections/change_password",
                        json={
                            "client_id": self.client_id,
                            "email": email.lower().strip(),
                            "connection": "Username-Password-Authentication",
                        },
                        timeout=10,
                    )
                    if reset_resp.status_code == 200:
                        logger.info(f"Password reset email sent to {email}")
                    else:
                        logger.warning(f"Failed to send password reset email: {reset_resp.text}")

                    # 3. Also generate a ticket URL as backup (can be shared manually)
                    ticket_resp = await client.post(
                        f"{self.base_url}/api/v2/tickets/password-change",
                        headers=headers,
                        json={
                            "user_id": auth0_user_id,
                            "result_url": "https://agencylensai.com/auth",
                            "mark_email_as_verified": True,
                        },
                        timeout=10,
                    )
                    if ticket_resp.status_code == 201:
                        password_reset_url = ticket_resp.json().get("ticket")
                        logger.info(f"Password reset ticket also created for {email}")

                return {
                    "auth0_user_id": auth0_user_id,
                    "password_reset_url": password_reset_url,
                    "auto_created": True,
                }

            elif create_resp.status_code == 409:
                # User exists (race condition) — search again
                logger.info(f"Auth0 user already exists (409) for {email}, fetching ID")
                search_resp2 = await client.get(
                    f"{self.base_url}/api/v2/users-by-email",
                    params={"email": email.lower().strip()},
                    headers=headers,
                    timeout=10,
                )
                if search_resp2.status_code == 200 and search_resp2.json():
                    return {
                        "auth0_user_id": search_resp2.json()[0]["user_id"],
                        "password_reset_url": None,
                        "auto_created": False,
                    }

            # Fallback
            logger.error(f"Auth0 user creation failed for {email}: {create_resp.status_code} {create_resp.text}")
            import uuid
            return {
                "auth0_user_id": f"pending|{uuid.uuid4().hex[:24]}",
                "password_reset_url": None,
                "auto_created": False,
            }

    async def delete_user(self, auth0_user_id: str) -> bool:
        """Delete a user from Auth0. Returns True if successful."""
        if not self.enabled or auth0_user_id.startswith("pending|"):
            return False

        token = await self._get_mgmt_token()
        if not token:
            return False

        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                f"{self.base_url}/api/v2/users/{auth0_user_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            if resp.status_code == 204:
                logger.info(f"Auth0 user deleted: {auth0_user_id}")
                return True
            else:
                logger.error(f"Failed to delete Auth0 user {auth0_user_id}: {resp.status_code}")
                return False