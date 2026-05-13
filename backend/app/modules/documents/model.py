from datetime import UTC, datetime
from enum import StrEnum

from beanie import Document, Indexed, PydanticObjectId
from pydantic import Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class DocumentStatus(StrEnum):
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    INDEXED = "indexed"
    FAILED = "failed"


class DocumentType(StrEnum):
    PDF = "pdf"
    MARKDOWN = "markdown"
    MARKDOWN_ZIP = "markdown_zip"


class SourceDocument(Document):
    user_id: Indexed(PydanticObjectId)
    original_filename: str
    stored_filename: str
    document_type: DocumentType = DocumentType.PDF
    content_type: str
    size_bytes: int
    storage_path: str
    zip_session_id: PydanticObjectId | None = None
    status: DocumentStatus = DocumentStatus.UPLOADED
    chunk_count: int = 0
    vector_count: int = 0
    error_message: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    class Settings:
        name = "documents"
