from datetime import UTC, datetime

from beanie import PydanticObjectId

from app.modules.documents.model import DocumentStatus, SourceDocument


class DocumentRepository:
    async def create(self, document: SourceDocument) -> SourceDocument:
        await document.insert()
        return document

    async def get_for_user(self, document_id: str, user_id: str) -> SourceDocument | None:
        if not PydanticObjectId.is_valid(document_id) or not PydanticObjectId.is_valid(user_id):
            return None
        return await SourceDocument.find_one(
            SourceDocument.id == PydanticObjectId(document_id),
            SourceDocument.user_id == PydanticObjectId(user_id),
        )

    async def list_for_user(self, user_id: str) -> list[SourceDocument]:
        if not PydanticObjectId.is_valid(user_id):
            return []
        return (
            await SourceDocument.find(SourceDocument.user_id == PydanticObjectId(user_id))
            .sort("-created_at")
            .to_list()
        )

    async def mark_processing(self, document: SourceDocument) -> SourceDocument:
        document.status = DocumentStatus.PROCESSING
        document.error_message = None
        return await self.save(document)

    async def mark_indexed(
        self,
        document: SourceDocument,
        chunk_count: int,
        vector_count: int,
    ) -> SourceDocument:
        document.status = DocumentStatus.INDEXED
        document.chunk_count = chunk_count
        document.vector_count = vector_count
        document.error_message = None
        return await self.save(document)

    async def mark_failed(self, document: SourceDocument, error_message: str) -> SourceDocument:
        document.status = DocumentStatus.FAILED
        document.error_message = error_message[:500]
        return await self.save(document)

    async def save(self, document: SourceDocument) -> SourceDocument:
        document.updated_at = datetime.now(UTC)
        await document.save()
        return document
