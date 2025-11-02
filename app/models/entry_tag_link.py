"""
Entry-Tag link model.
"""
import uuid

from sqlalchemy import Column, ForeignKey
from sqlmodel import Field, Index, SQLModel

from .base import TimestampMixin


class EntryTagLink(TimestampMixin, SQLModel, table=True):
    """
    Link table for many-to-many relationship between entries and tags.
    """
    __tablename__ = "entry_tag_link"

    entry_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("entry.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False
        )
    )
    tag_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("tag.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False
        )
    )

    # Table constraints and indexes
    __table_args__ = (
        # We only need a separate index for tag_id lookups to efficiently find all entries for a given tag.
        Index('idx_entry_tag_link_tag_id', 'tag_id'),
    )
