"""
Shared API dependencies.
"""
import logging
from typing import Annotated, Optional

from fastapi import Depends, HTTPException, status, Cookie
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, ExpiredSignatureError
from sqlmodel import Session

from app.core.database import get_session
from app.core.security import verify_token
from app.middleware.request_logging import request_id_ctx
from app.models.user import User
from app.services.user_service import UserService

logger = logging.getLogger(__name__)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token")


def get_request_id() -> str:
    """
    Dependency to get the current request ID from context.

    This can be used in endpoints to access the request ID for logging or other purposes.

    Usage:
        @router.get("/example")
        async def example(request_id: Annotated[str, Depends(get_request_id)]):
            logger.info(f"Processing request {request_id}")

    Returns:
        The current request ID, or 'unknown' if not in a request context.
    """
    return request_id_ctx.get()


async def get_current_user(
    token: Annotated[Optional[str], Depends(oauth2_scheme)],
    cookie_token: Annotated[Optional[str], Cookie(alias="access_token")] = None,
    session: Annotated[Session, Depends(get_session)] = None
) -> User:
    """
    Dependency to get the current authenticated user from the token.
    Raises HTTPException with status 401 if authentication fails or token is revoked.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    def _unauthorized(detail: str = "Could not validate credentials") -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Use token from Authorization header or cookie (web video streaming)
    token_to_use = token or cookie_token
    if token_to_use is None:
        raise credentials_exception

    try:
        # Verify token signature and expiration
        payload = verify_token(token_to_use, "access")
        user_id: str = payload.get("sub")

        # Validate claim types
        if not isinstance(user_id, str) or not user_id:
            raise credentials_exception

    except HTTPException:
        raise
    except ExpiredSignatureError:
        logger.info("Expired token presented", extra={"user_id": locals().get('user_id')})
        raise credentials_exception
    except JWTError as e:
        logger.warning("JWT error during token validation", extra={"error": str(e)})
        raise credentials_exception
    except Exception as e:
        logger.error("Unexpected token validation error", extra={"error": str(e)})
        raise credentials_exception

    # Get user from database
    user = UserService(session).get_user_by_id(user_id)
    if user is None:
        raise credentials_exception

    # Check if user is active
    if not user.is_active:
        logger.info("Inactive user access attempt", extra={"user_id": user_id})
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is inactive"
        )

    return user
