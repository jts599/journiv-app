"""
Journal schemas.
"""
import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from app.models.enums import JournalColor


class JournalBase(BaseModel):
    """Base journal schema."""
    title: str
    description: Optional[str] = None
    color: Optional[JournalColor] = None
    icon: Optional[str] = None


class JournalCreate(JournalBase):
    """Journal creation schema."""
    pass


class JournalUpdate(BaseModel):
    """Journal update schema."""
    title: Optional[str] = None
    description: Optional[str] = None
    color: Optional[JournalColor] = None
    icon: Optional[str] = None
    is_favorite: Optional[bool] = None
    is_archived: Optional[bool] = None


class JournalResponse(JournalBase):
    """Journal response schema."""
    id: uuid.UUID
    user_id: uuid.UUID
    is_favorite: bool
    is_archived: bool
    entry_count: int
    last_entry_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
