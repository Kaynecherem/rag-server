"""Security utilities for JWT tokens and authentication."""

from datetime import datetime, timedelta

from jose import jwt, JWTError

from app.config import get_settings

settings = get_settings()

ALGORITHM = "HS256"


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
    """Create a JWT for authenticated staff."""
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


def verify_token(token: str) -> dict | None:
    """Verify any token (staff or policyholder). Returns claims or None."""
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
        return payload
    except JWTError:
        return None
