from typing import Any
from uuid import NAMESPACE_URL, uuid5

from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models

from app.core.config import settings
from app.core.exceptions import ServiceUnavailableException
from app.modules.ingestion.chunking import IngestionChunk


class QdrantVectorStore:
    def __init__(self) -> None:
        self.collection_name = settings.QDRANT_COLLECTION_NAME
        self.client = AsyncQdrantClient(
            url=settings.QDRANT_URL,
            api_key=settings.QDRANT_API_KEY or None,
        )

    async def _ensure_user_id_keyword_index(self) -> None:
        """Qdrant Cloud requires a keyword index on user_id for filtered vector search."""
        try:
            await self.client.create_payload_index(
                collection_name=self.collection_name,
                field_name="user_id",
                field_schema=models.PayloadSchemaType.KEYWORD,
            )
        except Exception as exc:
            err = str(exc).lower()
            if any(
                s in err
                for s in (
                    "already exists",
                    "already exist",
                    "duplicate",
                    "not changed",
                )
            ):
                return
            raise ServiceUnavailableException(
                "Vector store: could not create user_id keyword index "
                "(required for filtered search on Qdrant Cloud)",
            ) from exc

    async def ensure_collection(self) -> None:
        try:
            await self.client.get_collection(self.collection_name)
        except Exception:
            try:
                await self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=models.VectorParams(
                        size=settings.EMBEDDING_DIMENSION,
                        distance=models.Distance.COSINE,
                    ),
                )
            except Exception as exc:
                raise ServiceUnavailableException("Vector store is unavailable") from exc
        await self._ensure_user_id_keyword_index()

    async def upsert_document_chunks(
        self,
        document_id: str,
        user_id: str,
        filename: str,
        chunks: list[IngestionChunk],
        vectors: list[list[float]],
        *,
        per_chunk_payload: list[dict[str, Any]] | None = None,
    ) -> int:
        if len(chunks) != len(vectors):
            msg = "Chunk and vector counts do not match"
            raise ValueError(msg)
        if per_chunk_payload is not None and len(per_chunk_payload) != len(chunks):
            msg = "per_chunk_payload length must match chunks"
            raise ValueError(msg)

        await self.ensure_collection()

        points: list[models.PointStruct] = []
        for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True)):
            payload: dict[str, Any] = {
                "document_id": document_id,
                "user_id": user_id,
                "filename": filename,
                "chunk_index": index,
                "text": chunk.text,
                "section_title": chunk.section_title,
                "section_path": chunk.section_path,
            }
            if per_chunk_payload is not None:
                extra = per_chunk_payload[index]
                if extra:
                    payload.update(extra)
            points.append(
                models.PointStruct(
                    id=str(uuid5(NAMESPACE_URL, f"{document_id}:{index}")),
                    vector=vector,
                    payload=payload,
                )
            )

        try:
            await self.client.upsert(collection_name=self.collection_name, points=points)
        except Exception as exc:
            raise ServiceUnavailableException("Could not write vectors to Qdrant") from exc

        return len(points)

    async def search_user_chunks(
        self,
        *,
        user_id: str,
        query_vector: list[float],
        limit: int,
    ) -> list[tuple[float, dict[str, Any]]]:
        """Cosine similarity search; returns (score, payload) pairs, highest score first."""
        await self.ensure_collection()
        try:
            results = await self.client.search(
                collection_name=self.collection_name,
                query_vector=query_vector,
                limit=limit,
                query_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="user_id",
                            match=models.MatchValue(value=user_id),
                        )
                    ]
                ),
                with_payload=True,
            )
        except Exception as exc:
            raise ServiceUnavailableException("Vector search failed") from exc

        out: list[tuple[float, dict[str, Any]]] = []
        for hit in results:
            payload = hit.payload if isinstance(hit.payload, dict) else {}
            out.append((float(hit.score), payload))
        return out

    async def upsert_website_chunks(
        self,
        website_id: str,
        page_id: str,
        crawl_job_id: str,
        user_id: str,
        url: str,
        title: str | None,
        chunks: list[IngestionChunk],
        vectors: list[list[float]],
    ) -> int:
        if len(chunks) != len(vectors):
            msg = "Chunk and vector counts do not match"
            raise ValueError(msg)

        await self.ensure_collection()

        points = [
            models.PointStruct(
                id=str(uuid5(NAMESPACE_URL, f"website:{page_id}:{index}")),
                vector=vector,
                payload={
                    "source_type": "website",
                    "website_id": website_id,
                    "page_id": page_id,
                    "crawl_job_id": crawl_job_id,
                    "user_id": user_id,
                    "url": url,
                    "title": title,
                    "chunk_index": index,
                    "text": chunk.text,
                    "section_title": chunk.section_title,
                    "section_path": chunk.section_path,
                },
            )
            for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True))
        ]

        try:
            await self.client.upsert(collection_name=self.collection_name, points=points)
        except Exception as exc:
            raise ServiceUnavailableException("Could not write website vectors to Qdrant") from exc

        return len(points)
