import io
import zipfile
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient

from app.modules.embeddings.openai_embeddings import OpenAIEmbeddingProvider
from app.modules.vector_store.qdrant import QdrantVectorStore


@pytest.fixture
def fake_openai_vectors(monkeypatch) -> None:
    async def fake_embed(self: OpenAIEmbeddingProvider, texts: list[str]) -> list[list[float]]:
        return [[0.01] * 384 for _ in texts]

    monkeypatch.setattr(OpenAIEmbeddingProvider, "embed_documents", fake_embed)


async def _register_and_login(client: AsyncClient, email: str) -> str:
    user_payload = {
        "email": email,
        "password": "strong-password",
        "full_name": "Zip User",
    }
    await client.post("/api/v1/auth/register", json=user_payload)
    login_response = await client.post(
        "/api/v1/auth/login",
        data={
            "username": user_payload["email"],
            "password": user_payload["password"],
        },
    )
    return login_response.json()["access_token"]


def _zip_bytes(inner: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in inner.items():
            zf.writestr(path, content)
    buf.seek(0)
    return buf.read()


async def test_zip_session_manifest_nested_and_sorted(client: AsyncClient) -> None:
    token = await _register_and_login(client, "manifest@example.com")
    zip_bytes = _zip_bytes(
        {
            "readme.markdown": "## Readme\n\nHello **world**.\n",
            "docs/guide.md": "# Guide\n\nQ: First?\nA: One.\n\nQ: Second?\nA: Two.\n",
        }
    )
    r = await client.post(
        "/api/v1/documents/zip-sessions",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("bundle.zip", zip_bytes, "application/zip")},
    )
    assert r.status_code == 201, r.text
    data = r.json()
    paths = [f["path"] for f in data["markdown_files"]]
    assert paths == ["docs/guide.md", "readme.markdown"]
    assert [f["index"] for f in data["markdown_files"]] == [0, 1]
    assert data["original_filename"] == "bundle.zip"
    assert "session_id" in data


async def test_zip_session_ingest_batch(
    client: AsyncClient,
    monkeypatch,
    fake_openai_vectors,
) -> None:
    captured_batches: list[dict] = []

    async def capture_upsert(
        self: QdrantVectorStore,
        document_id: str,
        user_id: str,
        filename: str,
        chunks,
        vectors,
        *,
        per_chunk_payload=None,
    ) -> int:
        captured_batches.append(
            {
                "document_id": document_id,
                "filename": filename,
                "n_chunks": len(chunks),
                "payload0": per_chunk_payload[0] if per_chunk_payload else {},
            },
        )
        return len(chunks)

    monkeypatch.setattr(QdrantVectorStore, "upsert_document_chunks", capture_upsert)

    token = await _register_and_login(client, "ingest@example.com")
    zip_bytes = _zip_bytes(
        {
            "docs/guide.md": "# Guide\n\nQ: First?\nA: One.\n\nQ: Second?\nA: Two.\n",
            "readme.markdown": "## Readme\n\nHello **world**.\n",
        }
    )
    create = await client.post(
        "/api/v1/documents/zip-sessions",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("bundle.zip", zip_bytes, "application/zip")},
    )
    assert create.status_code == 201, create.text
    session_id = create.json()["session_id"]

    ingest = await client.post(
        f"/api/v1/documents/zip-sessions/{session_id}/ingest",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert ingest.status_code == 201, ingest.text
    body = ingest.json()
    assert body["document_type"] == "markdown_zip"
    assert body["status"] == "indexed"
    assert body["files_indexed"] == 2
    assert len(body["indexed_document_ids"]) == 2
    assert body["chunk_count"] >= 2
    assert body["vector_count"] == body["chunk_count"]
    assert body["markdown_files_total"] == 2
    assert body["markdown_skip"] == 0
    assert body["has_more_markdown_files"] is False
    assert body["next_markdown_skip"] is None
    assert body["zip_session_id"] == session_id
    assert "bundle.zip/docs/guide.md" in captured_batches[0]["filename"]
    assert "bundle.zip/readme.markdown" in captured_batches[1]["filename"]
    assert captured_batches[0]["payload0"].get("source_type") == "markdown_zip"
    assert captured_batches[0]["payload0"].get("zip_inner_path") == "docs/guide.md"
    assert captured_batches[0]["payload0"].get("zip_markdown_files_total") == 2


async def test_upload_zip_rejects_on_single_file_upload(client: AsyncClient, monkeypatch) -> None:
    monkeypatch.setattr(QdrantVectorStore, "upsert_document_chunks", AsyncMock(return_value=1))

    token = await _register_and_login(client, "zipreject@example.com")
    zip_bytes = _zip_bytes({"a.md": "# x"})
    r = await client.post(
        "/api/v1/documents/upload",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("x.zip", zip_bytes, "application/zip")},
    )
    assert r.status_code == 400


async def test_zip_session_skips_node_modules_markdown(
    client: AsyncClient,
    monkeypatch,
    fake_openai_vectors,
) -> None:
    monkeypatch.setattr(QdrantVectorStore, "upsert_document_chunks", AsyncMock(return_value=1))

    token = await _register_and_login(client, "skipnm@example.com")
    zip_bytes = _zip_bytes(
        {
            "node_modules/pkg/readme.md": "# Dep readme",
            "docs/keep.md": "# Keep\n\nBody.",
        }
    )
    create = await client.post(
        "/api/v1/documents/zip-sessions",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("skip.zip", zip_bytes, "application/zip")},
    )
    assert create.status_code == 201
    assert create.json()["markdown_files"][0]["path"] == "docs/keep.md"

    session_id = create.json()["session_id"]
    ingest = await client.post(
        f"/api/v1/documents/zip-sessions/{session_id}/ingest",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert ingest.status_code == 201
    ing = ingest.json()
    assert ing["files_indexed"] == 1
    assert len(ing["indexed_document_ids"]) == 1
    assert ing["markdown_files_total"] == 1


async def test_zip_session_pagination_then_session_closes(
    client: AsyncClient,
    monkeypatch,
    fake_openai_vectors,
) -> None:
    async def upsert_len(self, **kwargs):
        return len(kwargs["chunks"])

    monkeypatch.setattr(QdrantVectorStore, "upsert_document_chunks", upsert_len)

    token = await _register_and_login(client, "zippage@example.com")
    zip_bytes = _zip_bytes(
        {
            "a/a.md": "# A\n\nalpha.",
            "b/b.md": "# B\n\nbeta.",
            "c/c.md": "# C\n\ngamma.",
        }
    )
    create = await client.post(
        "/api/v1/documents/zip-sessions",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("parts.zip", zip_bytes, "application/zip")},
    )
    assert create.status_code == 201
    session_id = create.json()["session_id"]

    r1 = await client.post(
        f"/api/v1/documents/zip-sessions/{session_id}/ingest",
        headers={"Authorization": f"Bearer {token}"},
        json={"markdown_skip": 0, "markdown_path_limit": 2},
    )
    assert r1.status_code == 201
    d1 = r1.json()
    assert d1["markdown_files_total"] == 3
    assert d1["files_indexed"] == 2
    assert d1["has_more_markdown_files"] is True
    assert d1["next_markdown_skip"] == 2

    r2 = await client.post(
        f"/api/v1/documents/zip-sessions/{session_id}/ingest",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert r2.status_code == 201
    d2 = r2.json()
    assert d2["markdown_files_total"] == 3
    assert d2["files_indexed"] == 1
    assert d2["has_more_markdown_files"] is False
    assert d2["next_markdown_skip"] is None

    r3 = await client.post(
        f"/api/v1/documents/zip-sessions/{session_id}/ingest",
        headers={"Authorization": f"Bearer {token}"},
        json={},
    )
    assert r3.status_code == 400


async def test_zip_session_create_rejects_no_markdown(client: AsyncClient) -> None:
    token = await _register_and_login(client, "emptyzip@example.com")
    zip_bytes = _zip_bytes({"note.txt": "not markdown"})
    r = await client.post(
        "/api/v1/documents/zip-sessions",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("empty.zip", zip_bytes, "application/zip")},
    )
    assert r.status_code == 400


async def test_zip_ingest_path_indices_only_selected_files(
    client: AsyncClient,
    monkeypatch,
    fake_openai_vectors,
) -> None:
    indexed_paths: list[str] = []

    async def capture(self, **kwargs):
        pl = kwargs.get("per_chunk_payload") or []
        if pl:
            indexed_paths.append(pl[0].get("zip_inner_path"))
        return len(kwargs["chunks"])

    monkeypatch.setattr(QdrantVectorStore, "upsert_document_chunks", capture)

    token = await _register_and_login(client, "idx@example.com")
    zip_bytes = _zip_bytes(
        {
            "a.md": "# A\n\none.",
            "nested/b.md": "# B\n\ntwo.",
        }
    )
    create = await client.post(
        "/api/v1/documents/zip-sessions",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("two.zip", zip_bytes, "application/zip")},
    )
    assert create.status_code == 201
    session_id = create.json()["session_id"]
    # sorted: a.md, nested/b.md -> index 1 is nested/b.md
    ingest = await client.post(
        f"/api/v1/documents/zip-sessions/{session_id}/ingest",
        headers={"Authorization": f"Bearer {token}"},
        json={"path_indices": [1]},
    )
    assert ingest.status_code == 201
    ing = ingest.json()
    assert ing["files_indexed"] == 1
    assert len(ing["indexed_document_ids"]) == 1
    assert indexed_paths == ["nested/b.md"]


async def test_delete_zip_session(client: AsyncClient) -> None:
    token = await _register_and_login(client, "delzip@example.com")
    zip_bytes = _zip_bytes({"x.md": "# x"})
    create = await client.post(
        "/api/v1/documents/zip-sessions",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("t.zip", zip_bytes, "application/zip")},
    )
    assert create.status_code == 201
    session_id = create.json()["session_id"]
    r = await client.delete(
        f"/api/v1/documents/zip-sessions/{session_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 204
