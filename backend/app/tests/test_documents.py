from unittest.mock import AsyncMock

from httpx import AsyncClient

from app.core.config import settings
from app.modules.embeddings.openai_embeddings import OpenAIEmbeddingProvider
from app.modules.ingestion.chunking import IngestionChunk
from app.modules.vector_store.qdrant import QdrantVectorStore


async def test_upload_markdown_document(client: AsyncClient, monkeypatch) -> None:
    async def fake_embed_documents(self, texts: list[str]) -> list[list[float]]:
        dim = settings.EMBEDDING_DIMENSION
        return [[0.01] * dim for _ in texts]

    monkeypatch.setattr(OpenAIEmbeddingProvider, "embed_documents", fake_embed_documents)
    async def fake_upsert_document_chunks(
        self: QdrantVectorStore,
        document_id: str,
        user_id: str,
        filename: str,
        chunks: list[IngestionChunk],
        vectors: list[list[float]],
    ) -> int:
        return len(chunks)

    monkeypatch.setattr(
        QdrantVectorStore,
        "upsert_document_chunks",
        fake_upsert_document_chunks,
    )

    user_payload = {
        "email": "docs@example.com",
        "password": "strong-password",
        "full_name": "Docs User",
    }
    await client.post("/api/v1/auth/register", json=user_payload)
    login_response = await client.post(
        "/api/v1/auth/login",
        data={
            "username": user_payload["email"],
            "password": user_payload["password"],
        },
    )
    token = login_response.json()["access_token"]

    response = await client.post(
        "/api/v1/documents/upload",
        headers={"Authorization": f"Bearer {token}"},
        files={
            "file": (
                "guide.md",
                b"# Guide\n\nThis is markdown content that should be indexed.",
                "text/markdown",
            )
        },
    )

    assert response.status_code == 201
    data = response.json()
    assert data["original_filename"] == "guide.md"
    assert data["document_type"] == "markdown"
    assert data["status"] == "indexed"
    assert data["chunk_count"] >= 1


async def test_upload_rejects_unsupported_document(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.setattr(QdrantVectorStore, "upsert_document_chunks", AsyncMock(return_value=1))

    user_payload = {
        "email": "reject@example.com",
        "password": "strong-password",
        "full_name": "Reject User",
    }
    await client.post("/api/v1/auth/register", json=user_payload)
    login_response = await client.post(
        "/api/v1/auth/login",
        data={
            "username": user_payload["email"],
            "password": user_payload["password"],
        },
    )
    token = login_response.json()["access_token"]

    response = await client.post(
        "/api/v1/documents/upload",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("notes.txt", b"plain text", "text/plain")},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Only PDF and Markdown files are supported"
