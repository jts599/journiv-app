"""
Security utilities for authentication and password hashing.
"""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import ExpiredSignatureError, JWTError, jwt
from passlib.context import CryptContext

from app.core.config import settings
from app.core.logging_config import log_error

pwd_context = CryptContext(schemes=["argon2"], deprecated="auto")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its hash."""
    if plain_password is None:
        return False
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except (ValueError, TypeError):
        return False


def get_password_hash(password: str) -> str:
    """Hash a password."""
    return pwd_context.hash(password)


def _create_token(data: dict, token_type: str, expires_delta: timedelta) -> str:
    """
    Internal helper to create a JWT.

    Includes a JTI (JWT ID) claim for future compatibility with token revocation.
    Currently, JTI is not validated against a database, but including it now
    allows for future implementation of token blacklisting without breaking
    existing clients.
    """
    if not settings.secret_key:
        raise ValueError("Secret key is required for token creation")

    to_encode = data.copy()
    if "sub" not in to_encode:
        raise ValueError("Token payload must include a 'sub' claim")

    now = datetime.now(timezone.utc)
    expire = now + expires_delta

    to_encode.update({
        "exp": expire,
        "type": token_type,
        "iat": now,
        "jti": str(uuid.uuid4())  # JWT ID for future token revocation support
    })
    encoded_jwt = jwt.encode(to_encode, settings.secret_key, algorithm=settings.algorithm)
    return encoded_jwt


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create JWT access token."""
    delta = expires_delta or timedelta(minutes=settings.access_token_expire_minutes)
    return _create_token(data, "access", delta)


def create_refresh_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Create JWT refresh token."""
    delta = expires_delta or timedelta(days=settings.refresh_token_expire_days)
    return _create_token(data, "refresh", delta)


def verify_token(token: str, token_type: str = "access") -> dict:
    """Verify and decode JWT token."""
    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[settings.algorithm],
            options={"verify_aud": False}
        )

        if payload.get("type") != token_type:
            raise JWTError("Invalid token type")
        # Check for required 'sub' field
        if "sub" not in payload:
            raise JWTError("Token missing required 'sub' field")
        return payload
    except ExpiredSignatureError as exc:
        log_error(exc)
        raise
    except JWTError as e:
        log_error(e)
        raise
