"""Regression tests for classifier draft + Qdrant merged responder prep."""

import pytest

from app.modules.chat.intent import RouteDecision
from app.modules.chat.service import ChatService
from app.modules.vector_store.qdrant import QdrantVectorStore
from app.modules.embeddings.openai_embeddings import OpenAIEmbeddingProvider


@pytest.mark.asyncio
async def test_knowledge_path_merges_indexed_only_classifier_placeholder(monkeypatch) -> None:
    async def fake_route(llm, transcript, *, latest_user_text):
        return RouteDecision(intent="knowledge", search_query="dds general info", overview_answer=None)

    async def fake_embed(self, texts):
        return [[0.1] * 16]

    async def fake_search(self, user_id, query_vector, limit):
        return [
            (
                0.95,
                {
                    "text": "Indexed chunk about Lanterman Act.",
                    "source_type": "website",
                    "url": "https://www.dds.ca.gov/example-services",
                    "title": "Lanterman overview",
                    "filename": "bundle.zip/docs/guide.md",
                    "document_id": "doc-123",
                },
            ),
            (
                0.93,
                {
                    "text": "Indexed chunk without a DDS web URL.",
                    "url": "https://example.org/not-dds",
                    "title": "External page",
                    "filename": "guide.markdown",
                },
            )
        ]

    monkeypatch.setattr("app.modules.chat.service.classify_route", fake_route)
    monkeypatch.setattr(OpenAIEmbeddingProvider, "embed_documents", fake_embed)
    monkeypatch.setattr(QdrantVectorStore, "search_user_chunks", fake_search)

    svc = ChatService()
    msgs = await svc._build_responder_messages("user-id", [], "Tell me about DDS")

    assert msgs[0]["role"] == "system"
    body = msgs[0]["content"]
    assert "California Department of Developmental Services" in body
    assert "CLASSIFIER DRAFT:\n(none)" in body
    assert "[1]" in body
    assert "Lanterman" in body
    assert "DDS website URL: https://www.dds.ca.gov/example-services" in body
    assert "Page title: Lanterman overview" in body
    assert "https://example.org/not-dds" not in body
    assert "External page" not in body
    assert "bundle.zip/docs/guide.md" not in body
    assert "guide.markdown" not in body
    assert "Document ID: doc-123" not in body
    assert "Helpful links" in body
    assert "Do not say you are checking" in body or "Do not write phrases" in body
    assert "I do not have that information available" in body
    assert "Use only actual DDS website URLs" in body


@pytest.mark.asyncio
async def test_answered_from_overview_skips_qdrant(monkeypatch) -> None:
    async def fake_route(llm, transcript, *, latest_user_text):
        return RouteDecision(
            intent="answered_from_overview",
            search_query=None,
            overview_answer="Contact: info@dds.ca.gov",
        )

    called_embed = False

    async def fake_embed(self, texts):
        nonlocal called_embed
        called_embed = True
        return [[0.1] * 16]

    async def fake_search(self, user_id, query_vector, limit):
        raise AssertionError("qdrant should not run for answered_from_overview")

    monkeypatch.setattr("app.modules.chat.service.classify_route", fake_route)
    monkeypatch.setattr(OpenAIEmbeddingProvider, "embed_documents", fake_embed)
    monkeypatch.setattr(QdrantVectorStore, "search_user_chunks", fake_search)

    svc = ChatService()
    msgs = await svc._build_responder_messages("user-id", [], "How do I email DDS?")

    assert not called_embed
    body = msgs[0]["content"]
    assert "info@dds.ca.gov" in body
    assert "INDEXED PASSAGES:\n(none)" in body


@pytest.mark.asyncio
async def test_knowledge_path_no_hit_when_no_passages_above_threshold(monkeypatch) -> None:
    async def fake_route(llm, transcript, *, latest_user_text):
        return RouteDecision(intent="knowledge", search_query="x", overview_answer=None)

    async def fake_embed(self, texts):
        return [[0.1] * 16]

    async def fake_search(self, user_id, query_vector, limit):
        return [(0.1, {"text": "weak chunk"})]

    monkeypatch.setattr("app.modules.chat.service.classify_route", fake_route)
    monkeypatch.setattr(OpenAIEmbeddingProvider, "embed_documents", fake_embed)
    monkeypatch.setattr(QdrantVectorStore, "search_user_chunks", fake_search)

    svc = ChatService()
    msgs = await svc._build_responder_messages("user-id", [], "Obscure question")

    assert msgs[0]["content"].startswith("No relevant passages were found")
