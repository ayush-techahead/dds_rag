import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePath, PurePosixPath
from typing import Any
from uuid import uuid4

from beanie import PydanticObjectId
from fastapi import UploadFile

from app.core.config import settings
from app.core.exceptions import AppException, BadRequestException, NotFoundException
from app.modules.documents.markdown import MarkdownTextExtractor
from app.modules.documents.model import DocumentType, SourceDocument
from app.modules.documents.pdf import PdfTextExtractor
from app.modules.documents.repository import DocumentRepository
from app.modules.documents.schemas import (
    DocumentResponse,
    ZipBulkIngestResponse,
    ZipIngestBatchRequest,
    ZipSessionResponse,
)
from app.modules.documents.zip_manifest import build_markdown_zip_manifest
from app.modules.documents.zip_session_model import (
    ZipIngestSession,
    ZipIngestSessionStatus,
    ZipManifestRow,
    ensure_utc,
)
from app.modules.documents.zip_session_repository import ZipIngestSessionRepository
from app.modules.embeddings.openai_embeddings import OpenAIEmbeddingProvider
from app.modules.ingestion.chunking import IngestionChunk, resolve_chunker_for_ingestion
from app.modules.ingestion.markdown_clean import clean_markdown_for_ingestion
from app.modules.vector_store.qdrant import QdrantVectorStore

_ZIP_MEMBER_DB_NAME_MAX_LEN = 500


def _zip_member_display_name(archive_original_filename: str, inner_path: str) -> str:
    """Stable display + DB name: `{archive.zip}/{nested/path.md}` (nested path from ZIP root)."""
    archive = PurePosixPath(archive_original_filename).name
    inner = inner_path.replace("\\", "/").strip("/")
    if not inner:
        return archive[:_ZIP_MEMBER_DB_NAME_MAX_LEN]
    combined = f"{archive}/{inner}"
    return combined[:_ZIP_MEMBER_DB_NAME_MAX_LEN]


class DocumentService:
    def __init__(
        self,
        repository: DocumentRepository | None = None,
        pdf_extractor: PdfTextExtractor | None = None,
        markdown_extractor: MarkdownTextExtractor | None = None,
        vector_store: QdrantVectorStore | None = None,
        openai_embeddings: OpenAIEmbeddingProvider | None = None,
        zip_session_repository: ZipIngestSessionRepository | None = None,
    ) -> None:
        self.repository = repository or DocumentRepository()
        self.pdf_extractor = pdf_extractor or PdfTextExtractor()
        self.markdown_extractor = markdown_extractor or MarkdownTextExtractor()
        self.vector_store = vector_store or QdrantVectorStore()
        self.openai_embeddings = openai_embeddings or OpenAIEmbeddingProvider()
        self.zip_sessions = zip_session_repository or ZipIngestSessionRepository()

    async def upload_document(self, user_id: str, upload_file: UploadFile) -> DocumentResponse:
        document_type = self._get_document_type(upload_file)

        stored_path, size_bytes, stored_filename = await self._store_upload(user_id, upload_file)
        document = await self.repository.create(
            SourceDocument(
                user_id=PydanticObjectId(user_id),
                original_filename=PurePath(upload_file.filename or "document").name,
                stored_filename=stored_filename,
                document_type=document_type,
                content_type=upload_file.content_type or "application/octet-stream",
                size_bytes=size_bytes,
                storage_path=str(stored_path),
            )
        )

        try:
            await self.repository.mark_processing(document)
            text = self._extract_text(document_type, stored_path)
            chunks = resolve_chunker_for_ingestion(text, document_type.value).split(text)
            if not chunks:
                raise BadRequestException("Document does not contain enough text to index")

            chunk_texts = [chunk.text for chunk in chunks]
            vectors = await self.openai_embeddings.embed_documents(chunk_texts)
            vector_count = await self.vector_store.upsert_document_chunks(
                document_id=str(document.id),
                user_id=user_id,
                filename=document.original_filename,
                chunks=chunks,
                vectors=vectors,
            )
            indexed_document = await self.repository.mark_indexed(
                document,
                chunk_count=len(chunks),
                vector_count=vector_count,
            )
            return DocumentResponse.from_document(indexed_document)
        except AppException as exc:
            await self.repository.mark_failed(document, exc.message)
            raise
        except Exception as exc:
            await self.repository.mark_failed(document, "Unexpected document indexing failure")
            raise BadRequestException("Could not process uploaded document") from exc

    async def list_documents(self, user_id: str) -> list[DocumentResponse]:
        documents = await self.repository.list_for_user(user_id)
        return [DocumentResponse.from_document(document) for document in documents]

    async def get_document(self, user_id: str, document_id: str) -> DocumentResponse:
        document = await self.repository.get_for_user(document_id, user_id)
        if document is None:
            from app.core.exceptions import NotFoundException

            raise NotFoundException("Document not found")
        return DocumentResponse.from_document(document)

    def _get_document_type(self, upload_file: UploadFile) -> DocumentType:
        filename = PurePath(upload_file.filename or "").name
        if not filename:
            raise BadRequestException("A document file is required")

        suffix = Path(filename).suffix.lower()
        content_type = upload_file.content_type or "application/octet-stream"

        if suffix == ".pdf" and content_type in {"application/pdf", "application/octet-stream"}:
            return DocumentType.PDF
        if suffix in {".md", ".markdown"} and content_type in {
            "text/markdown",
            "text/plain",
            "application/octet-stream",
        }:
            return DocumentType.MARKDOWN

        raise BadRequestException("Only PDF and Markdown files are supported")

    def _extract_text(self, document_type: DocumentType, stored_path: Path) -> str:
        if document_type == DocumentType.PDF:
            return self.pdf_extractor.extract_text(stored_path)
        if document_type == DocumentType.MARKDOWN:
            return self.markdown_extractor.extract_text(stored_path)
        raise BadRequestException("Unsupported document type")

    async def _store_upload(self, user_id: str, upload_file: UploadFile) -> tuple[Path, int, str]:
        storage_dir = Path(settings.STORAGE_DIR) / "documents" / user_id
        storage_dir.mkdir(parents=True, exist_ok=True)

        original_name = PurePath(upload_file.filename or "document.pdf").name
        stored_filename = f"{uuid4().hex}-{original_name}"
        stored_path = storage_dir / stored_filename
        max_size_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024

        size_bytes = 0
        with stored_path.open("wb") as target:
            while chunk := await upload_file.read(1024 * 1024):
                size_bytes += len(chunk)
                if size_bytes > max_size_bytes:
                    stored_path.unlink(missing_ok=True)
                    raise BadRequestException(
                        f"PDF size must be {settings.MAX_UPLOAD_SIZE_MB} MB or less"
                    )
                target.write(chunk)

        if size_bytes == 0:
            stored_path.unlink(missing_ok=True)
            raise BadRequestException("Uploaded file is empty")

        return stored_path, size_bytes, stored_filename

    def _validate_zip_upload(self, upload_file: UploadFile) -> tuple[str, str]:
        filename = PurePath(upload_file.filename or "").name
        if not filename.lower().endswith(".zip"):
            raise BadRequestException("ZIP archive required (.zip)")
        content_type = upload_file.content_type or "application/octet-stream"
        if content_type not in {
            "application/zip",
            "application/x-zip-compressed",
            "application/octet-stream",
        }:
            raise BadRequestException("Invalid content type for ZIP upload")
        return filename, content_type

    async def _store_zip_session_file(
        self, user_id: str, upload_file: UploadFile
    ) -> tuple[Path, int, str]:
        storage_dir = Path(settings.STORAGE_DIR) / "zip_sessions" / user_id
        storage_dir.mkdir(parents=True, exist_ok=True)
        original_name = PurePath(upload_file.filename or "archive.zip").name
        stored_filename = f"{uuid4().hex}-{original_name}"
        stored_path = storage_dir / stored_filename
        max_size_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
        size_bytes = 0
        with stored_path.open("wb") as target:
            while chunk := await upload_file.read(1024 * 1024):
                size_bytes += len(chunk)
                if size_bytes > max_size_bytes:
                    stored_path.unlink(missing_ok=True)
                    raise BadRequestException(
                        f"ZIP size must be {settings.MAX_UPLOAD_SIZE_MB} MB or less"
                    )
                target.write(chunk)
        if size_bytes == 0:
            stored_path.unlink(missing_ok=True)
            raise BadRequestException("Uploaded ZIP is empty")
        return stored_path, size_bytes, stored_filename

    async def create_zip_markdown_session(
        self, user_id: str, upload_file: UploadFile
    ) -> ZipSessionResponse:
        filename, content_type = self._validate_zip_upload(upload_file)
        stored_path, size_bytes, stored_filename = await self._store_zip_session_file(
            user_id, upload_file
        )
        manifest_warnings: list[str] = []
        try:
            with zipfile.ZipFile(stored_path, "r") as zf:
                bad = zf.testzip()
                if bad is not None:
                    raise BadRequestException(f"ZIP archive is corrupt (failed on entry: {bad})")
                entries, mw = build_markdown_zip_manifest(zf)
                manifest_warnings.extend(mw)
            if not entries:
                stored_path.unlink(missing_ok=True)
                raise BadRequestException(
                    "ZIP does not contain any indexable Markdown (.md) files"
                )
            expires_at = datetime.now(UTC) + timedelta(hours=settings.ZIP_SESSION_TTL_HOURS)
            session = ZipIngestSession(
                user_id=PydanticObjectId(user_id),
                original_filename=filename,
                stored_filename=stored_filename,
                storage_path=str(stored_path),
                zip_size_bytes=size_bytes,
                content_type=content_type,
                manifest=[
                    ZipManifestRow(index=e.index, path=e.path, size_bytes=e.size_bytes)
                    for e in entries
                ],
                next_suggested_skip=0,
                expires_at=expires_at,
            )
            await self.zip_sessions.create(session)
            return ZipSessionResponse.from_session(
                session, manifest_warnings=manifest_warnings[:200]
            )
        except AppException:
            stored_path.unlink(missing_ok=True)
            raise
        except BadRequestException:
            stored_path.unlink(missing_ok=True)
            raise
        except Exception as exc:
            stored_path.unlink(missing_ok=True)
            raise BadRequestException("Could not inspect ZIP archive") from exc

    async def delete_zip_markdown_session(self, user_id: str, session_id: str) -> None:
        session = await self.zip_sessions.get_for_user(session_id, user_id)
        if session is None:
            raise NotFoundException("ZIP session not found")
        path = Path(session.storage_path)
        if path.is_file():
            path.unlink(missing_ok=True)
        await self.zip_sessions.delete(session)

    async def ingest_zip_session_batch(
        self,
        user_id: str,
        session_id: str,
        payload: ZipIngestBatchRequest,
    ) -> ZipBulkIngestResponse:
        session = await self.zip_sessions.get_for_user(session_id, user_id)
        if session is None:
            raise NotFoundException("ZIP session not found")
        if session.status != ZipIngestSessionStatus.OPEN:
            raise BadRequestException("ZIP session is not open for ingestion")
        if datetime.now(UTC) > ensure_utc(session.expires_at):
            raise BadRequestException("ZIP session has expired; create a new session")

        manifest = session.manifest
        total_md = len(manifest)
        if total_md == 0:
            raise BadRequestException("Session manifest is empty")

        use_manifest_indices = (
            payload.path_indices is not None and len(payload.path_indices) > 0
        )

        if use_manifest_indices:
            max_idx = settings.ZIP_INGEST_MAX_PATH_INDICES
            if len(payload.path_indices) > max_idx:
                raise BadRequestException(
                    f"path_indices may contain at most {max_idx} entries",
                )
            idx_set = sorted({i for i in payload.path_indices if 0 <= i < total_md})
            if not idx_set:
                raise BadRequestException("No valid path_indices in manifest range")
            batch_rows = [manifest[i] for i in idx_set]
            skip_report = idx_set[0]
            limit_report = len(batch_rows)
            has_more = False
            next_skip: int | None = None
        else:
            skip_used = (
                payload.markdown_skip
                if payload.markdown_skip is not None
                else session.next_suggested_skip
            )
            if skip_used >= total_md:
                raise BadRequestException(
                    f"markdown_skip ({skip_used}) exceeds manifest length ({total_md})"
                )
            if payload.markdown_path_limit is not None:
                limit_used = payload.markdown_path_limit
            else:
                limit_used = settings.ZIP_INGEST_PATH_BATCH_DEFAULT
            limit_used = min(max(1, limit_used), settings.ZIP_INGEST_MAX_PATH_BATCH)
            batch_rows = manifest[skip_used : skip_used + limit_used]
            skip_report = skip_used
            limit_report = limit_used
            has_more = skip_used + len(batch_rows) < total_md
            next_skip = skip_used + len(batch_rows) if has_more else None

        zip_path = Path(session.storage_path)
        if not zip_path.is_file():
            raise BadRequestException("ZIP file is missing from storage; create a new session")

        sid = PydanticObjectId(session_id)
        warnings: list[str] = []
        files_skipped = 0
        prepared: list[tuple[ZipManifestRow, str, list[IngestionChunk]]] = []

        with zipfile.ZipFile(zip_path, "r") as zf:
            for row in batch_rows:
                entry_name = row.path
                info = zf.getinfo(entry_name)
                if info.file_size > settings.ZIP_INGEST_MAX_ENTRY_BYTES:
                    warnings.append(
                        f"Skipped {entry_name}: file exceeds ZIP_INGEST_MAX_ENTRY_BYTES",
                    )
                    files_skipped += 1
                    continue

                try:
                    raw_bytes = zf.read(entry_name)
                except OSError as exc:
                    warnings.append(f"Skipped {entry_name}: {exc}")
                    files_skipped += 1
                    continue

                try:
                    raw_text = raw_bytes.decode("utf-8")
                except UnicodeDecodeError:
                    warnings.append(f"Skipped {entry_name}: not valid UTF-8")
                    files_skipped += 1
                    continue

                cleaned = clean_markdown_for_ingestion(raw_text)
                if not cleaned.strip():
                    warnings.append(f"Skipped {entry_name}: empty after cleaning")
                    files_skipped += 1
                    continue

                chunks = resolve_chunker_for_ingestion(cleaned, "markdown").split(cleaned)
                if not chunks:
                    warnings.append(f"Skipped {entry_name}: no chunks produced")
                    files_skipped += 1
                    continue

                prepared.append((row, entry_name, chunks))

        if not prepared:
            raise BadRequestException("No Markdown content produced chunks after processing")

        all_chunks: list[IngestionChunk] = []
        per_chunk_payload: list[dict[str, Any]] = []
        for row, entry_name, chunks in prepared:
            inner_base = PurePosixPath(entry_name).name
            display_name = _zip_member_display_name(session.original_filename, entry_name)
            inner_norm = entry_name.replace("\\", "/")
            for _ch in chunks:
                all_chunks.append(_ch)
                per_chunk_payload.append(
                    {
                        "filename": display_name,
                        "source_type": "markdown_zip",
                        "zip_archive_filename": session.original_filename,
                        "zip_inner_path": inner_norm,
                        "inner_filename": inner_base,
                        "zip_manifest_index": row.index,
                        "zip_markdown_files_total": total_md,
                        "zip_markdown_batch_skip": skip_report,
                        "zip_markdown_batch_limit": limit_report,
                    },
                )

        chunk_texts = [c.text for c in all_chunks]
        try:
            vectors = await self.openai_embeddings.embed_documents(chunk_texts)
        except AppException:
            raise
        except Exception as exc:
            raise BadRequestException("Could not embed ZIP batch contents") from exc

        indexed_docs: list[SourceDocument] = []
        offset = 0
        for row, entry_name, chunks in prepared:
            display_name = _zip_member_display_name(session.original_filename, entry_name)
            inner_norm = entry_name.replace("\\", "/")
            n = len(chunks)
            slice_vec = vectors[offset : offset + n]
            slice_payload = per_chunk_payload[offset : offset + n]
            offset += n

            document = await self.repository.create(
                SourceDocument(
                    user_id=PydanticObjectId(user_id),
                    original_filename=display_name,
                    stored_filename=f"{uuid4().hex}-{inner_norm.replace('/', '_')[:120]}.md",
                    document_type=DocumentType.MARKDOWN_ZIP,
                    content_type=session.content_type,
                    size_bytes=row.size_bytes,
                    storage_path=str(zip_path),
                    zip_session_id=sid,
                ),
            )
            try:
                await self.repository.mark_processing(document)
                vector_count = await self.vector_store.upsert_document_chunks(
                    document_id=str(document.id),
                    user_id=user_id,
                    filename=display_name,
                    chunks=chunks,
                    vectors=slice_vec,
                    per_chunk_payload=slice_payload,
                )
                indexed = await self.repository.mark_indexed(
                    document,
                    chunk_count=n,
                    vector_count=vector_count,
                )
                indexed_docs.append(indexed)
            except AppException as exc:
                await self.repository.mark_failed(document, exc.message)
                warnings.append(f"{inner_norm}: {exc.message}")
                files_skipped += 1
            except Exception as exc:
                await self.repository.mark_failed(
                    document,
                    "Unexpected per-file ZIP indexing failure",
                )
                warnings.append(f"{inner_norm}: indexing failed ({type(exc).__name__})")
                files_skipped += 1

        files_indexed = len(indexed_docs)
        if files_indexed == 0:
            raise BadRequestException("No Markdown files could be indexed in this batch")

        if not use_manifest_indices:
            session.next_suggested_skip = skip_used + len(batch_rows)
            if session.next_suggested_skip >= total_md:
                if zip_path.is_file():
                    zip_path.unlink(missing_ok=True)
                session.storage_path = ""
                session.status = ZipIngestSessionStatus.CLOSED
            await self.zip_sessions.save(session)

        primary = indexed_docs[0]
        agg_chunks = sum(d.chunk_count for d in indexed_docs)
        agg_vectors = sum(d.vector_count for d in indexed_docs)
        return ZipBulkIngestResponse.build(
            primary,
            files_indexed=files_indexed,
            files_skipped=files_skipped,
            warnings=warnings[:100],
            markdown_files_total=total_md,
            markdown_skip=skip_report,
            markdown_limit=limit_report,
            has_more_markdown_files=has_more,
            next_markdown_skip=next_skip,
            zip_session_id=session_id,
            indexed_document_ids=[str(d.id) for d in indexed_docs],
            aggregate_chunk_count=agg_chunks,
            aggregate_vector_count=agg_vectors,
        )
