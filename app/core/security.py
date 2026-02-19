"""
Security utilities for JWT tokens and authentication.

Supports two auth flows:
1. Auth0 RS256 JWTs for staff (production) — verified against Auth0 JWKS
2. Local HS256 JWTs for policyholders + dev staff tokens — verified with SECRET_KEY
"""

import time
import logging
from datetime import datetime, timedelta

import httpx
from jose import jwt, JWTError

from app.config import get_settings

settings = get_settings()
logger = logging.getLogger("api.security")

ALGORITHM = "HS256"


# ═══════════════════════════════════════════════════════════════════════════
# Auth0 JWKS Cache (for RS256 staff tokens)
# ═══════════════════════════════════════════════════════════════════════════

class JWKSCache:
    """
    Caches Auth0 JWKS (JSON Web Key Set) to avoid fetching on every request.
    Keys refresh every 6 hours or on cache miss.
    """

    def __init__(self):
        self._keys: dict = {}
        self._last_fetched: float = 0
        self._ttl: int = 6 * 60 * 60  # 6 hours

    def _is_expired(self) -> bool:
        return time.time() - self._last_fetched > self._ttl

    def _fetch_keys(self):
        """Synchronously fetch JWKS from Auth0 (called lazily)."""
        jwks_url = settings.auth0_jwks_url
        try:
            resp = httpx.get(jwks_url, timeout=10)
            resp.raise_for_status()
            jwks = resp.json()
            self._keys = {
                key["kid"]: key
                for key in jwks.get("keys", [])
                if key.get("use") == "sig"
            }
            self._last_fetched = time.time()
            logger.info(f"Refreshed JWKS cache: {len(self._keys)} signing keys")
        except httpx.HTTPError as e:
            logger.error(f"Failed to fetch JWKS from Auth0: {e}")
            if not self._keys:
                raise

    def get_signing_key(self, kid: str) -> dict:
        """Get the signing key matching the JWT's key ID."""
        if not self._keys or self._is_expired() or kid not in self._keys:
            self._fetch_keys()
        key = self._keys.get(kid)
        if not key:
            raise ValueError(f"Signing key not found for kid: {kid}")
        return key


_jwks_cache = JWKSCache()


# ═══════════════════════════════════════════════════════════════════════════
# Auth0 Token Verification (RS256 — staff in production)
# ═══════════════════════════════════════════════════════════════════════════

def verify_auth0_token(token: str) -> dict | None:
    """
    Verify an Auth0-issued RS256 JWT and return decoded claims.

    Validates:
    - Signature against Auth0 JWKS
    - Issuer matches Auth0 domain
    - Audience matches our API identifier
    - Token is not expired

    Returns claims dict with custom namespace fields mapped to flat keys:
        - tenant_id (from https://api.insurance-rag.com/tenant_id)
        - roles (from https://api.insurance-rag.com/roles)
        - email (from https://api.insurance-rag.com/email or email)
    """
    try:
        # Decode header to get key ID
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")
        if not kid:
            return None

        # Get signing key from cached JWKS
        signing_key = _jwks_cache.get_signing_key(kid)

        # Verify and decode
        payload = jwt.decode(
            token,
            signing_key,
            algorithms=[settings.auth0_algorithms],
            audience=settings.auth0_audience,
            issuer=settings.auth0_issuer,
        )

        # Extract custom claims (Auth0 namespaced)
        namespace = settings.auth0_audience  # https://api.insurance-rag.com
        tenant_id = payload.get(f"{namespace}/tenant_id")
        roles = payload.get(f"{namespace}/roles", [])
        email = payload.get(f"{namespace}/email", payload.get("email", ""))

        if not tenant_id:
            logger.warning("Auth0 token missing tenant_id claim", extra={
                "sub": payload.get("sub"),
            })
            return None

        # Determine role: first role in list, or "staff" default
        role = "admin" if "admin" in roles else "staff"

        return {
            "sub": payload.get("sub", ""),
            "tenant_id": tenant_id,
            "email": email,
            "role": role,
            "roles": roles,
            "type": "auth0_staff",
            "permissions": payload.get("permissions", []),
        }

    except jwt.ExpiredSignatureError:
        logger.debug("Auth0 token expired")
        return None
    except jwt.JWTClaimsError as e:
        logger.debug(f"Auth0 token claims error: {e}")
        return None
    except JWTError as e:
        logger.debug(f"Auth0 token verification failed: {e}")
        return None
    except ValueError as e:
        logger.debug(f"Auth0 JWKS key error: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════
# Local Token Creation/Verification (HS256 — policyholders + dev)
# ═══════════════════════════════════════════════════════════════════════════

def create_policyholder_token(tenant_id: str, policy_number: str, expires_hours: int = 24) -> str:
    """Create a short-lived JWT for a verified policyholder."""
    payload = {
        "sub": policy_number,
        "tenant_id": tenant_id,
        "role": "policyholder",
        "type": "policyholder_session",
        "exp": datetime.utcnow() + timedelta(hours=expires_hours),
        "iat": datetime.utcnow(),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def verify_policyholder_token(token: str) -> dict | None:
    """Verify and decode a policyholder session token. Returns claims or None."""
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
        if payload.get("type") != "policyholder_session":
            return None
        return payload
    except JWTError:
        return None


def create_staff_token(tenant_id: str, user_id: str, email: str, role: str, expires_hours: int = 8) -> str:
    """Create a JWT for authenticated staff (dev/test mode)."""
    payload = {
        "sub": user_id,
        "tenant_id": tenant_id,
        "email": email,
        "role": role,
        "type": "staff_session",
        "exp": datetime.utcnow() + timedelta(hours=expires_hours),
        "iat": datetime.utcnow(),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def _verify_local_token(token: str) -> dict | None:
    """Verify a locally-issued HS256 token (staff dev + policyholder)."""
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None


# ═══════════════════════════════════════════════════════════════════════════
# Unified Token Verification
# ═══════════════════════════════════════════════════════════════════════════

def verify_token(token: str) -> dict | None:
    """
    Verify any token — tries Auth0 RS256 first, falls back to local HS256.

    This allows both flows to work simultaneously:
    - Production staff: Auth0 RS256 JWTs (with tenant_id in custom claims)
    - Dev staff: Local HS256 tokens (from /auth/test-setup)
    - Policyholders: Local HS256 tokens (from /auth/verify-policyholder)
    """
    # Try Auth0 first (RS256) — only if Auth0 is configured
    if settings.auth0_domain and settings.auth0_audience:
        try:
            # Quick check: Auth0 tokens have a 'kid' in the header
            header = jwt.get_unverified_header(token)
            if header.get("kid"):
                result = verify_auth0_token(token)
                if result:
                    return result
        except JWTError:
            pass

    # Fall back to local token verification (HS256)
    return _verify_local_token(token)