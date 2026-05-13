from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from pydantic import ValidationError

from app.api.deps import get_current_user
from app.modules.documents.schemas import (
    DocumentResponse,
    ZipBulkIngestResponse,
    ZipIngestBatchRequest,
    ZipSessionResponse,
)
from app.modules.documents.service import DocumentService
from app.modules.users.model import User

router = APIRouter()

_ZIP_INGEST_OPENAPI_EXTRA = {
    "requestBody": {
        "required": False,
        "description": (
            "Optional JSON body; omit or send `{}` to use session defaults for skip/limit slicing."
        ),
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/ZipIngestBatchRequest"},
            }
        },
    },
}


@router.post("/upload", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
async def upload_document(
    current_user: Annotated[User, Depends(get_current_user)],
    file: Annotated[UploadFile, File(...)],
) -> DocumentResponse:
    return await DocumentService().upload_document(str(current_user.id), file)


@router.post(
    "/zip-sessions",
    response_model=ZipSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_zip_markdown_session(
    current_user: Annotated[User, Depends(get_current_user)],
    file: Annotated[UploadFile, File(...)],
) -> ZipSessionResponse:
    """Phase 1: store ZIP and return a flat sorted manifest (root + nested paths). No embeddings."""
    return await DocumentService().create_zip_markdown_session(str(current_user.id), file)


@router.post(
    "/zip-sessions/{session_id}/ingest",
    response_model=ZipBulkIngestResponse,
    status_code=status.HTTP_201_CREATED,
    openapi_extra=_ZIP_INGEST_OPENAPI_EXTRA,
)
async def ingest_zip_session_batch(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    session_id: str,
) -> ZipBulkIngestResponse:
    """Phase 2: chunk + OpenAI embeddings + Qdrant for one path batch (configurable skip/limit)."""
    raw = await request.body()
    try:
        body = (
            ZipIngestBatchRequest.model_validate_json(raw)
            if raw.strip()
            else ZipIngestBatchRequest()
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    return await DocumentService().ingest_zip_session_batch(
        str(current_user.id), session_id, body
    )


@router.delete("/zip-sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_zip_markdown_session(
    session_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
) -> None:
    await DocumentService().delete_zip_markdown_session(str(current_user.id), session_id)


@router.get("", response_model=list[DocumentResponse])
async def list_documents(
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[DocumentResponse]:
    return await DocumentService().list_documents(str(current_user.id))


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
) -> DocumentResponse:
    return await DocumentService().get_document(str(current_user.id), document_id)
