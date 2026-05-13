from datetime import UTC, datetime
from enum import StrEnum

from beanie import Document, Indexed, PydanticObjectId
from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_utc(dt: datetime) -> datetime:
    """Normalize for comparisons/serialization; Beanie/Mongo often returns naive UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


class ZipIngestSessionStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    EXPIRED = "expired"


class ZipManifestRow(BaseModel):
    index: int
    path: str
    size_bytes: int


class ZipIngestSession(Document):
    """Uploaded ZIP held for phased manifest + batched ingest (no embeddings until ingest)."""

    user_id: Indexed(PydanticObjectId)
    original_filename: str
    stored_filename: str
    storage_path: str
    zip_size_bytes: int
    content_type: str
    status: ZipIngestSessionStatus = ZipIngestSessionStatus.OPEN
    manifest: list[ZipManifestRow] = Field(default_factory=list)
    next_suggested_skip: int = 0
    expires_at: datetime
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    class Settings:
        name = "zip_ingest_sessions"
