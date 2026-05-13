from datetime import datetime

from beanie import PydanticObjectId
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.config import settings
from app.modules.documents.model import DocumentStatus, DocumentType, SourceDocument
from app.modules.documents.zip_session_model import ZipIngestSession, ensure_utc


class DocumentResponse(BaseModel):
    id: str
    user_id: str
    original_filename: str
    document_type: DocumentType
    content_type: str
    size_bytes: int
    status: DocumentStatus
    chunk_count: int
    vector_count: int
    error_message: str | None
    zip_session_id: str | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @field_validator("id", "user_id", "zip_session_id", mode="before")
    @classmethod
    def stringify_object_id(cls, value: PydanticObjectId | str | None) -> str | None:
        return str(value) if value is not None else None

    @classmethod
    def from_document(cls, document: SourceDocument) -> "DocumentResponse":
        return cls.model_validate(document)


class ZipMarkdownFileEntry(BaseModel):
    """One Markdown member inside a ZIP (root or nested); `path` uses `/`."""

    index: int
    path: str
    size_bytes: int


class ZipSessionResponse(BaseModel):
    """Phase 1: flat manifest only (no embeddings)."""

    session_id: str
    user_id: str
    original_filename: str
    zip_size_bytes: int
    markdown_files: list[ZipMarkdownFileEntry]
    manifest_warnings: list[str] = Field(default_factory=list)
    expires_at: datetime
    default_path_batch: int = Field(default_factory=lambda: settings.ZIP_INGEST_PATH_BATCH_DEFAULT)
    max_path_batch: int = Field(default_factory=lambda: settings.ZIP_INGEST_MAX_PATH_BATCH)
    max_path_indices_per_request: int = Field(
        default_factory=lambda: settings.ZIP_INGEST_MAX_PATH_INDICES
    )
    openai_embedding_text_batch: int = Field(
        default_factory=lambda: settings.OPENAI_EMBEDDING_BATCH_SIZE
    )

    @field_validator("session_id", "user_id", mode="before")
    @classmethod
    def stringify_ids(cls, value: PydanticObjectId | str) -> str:
        return str(value)

    @classmethod
    def from_session(
        cls,
        session: ZipIngestSession,
        *,
        manifest_warnings: list[str],
    ) -> "ZipSessionResponse":
        files = [
            ZipMarkdownFileEntry(index=r.index, path=r.path, size_bytes=r.size_bytes)
            for r in session.manifest
        ]
        return cls(
            session_id=str(session.id),
            user_id=str(session.user_id),
            original_filename=session.original_filename,
            zip_size_bytes=session.zip_size_bytes,
            markdown_files=files,
            manifest_warnings=manifest_warnings,
            expires_at=ensure_utc(session.expires_at),
        )


class ZipIngestBatchRequest(BaseModel):
    """Phase 2: either slice by skip/limit or explicit manifest indices."""

    markdown_skip: int | None = Field(
        default=None,
        ge=0,
        description="Start index into session manifest; defaults to session next_suggested_skip",
    )
    markdown_path_limit: int | None = Field(
        default=None,
        ge=1,
        description="Max Markdown files this request; defaults to ZIP_INGEST_PATH_BATCH_DEFAULT",
    )
    path_indices: list[int] | None = Field(
        default=None,
        description="Process these manifest indices only (mutually exclusive with skip/limit)",
    )

    @model_validator(mode="after")
    def exclusive_slice_or_indices(self) -> "ZipIngestBatchRequest":
        if self.path_indices is not None:
            if len(self.path_indices) == 0:
                raise ValueError("path_indices cannot be empty; omit the field to use skip/limit")
            if self.markdown_skip is not None or self.markdown_path_limit is not None:
                msg = "Provide either path_indices or markdown_skip/markdown_path_limit, not both"
                raise ValueError(msg)
        return self


class ZipBulkIngestResponse(DocumentResponse):
    """Response for one ingest batch (one Mongo row + Qdrant points per inner Markdown file)."""

    files_indexed: int = 0
    files_skipped: int = 0
    warnings: list[str] = Field(default_factory=list)
    markdown_files_total: int = 0
    markdown_skip: int = 0
    markdown_limit: int = 0
    has_more_markdown_files: bool = False
    next_markdown_skip: int | None = None
    zip_session_id: str | None = None
    indexed_document_ids: list[str] = Field(default_factory=list)

    @classmethod
    def build(
        cls,
        document: SourceDocument,
        *,
        files_indexed: int,
        files_skipped: int,
        warnings: list[str],
        markdown_files_total: int,
        markdown_skip: int,
        markdown_limit: int,
        has_more_markdown_files: bool,
        next_markdown_skip: int | None,
        zip_session_id: str | None = None,
        indexed_document_ids: list[str] | None = None,
        aggregate_chunk_count: int | None = None,
        aggregate_vector_count: int | None = None,
    ) -> "ZipBulkIngestResponse":
        base = DocumentResponse.from_document(document).model_dump()
        if aggregate_chunk_count is not None:
            base["chunk_count"] = aggregate_chunk_count
        if aggregate_vector_count is not None:
            base["vector_count"] = aggregate_vector_count
        # Avoid duplicate kwargs: DocumentResponse already includes zip_session_id from the model.
        base.pop("zip_session_id", None)
        return cls(
            **base,
            files_indexed=files_indexed,
            files_skipped=files_skipped,
            warnings=warnings,
            markdown_files_total=markdown_files_total,
            markdown_skip=markdown_skip,
            markdown_limit=markdown_limit,
            has_more_markdown_files=has_more_markdown_files,
            next_markdown_skip=next_markdown_skip,
            zip_session_id=zip_session_id,
            indexed_document_ids=indexed_document_ids or [],
        )
