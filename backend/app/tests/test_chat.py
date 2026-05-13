import json
import logging

import pytest
from httpx import AsyncClient

from app.core.exceptions import ServiceUnavailableException
from app.modules.chat.audit import RealtimeSessionEvent, VoiceEventType
from app.modules.chat.intent import RouteDecision
from app.modules.chat.llm_client import ChatCompletionClient
from app.modules.chat.realtime_session import build_session_payload, mint_openai_realtime_session
from app.modules.chat.repository import ChatRepository
from app.modules.chat.schemas import RealtimeClientSecret, RealtimeSessionMintResponse
from app.modules.chat.service import ChatService
from app.modules.embeddings.query_cache import get_query_embedding_cache


@pytest.fixture
def bypass_chat_router(monkeypatch):
    """Skip real router LLM + embeddings in chat tests; behave as greeting intent."""

    async def fake_classify(llm, transcript, *, latest_user_text):
        return RouteDecision(intent="greeting", search_query=None)

    monkeypatch.setattr(
        "app.modules.chat.service.classify_route",
        fake_classify,
    )


async def test_send_message_returns_llm_reply(
    client: AsyncClient,
    monkeypatch,
    bypass_chat_router,
) -> None:
    async def fake_complete(
        self: ChatCompletionClient,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        assert messages[-1]["role"] == "user"
        assert "Hello" in messages[-1]["content"]
        assert model is not None  # responder uses CHAT_RESPONDER_MODEL
        return "Synthetic assistant reply"

    monkeypatch.setattr(ChatCompletionClient, "complete", fake_complete)

    user_payload = {
        "email": "chat@example.com",
        "password": "strong-password",
        "full_name": "Chat User",
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
    headers = {"Authorization": f"Bearer {token}"}

    session_response = await client.post("/api/v1/chat/sessions", json={}, headers=headers)
    assert session_response.status_code == 201
    session_id = session_response.json()["id"]

    msg_response = await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        headers=headers,
        json={"content": "Hello from user"},
    )
    assert msg_response.status_code == 200
    messages = msg_response.json()
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "Hello from user"
    assert messages[0]["id"]
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] == "Synthetic assistant reply"
    assert messages[1]["id"]


async def test_send_message_fails_when_llm_not_configured(
    client: AsyncClient,
    monkeypatch,
    bypass_chat_router,
) -> None:
    async def fail_complete(
        self: ChatCompletionClient,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        raise ServiceUnavailableException("LLM unavailable")

    monkeypatch.setattr(ChatCompletionClient, "complete", fail_complete)

    user_payload = {
        "email": "chat2@example.com",
        "password": "strong-password",
        "full_name": "Chat User",
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
    headers = {"Authorization": f"Bearer {token}"}

    session_response = await client.post("/api/v1/chat/sessions", json={}, headers=headers)
    session_id = session_response.json()["id"]

    msg_response = await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        headers=headers,
        json={"content": "Hi"},
    )
    assert msg_response.status_code == 503


async def test_stream_message_persists_and_emits_sse(
    client: AsyncClient,
    monkeypatch,
    bypass_chat_router,
) -> None:
    async def fake_stream(
        self: ChatCompletionClient,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float | None = None,
    ):
        assert model is not None
        yield "Syn"
        yield "thetic stream"

    monkeypatch.setattr(ChatCompletionClient, "stream_complete", fake_stream)

    user_payload = {
        "email": "stream@example.com",
        "password": "strong-password",
        "full_name": "Stream User",
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
    headers = {"Authorization": f"Bearer {token}"}

    session_response = await client.post("/api/v1/chat/sessions", json={}, headers=headers)
    session_id = session_response.json()["id"]

    async with client.stream(
        "POST",
        f"/api/v1/chat/sessions/{session_id}/messages/stream",
        headers=headers,
        json={"content": "Hello stream"},
    ) as stream_response:
        assert stream_response.status_code == 200
        assert stream_response.headers.get("content-type", "").startswith("text/event-stream")
        raw = await stream_response.aread()

    events: list[dict] = []
    for block in raw.decode("utf-8").split("\n\n"):
        block = block.strip()
        if not block.startswith("data: "):
            continue
        events.append(json.loads(block.removeprefix("data: ").strip()))

    assert events[0]["event"] == "loading"
    assert events[0]["message"] == "Thinking..."

    delta_text = "".join(e["text"] for e in events if e.get("event") == "delta")
    assert delta_text == "Synthetic stream"

    done = next(e for e in events if e.get("event") == "done")
    assert done["message"] == "Synthetic stream"

    detail = await client.get(f"/api/v1/chat/sessions/{session_id}", headers=headers)
    assert detail.status_code == 200
    messages = detail.json()["messages"]
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "Hello stream"
    assert messages[1]["role"] == "assistant"
    assert messages[1]["content"] == "Synthetic stream"


async def test_delete_session_hides_from_list(
    client: AsyncClient,
    monkeypatch,
    bypass_chat_router,
) -> None:
    user_payload = {
        "email": "delete-session@example.com",
        "password": "strong-password",
        "full_name": "Delete User",
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
    headers = {"Authorization": f"Bearer {token}"}

    keep = await client.post("/api/v1/chat/sessions", json={}, headers=headers)
    remove = await client.post("/api/v1/chat/sessions", json={}, headers=headers)
    keep_id = keep.json()["id"]
    remove_id = remove.json()["id"]

    del_resp = await client.delete(f"/api/v1/chat/sessions/{remove_id}", headers=headers)
    assert del_resp.status_code == 204

    listed = await client.get("/api/v1/chat/sessions", headers=headers)
    assert listed.status_code == 200
    ids = {s["id"] for s in listed.json()}
    assert keep_id in ids
    assert remove_id not in ids

    get_removed = await client.get(f"/api/v1/chat/sessions/{remove_id}", headers=headers)
    assert get_removed.status_code == 404

    del_again = await client.delete(f"/api/v1/chat/sessions/{remove_id}", headers=headers)
    assert del_again.status_code == 204


async def test_realtime_session_mint_requires_auth(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/chat/sessions/507f1f77bcf86cd799439011/realtime/session")
    assert resp.status_code in (401, 403)


async def test_realtime_session_mint_404_for_unknown_session(
    client: AsyncClient,
    monkeypatch,
    bypass_chat_router,
) -> None:
    user_payload = {
        "email": "voice404@example.com",
        "password": "strong-password",
        "full_name": "Voice User",
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
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/v1/chat/sessions/507f1f77bcf86cd799439011/realtime/session",
        headers=headers,
    )
    assert resp.status_code == 404


async def test_realtime_session_mint_returns_ephemeral_payload(
    client: AsyncClient,
    monkeypatch,
    bypass_chat_router,
) -> None:
    async def fake_mint(*, chat_session_id: str) -> RealtimeSessionMintResponse:
        return RealtimeSessionMintResponse(
            chat_session_id=chat_session_id,
            openai_session_id="sess_fake",
            client_secret=RealtimeClientSecret(value="ek_fake", expires_at=2000000000),
            model="gpt-realtime",
        )

    monkeypatch.setattr(
        "app.api.v1.endpoints.chat.mint_openai_realtime_session",
        fake_mint,
    )

    user_payload = {
        "email": "voicemint@example.com",
        "password": "strong-password",
        "full_name": "Mint User",
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
    headers = {"Authorization": f"Bearer {token}"}

    session_response = await client.post("/api/v1/chat/sessions", json={}, headers=headers)
    session_id = session_response.json()["id"]

    resp = await client.post(
        f"/api/v1/chat/sessions/{session_id}/realtime/session",
        headers=headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["chat_session_id"] == session_id
    assert data["openai_session_id"] == "sess_fake"
    assert data["client_secret"]["value"] == "ek_fake"
    assert data["client_secret"]["expires_at"] == 2000000000
    assert data["model"] == "gpt-realtime"


async def test_realtime_lookup_documentation_tool(
    client: AsyncClient,
    monkeypatch,
    bypass_chat_router,
) -> None:
    async def fake_passages(
        self: ChatService,
        user_id: str,
        query: str,
        *,
        session_id: str | None = None,
        top_k: int | None = None,
    ) -> list[dict]:
        assert "eligibility" in query
        return [{"text": "You must apply in person.", "title": "Guide", "url": "https://x.example"}]

    monkeypatch.setattr(ChatService, "lookup_indexed_passages", fake_passages)

    user_payload = {
        "email": "voicetool@example.com",
        "password": "strong-password",
        "full_name": "Tool User",
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
    headers = {"Authorization": f"Bearer {token}"}

    session_response = await client.post("/api/v1/chat/sessions", json={}, headers=headers)
    session_id = session_response.json()["id"]

    resp = await client.post(
        f"/api/v1/chat/sessions/{session_id}/realtime/tools/lookup_documentation",
        headers=headers,
        json={"query": "eligibility steps"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "result" in body
    assert "[1]" in body["result"]
    assert "You must apply in person." in body["result"]


async def test_voice_commit_persists_messages(
    client: AsyncClient,
    monkeypatch,
    bypass_chat_router,
) -> None:
    user_payload = {
        "email": "voicecommit@example.com",
        "password": "strong-password",
        "full_name": "Commit User",
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
    headers = {"Authorization": f"Bearer {token}"}

    session_response = await client.post("/api/v1/chat/sessions", json={}, headers=headers)
    session_id = session_response.json()["id"]

    resp = await client.post(
        f"/api/v1/chat/sessions/{session_id}/voice/commit",
        headers=headers,
        json={
            "user_transcript": "What is DDS?",
            "assistant_transcript": "DDS is the California Department of Developmental Services.",
            "openai_response_id": "resp_abc123",
        },
    )
    assert resp.status_code == 200
    messages = resp.json()
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "What is DDS?"
    assert messages[0]["source"] == "voice"
    assert messages[1]["role"] == "assistant"
    assert "Developmental Services" in messages[1]["content"]
    assert messages[1]["source"] == "voice"

    detail = await client.get(f"/api/v1/chat/sessions/{session_id}", headers=headers)
    assert detail.status_code == 200
    thread = detail.json()["messages"]
    assert len(thread) == 2
    assert thread[0]["source"] == "voice"
    assert thread[1]["source"] == "voice"


async def _register_and_login(client: AsyncClient, email: str) -> dict[str, str]:
    user_payload = {
        "email": email,
        "password": "strong-password",
        "full_name": "Voice Tester",
    }
    await client.post("/api/v1/auth/register", json=user_payload)
    login_response = await client.post(
        "/api/v1/auth/login",
        data={"username": user_payload["email"], "password": user_payload["password"]},
    )
    token = login_response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def test_voice_commit_is_idempotent_for_same_client_turn_id(
    client: AsyncClient,
    bypass_chat_router,
) -> None:
    headers = await _register_and_login(client, "voiceidem@example.com")
    session_response = await client.post("/api/v1/chat/sessions", json={}, headers=headers)
    session_id = session_response.json()["id"]

    body = {
        "user_transcript": "What is DDS?",
        "assistant_transcript": "DDS is the California Department of Developmental Services.",
        "client_turn_id": "turn-1",
        "openai_response_id": "resp_turn_1",
    }

    first = await client.post(
        f"/api/v1/chat/sessions/{session_id}/voice/commit",
        headers=headers,
        json=body,
    )
    second = await client.post(
        f"/api/v1/chat/sessions/{session_id}/voice/commit",
        headers=headers,
        json=body,
    )
    assert first.status_code == 200
    assert second.status_code == 200
    first_messages = first.json()
    second_messages = second.json()
    assert len(first_messages) == 2
    assert len(second_messages) == 2
    assert [m["id"] for m in first_messages] == [m["id"] for m in second_messages]
    assert [m["role"] for m in second_messages] == ["user", "assistant"]
    assert [m["source"] for m in second_messages] == ["voice", "voice"]
    assert [m["content"] for m in second_messages] == [
        "What is DDS?",
        "DDS is the California Department of Developmental Services.",
    ]

    detail = await client.get(f"/api/v1/chat/sessions/{session_id}", headers=headers)
    assert len(detail.json()["messages"]) == 2


async def test_voice_followup_turns_stay_in_same_chat_session(
    client: AsyncClient,
    bypass_chat_router,
) -> None:
    headers = await _register_and_login(client, "voicefollowup@example.com")
    session_response = await client.post("/api/v1/chat/sessions", json={}, headers=headers)
    session_id = session_response.json()["id"]

    first = await client.post(
        f"/api/v1/chat/sessions/{session_id}/voice/commit",
        headers=headers,
        json={
            "user_transcript": "Initial question",
            "assistant_transcript": "Initial answer",
            "client_turn_id": "voice-turn-1",
            "openai_response_id": "resp_1",
        },
    )
    followup = await client.post(
        f"/api/v1/chat/sessions/{session_id}/voice/commit",
        headers=headers,
        json={
            "user_transcript": "Follow-up question",
            "assistant_transcript": "Follow-up answer",
            "client_turn_id": "voice-turn-2",
            "openai_response_id": "resp_2",
        },
    )
    assert first.status_code == 200, first.text
    assert followup.status_code == 200, followup.text

    detail = await client.get(f"/api/v1/chat/sessions/{session_id}", headers=headers)
    thread = detail.json()["messages"]
    assert [m["content"] for m in thread] == [
        "Initial question",
        "Initial answer",
        "Follow-up question",
        "Follow-up answer",
    ]
    assert {m["session_id"] for m in thread} == {session_id}
    assert [m["role"] for m in thread] == ["user", "assistant", "user", "assistant"]
    assert all(m["source"] == "voice" for m in thread)


async def test_voice_commit_returns_5xx_and_persists_nothing_on_repo_failure(
    client: AsyncClient,
    monkeypatch,
    bypass_chat_router,
) -> None:
    """Failure inside ``create_voice_turn`` must not leave any rows behind."""
    headers = await _register_and_login(client, "voicerb@example.com")
    session_response = await client.post("/api/v1/chat/sessions", json={}, headers=headers)
    session_id = session_response.json()["id"]

    async def flaky_voice_turn(self, user_msg, assistant_msg):
        raise ServiceUnavailableException("simulated mongo failure")

    monkeypatch.setattr(ChatRepository, "create_voice_turn", flaky_voice_turn)

    resp = await client.post(
        f"/api/v1/chat/sessions/{session_id}/voice/commit",
        headers=headers,
        json={"user_transcript": "Hi", "assistant_transcript": "Hello there"},
    )
    assert resp.status_code == 503

    monkeypatch.undo()
    detail = await client.get(f"/api/v1/chat/sessions/{session_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["messages"] == []


async def test_voice_commit_recovers_orphan_from_previous_failed_attempt(
    client: AsyncClient,
    bypass_chat_router,
) -> None:
    """A user-only row left behind by a previous crash is cleaned up on retry."""
    from beanie import PydanticObjectId

    from app.modules.chat.model import ChatMessage as ChatMessageModel
    from app.modules.chat.model import MessageRole, MessageSource

    headers = await _register_and_login(client, "voiceorphan@example.com")
    session_response = await client.post("/api/v1/chat/sessions", json={}, headers=headers)
    session_id = session_response.json()["id"]

    me = await client.get("/api/v1/users/me", headers=headers)
    assert me.status_code == 200
    user_id = me.json()["id"]

    orphan = ChatMessageModel(
        session_id=PydanticObjectId(session_id),
        user_id=PydanticObjectId(user_id),
        role=MessageRole.USER,
        content="orphan user transcript",
        source=MessageSource.VOICE,
        client_turn_id="orphan-turn",
    )
    await orphan.insert()

    resp = await client.post(
        f"/api/v1/chat/sessions/{session_id}/voice/commit",
        headers=headers,
        json={
            "user_transcript": "fresh user transcript",
            "assistant_transcript": "fresh assistant transcript",
            "client_turn_id": "orphan-turn",
        },
    )
    assert resp.status_code == 200
    pair = resp.json()
    assert pair[0]["content"] == "fresh user transcript"
    assert pair[1]["content"] == "fresh assistant transcript"

    detail = await client.get(f"/api/v1/chat/sessions/{session_id}", headers=headers)
    thread = detail.json()["messages"]
    assert len(thread) == 2, thread
    assert {m["content"] for m in thread} == {
        "fresh user transcript",
        "fresh assistant transcript",
    }


async def test_voice_commit_concurrent_retries_yield_a_single_pair(
    client: AsyncClient,
    bypass_chat_router,
) -> None:
    """The unique index + race-resolution path must prevent duplicate rows.

    Two simultaneous commits with the same ``client_turn_id`` are fired in parallel;
    afterwards exactly one ``[user, assistant]`` pair must be stored.
    """
    import asyncio

    headers = await _register_and_login(client, "voicerace@example.com")
    session_response = await client.post("/api/v1/chat/sessions", json={}, headers=headers)
    session_id = session_response.json()["id"]

    body = {
        "user_transcript": "Race condition test",
        "assistant_transcript": "Idempotent reply",
        "client_turn_id": "race-1",
    }

    results = await asyncio.gather(
        *[
            client.post(
                f"/api/v1/chat/sessions/{session_id}/voice/commit",
                headers=headers,
                json=body,
            )
            for _ in range(5)
        ]
    )
    for r in results:
        assert r.status_code == 200, r.text
    seen_ids = {tuple(m["id"] for m in r.json()) for r in results}
    assert len(seen_ids) == 1, f"expected one canonical pair, got {seen_ids}"

    detail = await client.get(f"/api/v1/chat/sessions/{session_id}", headers=headers)
    assert len(detail.json()["messages"]) == 2


async def test_voice_lookup_tool_uses_voice_specific_top_k_and_excerpt_budget(
    client: AsyncClient,
    monkeypatch,
    bypass_chat_router,
) -> None:
    """Voice tool route must request fewer passages and trim excerpts more aggressively."""
    captured: dict[str, object] = {}

    async def fake_passages(
        self: ChatService,
        user_id: str,
        query: str,
        *,
        session_id: str | None = None,
        top_k: int | None = None,
    ) -> list[dict]:
        captured["top_k"] = top_k
        long_text = "A" * 6000
        return [{"text": long_text, "title": "Doc", "url": "https://x.example"}]

    monkeypatch.setattr(ChatService, "lookup_indexed_passages", fake_passages)

    headers = await _register_and_login(client, "voicebudget@example.com")
    session_response = await client.post("/api/v1/chat/sessions", json={}, headers=headers)
    session_id = session_response.json()["id"]

    resp = await client.post(
        f"/api/v1/chat/sessions/{session_id}/realtime/tools/lookup_documentation",
        headers=headers,
        json={"query": "anything"},
    )
    assert resp.status_code == 200
    result = resp.json()["result"]

    from app.core.config import settings as live_settings

    assert captured["top_k"] == live_settings.CHAT_RAG_VOICE_TOP_K
    # Excerpt must be capped to the voice budget plus the truncation marker.
    assert "A" * (live_settings.CHAT_RAG_VOICE_EXCERPT_CHARS + 1) not in result
    assert "…" in result


async def test_voice_commit_rejects_overlong_transcript(
    client: AsyncClient,
    bypass_chat_router,
) -> None:
    headers = await _register_and_login(client, "voicelen@example.com")
    session_response = await client.post("/api/v1/chat/sessions", json={}, headers=headers)
    session_id = session_response.json()["id"]

    resp = await client.post(
        f"/api/v1/chat/sessions/{session_id}/voice/commit",
        headers=headers,
        json={
            "user_transcript": "x" * 8001,
            "assistant_transcript": "ok",
        },
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["detail"] == "Validation error"
    assert any("user_transcript" in (err.get("loc") or []) for err in body["errors"])


def test_realtime_session_payload_includes_response_token_budget_and_vad() -> None:
    """Without these knobs Realtime cuts audio off mid-sentence; this guards regressions."""
    payload = build_session_payload("507f1f77bcf86cd799439011")
    session = payload["session"]
    audio = session["audio"]

    # GA sessions request audio output; OpenAI still emits the audio transcript events
    # the SPA uses to render assistant text.
    assert session["output_modalities"] == ["audio"]

    assert session["max_output_tokens"] in {"inf", *range(1, 4097)}

    vad = audio["input"]["turn_detection"]
    assert vad == {
        "type": "server_vad",
        "interrupt_response": False,
        "threshold": 0.78,
        "prefix_padding_ms": 350,
        "silence_duration_ms": 650,
    }

    instructions = session["instructions"]
    assert "Use the same answer structure as text chat" in instructions
    assert "## Where to learn more" in instructions
    assert "Use only actual DDS website URLs" in instructions
    assert "user would like links to DDS resources or any other information" in instructions
    assert "put them only in the final ## Where to learn more section" in instructions
    assert "Document/filename" not in instructions
    assert "do not use Markdown section headings" not in instructions
    assert "Briefly cite" not in instructions

    tools = session["tools"]
    assert len(tools) == 1 and tools[0]["name"] == "lookup_documentation"


async def test_realtime_session_mint_sends_token_budget_to_openai(
    client: AsyncClient,
    monkeypatch,
    bypass_chat_router,
) -> None:
    """End-to-end: minting the session POSTs `max_output_tokens` to OpenAI."""
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200
        is_success = True

        def json(self) -> dict[str, object]:
            return {
                "value": "ek_fake",
                "expires_at": 2000000000,
                "session": {
                    "id": "sess_fake",
                    "model": "gpt-realtime",
                    "audio": {
                        "input": {
                            "turn_detection": {
                                "type": "server_vad",
                                "interrupt_response": False,
                                "threshold": 0.78,
                                "prefix_padding_ms": 350,
                                "silence_duration_ms": 650,
                            }
                        },
                        "output": {"voice": "alloy"},
                    },
                },
            }

    class FakeAsyncClient:
        def __init__(self, *_, **__) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(self, url: str, *, json: dict, headers: dict) -> FakeResponse:
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr(
        "app.modules.chat.realtime_session.httpx.AsyncClient",
        FakeAsyncClient,
    )
    monkeypatch.setattr(
        "app.modules.chat.realtime_session.settings.OPENAI_API_KEY",
        "test-key",
        raising=False,
    )

    headers = await _register_and_login(client, "voiceknobs@example.com")
    session_response = await client.post("/api/v1/chat/sessions", json={}, headers=headers)
    session_id = session_response.json()["id"]

    resp = await client.post(
        f"/api/v1/chat/sessions/{session_id}/realtime/session",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    sent = captured["json"]
    assert isinstance(sent, dict)
    session = sent["session"]
    assert isinstance(session, dict)
    url = captured["url"]
    assert isinstance(url, str)
    assert url.endswith("/realtime/client_secrets")
    assert "max_output_tokens" in session
    assert session["output_modalities"] == ["audio"]
    audio = session["audio"]
    assert isinstance(audio, dict)
    audio_input = audio["input"]
    assert isinstance(audio_input, dict)
    vad = audio_input["turn_detection"]
    assert isinstance(vad, dict)
    assert vad == {
        "type": "server_vad",
        "interrupt_response": False,
        "threshold": 0.78,
        "prefix_padding_ms": 350,
        "silence_duration_ms": 650,
    }


async def test_realtime_session_mint_logs_openai_vad_normalization(
    monkeypatch,
    caplog,
) -> None:
    class FakeResponse:
        status_code = 200
        is_success = True

        def json(self) -> dict[str, object]:
            return {
                "value": "ek_fake",
                "expires_at": 2000000000,
                "session": {
                    "id": "sess_normalized",
                    "model": "gpt-realtime",
                    "audio": {
                        "input": {
                            "turn_detection": {
                                "type": "server_vad",
                                "interrupt_response": True,
                                "threshold": 0.5,
                                "prefix_padding_ms": 300,
                                "silence_duration_ms": 600,
                            }
                        },
                        "output": {"voice": "alloy"},
                    },
                },
            }

    class FakeAsyncClient:
        def __init__(self, *_, **__) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(self, url: str, *, json: dict, headers: dict) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(
        "app.modules.chat.realtime_session.httpx.AsyncClient",
        FakeAsyncClient,
    )
    monkeypatch.setattr(
        "app.modules.chat.realtime_session.settings.OPENAI_API_KEY",
        "test-key",
        raising=False,
    )
    caplog.set_level(logging.WARNING, logger="app.modules.chat.realtime_session")

    await mint_openai_realtime_session(chat_session_id="507f1f77bcf86cd799439011")

    mismatch_records = [
        r for r in caplog.records if r.message == "chat.realtime.session.minted_vad_mismatch"
    ]
    assert mismatch_records
    assert mismatch_records[0].returned_turn_detection["interrupt_response"] is True
    assert mismatch_records[0].returned_session_config["audio"]["input"]["turn_detection"][
        "interrupt_response"
    ] is True


async def test_text_messages_are_tagged_source_text(
    client: AsyncClient,
    monkeypatch,
    bypass_chat_router,
) -> None:
    async def fake_complete(
        self: ChatCompletionClient,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        return "Typed assistant reply"

    monkeypatch.setattr(ChatCompletionClient, "complete", fake_complete)

    headers = await _register_and_login(client, "texttagged@example.com")
    session_response = await client.post("/api/v1/chat/sessions", json={}, headers=headers)
    session_id = session_response.json()["id"]

    resp = await client.post(
        f"/api/v1/chat/sessions/{session_id}/messages",
        headers=headers,
        json={"content": "Hello"},
    )
    assert resp.status_code == 200
    msgs = resp.json()
    assert msgs[0]["source"] == "text"
    assert msgs[1]["source"] == "text"


async def test_invalid_session_id_returns_422_not_404(client: AsyncClient) -> None:
    """Malformed session ids must fail at the routing layer, not as 404 from Mongo."""
    headers = await _register_and_login(client, "voiceid422@example.com")
    for bad in ("not-an-objectid", "1234", "G" * 24):
        resp = await client.get(f"/api/v1/chat/sessions/{bad}", headers=headers)
        assert resp.status_code == 422, (bad, resp.text)


async def test_voice_commit_title_is_generated_in_background(
    client: AsyncClient,
    monkeypatch,
    bypass_chat_router,
) -> None:
    """Voice commit must NOT block on title generation but DOES eventually persist a title."""
    import asyncio

    async def fake_title(llm, *, user_text, assistant_text):
        await asyncio.sleep(0)  # let the loop schedule us properly
        return "Generated Voice Title"

    monkeypatch.setattr("app.modules.chat.service.suggest_session_title", fake_title)

    headers = await _register_and_login(client, "voicetitle@example.com")
    session_response = await client.post("/api/v1/chat/sessions", json={}, headers=headers)
    session_id = session_response.json()["id"]

    resp = await client.post(
        f"/api/v1/chat/sessions/{session_id}/voice/commit",
        headers=headers,
        json={
            "user_transcript": "Hello",
            "assistant_transcript": "Hi there",
        },
    )
    assert resp.status_code == 200
    # Voice path returns immediately without the title header — the SPA contract is
    # that the title appears on the next GET, not on the commit response.
    assert "X-Chat-Session-Title" not in resp.headers

    title: str | None = None
    for _ in range(50):
        detail = await client.get(
            f"/api/v1/chat/sessions/{session_id}",
            headers=headers,
        )
        title = detail.json().get("title")
        if title:
            break
        await asyncio.sleep(0.02)

    assert title == "Generated Voice Title", title


async def test_realtime_session_mint_retries_transient_then_succeeds(
    client: AsyncClient,
    monkeypatch,
    bypass_chat_router,
) -> None:
    """Transient 503 from OpenAI is retried until success."""
    attempts: list[int] = []

    class StubResponse:
        def __init__(self, status_code: int, body: dict[str, object]) -> None:
            self.status_code = status_code
            self._body = body
            self.is_success = 200 <= status_code < 300

        def json(self) -> dict[str, object]:
            return self._body

    class FakeAsyncClient:
        def __init__(self, *_, **__) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(self, url, *, json, headers) -> StubResponse:
            attempts.append(len(attempts) + 1)
            if len(attempts) == 1:
                return StubResponse(503, {"error": {"message": "transient"}})
            return StubResponse(
                200,
                {
                    "value": "ek_ok",
                    "expires_at": 2000000000,
                    "session": {
                        "id": "sess_ok",
                        "model": "gpt-realtime",
                    },
                },
            )

    monkeypatch.setattr(
        "app.modules.chat.realtime_session.httpx.AsyncClient", FakeAsyncClient
    )
    monkeypatch.setattr(
        "app.modules.chat.realtime_session.settings.OPENAI_API_KEY",
        "test-key",
        raising=False,
    )
    monkeypatch.setattr(
        "app.modules.chat.realtime_session.settings.OPENAI_REALTIME_MINT_BACKOFF_BASE_SECONDS",
        0.0,
        raising=False,
    )

    headers = await _register_and_login(client, "voiceretry@example.com")
    session_response = await client.post("/api/v1/chat/sessions", json={}, headers=headers)
    session_id = session_response.json()["id"]

    resp = await client.post(
        f"/api/v1/chat/sessions/{session_id}/realtime/session",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert len(attempts) >= 2


async def test_realtime_session_mint_does_not_retry_4xx(
    client: AsyncClient,
    monkeypatch,
    bypass_chat_router,
) -> None:
    """Auth/config errors (4xx) must fail fast — retrying wastes latency."""
    attempts: list[int] = []

    class StubResponse:
        status_code = 400
        is_success = False

        def json(self) -> dict[str, object]:
            return {"error": {"message": "bad request"}}

    class FakeAsyncClient:
        def __init__(self, *_, **__) -> None:
            pass

        async def __aenter__(self) -> "FakeAsyncClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(self, url, *, json, headers) -> StubResponse:
            attempts.append(1)
            return StubResponse()

    monkeypatch.setattr(
        "app.modules.chat.realtime_session.httpx.AsyncClient", FakeAsyncClient
    )
    monkeypatch.setattr(
        "app.modules.chat.realtime_session.settings.OPENAI_API_KEY",
        "test-key",
        raising=False,
    )

    headers = await _register_and_login(client, "voice4xx@example.com")
    session_response = await client.post("/api/v1/chat/sessions", json={}, headers=headers)
    session_id = session_response.json()["id"]

    resp = await client.post(
        f"/api/v1/chat/sessions/{session_id}/realtime/session",
        headers=headers,
    )
    assert resp.status_code == 400
    assert attempts == [1]


async def test_mint_rate_limit_returns_429(
    client: AsyncClient,
    monkeypatch,
    bypass_chat_router,
) -> None:
    """Once the user crosses the configured per-window limit, mint returns 429."""

    async def fake_mint(*, chat_session_id: str) -> RealtimeSessionMintResponse:
        return RealtimeSessionMintResponse(
            chat_session_id=chat_session_id,
            openai_session_id="sess_rl",
            client_secret=RealtimeClientSecret(value="ek", expires_at=2000000000),
            model="gpt-realtime",
        )

    monkeypatch.setattr("app.api.v1.endpoints.chat.mint_openai_realtime_session", fake_mint)
    monkeypatch.setattr(
        "app.api.v1.endpoints.chat.settings.OPENAI_REALTIME_MINT_LIMIT_PER_USER",
        2,
        raising=False,
    )
    monkeypatch.setattr(
        "app.api.v1.endpoints.chat.settings.OPENAI_REALTIME_MINT_LIMIT_WINDOW_SECONDS",
        60,
        raising=False,
    )

    headers = await _register_and_login(client, "voicerl@example.com")
    session_response = await client.post("/api/v1/chat/sessions", json={}, headers=headers)
    session_id = session_response.json()["id"]

    for _ in range(2):
        ok = await client.post(
            f"/api/v1/chat/sessions/{session_id}/realtime/session",
            headers=headers,
        )
        assert ok.status_code == 200, ok.text

    blocked = await client.post(
        f"/api/v1/chat/sessions/{session_id}/realtime/session",
        headers=headers,
    )
    assert blocked.status_code == 429
    detail = blocked.json()["detail"].lower()
    assert "rate" in detail or "too many" in detail


async def test_voice_audit_rows_record_mint_and_commit(
    client: AsyncClient,
    monkeypatch,
    bypass_chat_router,
) -> None:
    """Mint + commit each write an audit row that survives the request."""

    async def fake_mint(*, chat_session_id: str) -> RealtimeSessionMintResponse:
        return RealtimeSessionMintResponse(
            chat_session_id=chat_session_id,
            openai_session_id="sess_audit",
            client_secret=RealtimeClientSecret(value="ek", expires_at=2000000000),
            model="gpt-realtime",
        )

    monkeypatch.setattr("app.api.v1.endpoints.chat.mint_openai_realtime_session", fake_mint)

    headers = await _register_and_login(client, "voiceaudit@example.com")
    session_response = await client.post("/api/v1/chat/sessions", json={}, headers=headers)
    session_id = session_response.json()["id"]

    mint_resp = await client.post(
        f"/api/v1/chat/sessions/{session_id}/realtime/session",
        headers=headers,
    )
    assert mint_resp.status_code == 200

    commit_resp = await client.post(
        f"/api/v1/chat/sessions/{session_id}/voice/commit",
        headers=headers,
        json={
            "user_transcript": "Audit me",
            "assistant_transcript": "OK",
            "client_turn_id": "audit-1",
        },
    )
    assert commit_resp.status_code == 200

    events = await RealtimeSessionEvent.find_all().to_list()
    kinds = {e.event for e in events}
    assert VoiceEventType.MINT in kinds
    assert VoiceEventType.COMMIT in kinds

    mint_event = next(e for e in events if e.event == VoiceEventType.MINT)
    assert mint_event.openai_session_id == "sess_audit"
    assert mint_event.elapsed_ms is not None and mint_event.elapsed_ms >= 0


async def test_embedding_cache_avoids_duplicate_provider_calls(
    client: AsyncClient,
    monkeypatch,
    bypass_chat_router,
) -> None:
    """The second tool lookup with the same query must reuse the cached embedding."""
    from app.modules.embeddings.openai_embeddings import OpenAIEmbeddingProvider
    from app.modules.vector_store.qdrant import QdrantVectorStore

    await get_query_embedding_cache().clear()

    embed_calls: list[str] = []

    async def fake_embed(self, texts):
        embed_calls.extend(texts)
        return [[0.0] * 8 for _ in texts]

    async def fake_search(
        self,
        *,
        user_id,
        query_vector,
        limit,
    ):
        return [(0.9, {"text": "hit", "title": "T", "url": "https://x.example"})]

    monkeypatch.setattr(OpenAIEmbeddingProvider, "embed_documents", fake_embed)
    monkeypatch.setattr(QdrantVectorStore, "search_user_chunks", fake_search)

    headers = await _register_and_login(client, "voicecache@example.com")
    session_response = await client.post("/api/v1/chat/sessions", json={}, headers=headers)
    session_id = session_response.json()["id"]

    for _ in range(3):
        resp = await client.post(
            f"/api/v1/chat/sessions/{session_id}/realtime/tools/lookup_documentation",
            headers=headers,
            json={"query": "repeated voice query"},
        )
        assert resp.status_code == 200

    assert len(embed_calls) == 1, embed_calls
