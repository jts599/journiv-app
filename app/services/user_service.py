"""
User service for handling users and user settings.
"""
import time
import uuid
from typing import Optional

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlmodel import Session, select

from app.core.exceptions import (
    UserNotFoundError,
    UserAlreadyExistsError,
    InvalidCredentialsError,
    UnauthorizedError,
    UserSettingsNotFoundError,
)
from app.core.logging_config import log_error, log_warning, log_info
from app.core.security import get_password_hash, verify_password
from app.models.user import User, UserSettings
from app.schemas.user import UserCreate, UserUpdate, UserSettingsCreate, UserSettingsUpdate

# Hash evaluated once to keep timing consistent for missing users
_DUMMY_PASSWORD_HASH = get_password_hash("journiv-dummy-password")


def _schema_dump(schema_obj, *, exclude_unset: bool = False):
    """Support both Pydantic v1 and v2 dump APIs."""
    if hasattr(schema_obj, "model_dump"):
        return schema_obj.model_dump(exclude_unset=exclude_unset)
    return schema_obj.dict(exclude_unset=exclude_unset)


class UserService:
    """User service class."""

    def __init__(self, session: Session):
        self.session = session

    def get_user_by_id(self, user_id: str) -> Optional[User]:
        """Get user by ID."""
        try:
            user_uuid = uuid.UUID(user_id)
            statement = select(User).where(User.id == user_uuid)
            return self.session.exec(statement).first()
        except ValueError:
            return None

    def get_user_by_email(self, email: str) -> Optional[User]:
        """Get user by email."""
        statement = select(User).where(User.email == email)
        return self.session.exec(statement).first()

    def create_user(self, user_data: UserCreate) -> User:
        """Create a new user."""
        # Check if user already exists
        existing_user = self.get_user_by_email(user_data.email)
        if existing_user:
            raise UserAlreadyExistsError("Email already registered")

        # Create user
        hashed_password = get_password_hash(user_data.password)
        user = User(
            email=user_data.email,
            password=hashed_password,
            name=user_data.name
        )

        self.session.add(user)
        try:
            # Flush to assign identifiers and catch integrity issues early
            self.session.flush()
            # Create default user settings without committing
            self.create_user_settings(user.id, UserSettingsCreate(), commit=False)
            self.session.commit()
            self.session.refresh(user)
        except IntegrityError as exc:
            self.session.rollback()
            raise UserAlreadyExistsError("Email already registered") from exc
        except SQLAlchemyError as exc:
            self.session.rollback()
            log_error(exc, user_email=user.email)
            raise

        return user

    def update_user(self, user_id: str, user_data: UserUpdate) -> User:
        """Update user information."""
        user = self.get_user_by_id(user_id)
        if not user:
            raise UserNotFoundError("User not found")

        # Handle password change if provided
        if user_data.current_password is not None and user_data.new_password is not None:
            # Verify current password
            if not verify_password(user_data.current_password, user.password):
                log_warning(
                    f"Password change failed for {user.email}: current password mismatch"
                )
                raise InvalidCredentialsError("Current password is incorrect")

            # Update password
            user.password = get_password_hash(user_data.new_password)

        # Update other fields
        if user_data.name is not None:
            user.name = user_data.name
        if user_data.profile_picture_url is not None:
            user.profile_picture_url = user_data.profile_picture_url

        try:
            self.session.add(user)
            self.session.commit()
            self.session.refresh(user)
        except SQLAlchemyError as exc:
            self.session.rollback()
            log_error(exc, user_email=user.email)
            raise

        return user

    def delete_user(self, user_id: str) -> bool:
        """Permanently delete a user and all related data.

        All related data (journals, entries, media, tags, mood logs, prompts,
        settings, and writing streaks) are automatically deleted via
        database-level CASCADE constraints and ORM relationship cascades.
        """
        user = self.get_user_by_id(user_id)
        if not user:
            raise UserNotFoundError("User not found")

        user_email = user.email

        # Delete the user - cascade deletion handles all related data
        self.session.delete(user)

        try:
            self.session.commit()
        except SQLAlchemyError as exc:
            self.session.rollback()
            log_error(exc, user_email=user_email)
            raise

        log_info(f"User and all related data deleted via cascade: {user_email}")
        return True

    def authenticate_user(self, email: str, password: str) -> User:
        """Authenticate user with email and password."""
        user = self.get_user_by_email(email)
        if not user:
            # Perform dummy verify to keep timing consistent
            verify_password(password, _DUMMY_PASSWORD_HASH)
            time.sleep(0.05)
            raise InvalidCredentialsError("Incorrect email or password")

        if not verify_password(password, user.password):
            time.sleep(0.05)
            raise InvalidCredentialsError("Incorrect email or password")

        if not user.is_active:
            raise UnauthorizedError("User account is inactive")

        return user

    def create_user_settings(
        self,
        user_id: uuid.UUID,
        settings_data: UserSettingsCreate,
        *,
        commit: bool = True
    ) -> UserSettings:
        """Create user settings."""
        settings = UserSettings(
            user_id=user_id,
            **_schema_dump(settings_data)
        )

        self.session.add(settings)
        if commit:
            try:
                self.session.commit()
                self.session.refresh(settings)
            except SQLAlchemyError as exc:
                self.session.rollback()
                log_error(exc)
                raise
        else:
            self.session.flush()

        return settings

    def get_user_settings(self, user_id: str) -> UserSettings:
        """Get user settings."""
        try:
            user_uuid = uuid.UUID(user_id)
            statement = select(UserSettings).where(UserSettings.user_id == user_uuid)
            settings = self.session.exec(statement).first()
            if not settings:
                raise UserSettingsNotFoundError("User settings not found")
            return settings
        except ValueError:
            raise UserNotFoundError("Invalid user ID format")

    def update_user_settings(self, user_id: str, settings_data: UserSettingsUpdate) -> UserSettings:
        """Update user settings."""
        settings = self.get_user_settings(user_id)

        # Update fields
        update_data = _schema_dump(settings_data, exclude_unset=True)
        for field, value in update_data.items():
            setattr(settings, field, value)

        try:
            self.session.add(settings)
            self.session.commit()
            self.session.refresh(settings)
        except SQLAlchemyError as exc:
            self.session.rollback()
            log_error(exc)
            raise

        return settings

    def get_user_timezone(self, user_id: uuid.UUID) -> str:
        """
        Get user's timezone from settings.

        Args:
            user_id: User UUID

        Returns:
            str: IANA timezone string (defaults to "UTC" if not set)
        """
        try:
            statement = select(UserSettings).where(UserSettings.user_id == user_id)
            settings = self.session.exec(statement).first()
            if settings and settings.time_zone:
                return settings.time_zone
        except Exception:
            pass
        return "UTC"
