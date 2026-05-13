"""Router intent: overview vs knowledge, including server-side guards."""

import pytest

from app.modules.chat import intent as chat_intent
from app.modules.chat.intent import classify_route
from app.modules.chat.llm_client import ChatCompletionClient


def test_message_requires_indexed_knowledge_age() -> None:
    assert chat_intent._message_requires_indexed_knowledge("suggest autism training for 5 year old")


def test_message_requires_indexed_knowledge_not_broad_dds() -> None:
    assert not chat_intent._message_requires_indexed_knowledge("What is the DDS mission?")


@pytest.mark.asyncio
async def test_classify_downgrades_overview_when_age_specific(monkeypatch) -> None:
    async def fake_complete(
        self,
        messages,
        *,
        model=None,
        max_tokens=None,
        temperature=None,
    ):
        return (
            '{"intent":"answered_from_overview","search_query":null,'
            '"overview_answer":"Try Early Start."}'
        )

    monkeypatch.setattr(ChatCompletionClient, "complete", fake_complete)
    llm = ChatCompletionClient()
    decision = await classify_route(
        llm,
        "",
        latest_user_text="suggest autism training for 5 year old",
    )
    assert decision.intent == "knowledge"
    assert decision.search_query == "suggest autism training for 5 year old"
    assert decision.overview_answer is None


@pytest.mark.asyncio
async def test_classify_keeps_overview_when_broad(monkeypatch) -> None:
    async def fake_complete(
        self,
        messages,
        *,
        model=None,
        max_tokens=None,
        temperature=None,
    ):
        return (
            '{"intent":"answered_from_overview","search_query":null,'
            '"overview_answer":"DDS supports Californians with developmental disabilities."}'
        )

    monkeypatch.setattr(ChatCompletionClient, "complete", fake_complete)
    llm = ChatCompletionClient()
    decision = await classify_route(
        llm,
        "",
        latest_user_text="What is the DDS mission?",
    )
    assert decision.intent == "answered_from_overview"
    assert decision.overview_answer == "DDS supports Californians with developmental disabilities."
