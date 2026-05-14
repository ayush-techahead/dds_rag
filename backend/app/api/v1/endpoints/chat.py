import time
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response, status
from fastapi.responses import StreamingResponse

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.exceptions import (
    AppException,
    BadRequestException,
    ServiceUnavailableException,
    TooManyRequestsException,
)
from app.modules.chat.audit import (
    VoiceEventType,
    count_recent_mints_for_user,
    record_voice_event,
)
from app.modules.chat.realtime_session import mint_openai_realtime_session
from app.modules.chat.schemas import (
    ChatMessageCreate,
    ChatMessageResponse,
    ChatSessionCreate,
    ChatSessionDetailResponse,
    ChatSessionResponse,
    LookupDocumentationRequest,
    LookupDocumentationResponse,
    RealtimeSessionMintResponse,
    VoiceCommitRequest,
)
from app.modules.chat.service import ChatService
from app.modules.users.model import User

router = APIRouter()

# Mongo ObjectIds are 24-char hex strings. Rejecting anything else at the FastAPI
# routing layer returns 422 instead of bouncing off a Mongo lookup as 404; this is
# both faster (no DB roundtrip) and lets the SPA distinguish bug from legitimate
# "session you asked for doesn't exist".
_SESSION_ID_PATTERN = r"^[0-9a-fA-F]{24}$"
SessionIdPath = Annotated[
    str,
    Path(
        pattern=_SESSION_ID_PATTERN,
        description="24-char hex Mongo ObjectId.",
        examples=["507f1f77bcf86cd799439011"],
    ),
]

_CHAT_SSE_RESPONSES: dict[int | str, dict] = {
    200: {
        "description": (
            "Server-Sent Events (`text/event-stream`). Each event is `data: ` + one JSON object + "
            "two newlines. Events: `loading` (optional status), `delta` (`text` token), `done` "
            "(`message` = full assistant reply; optional `session_title` after the first reply "
            "when the session was auto-titled), or `error` (`detail`)."
        ),
        "content": {
            "text/event-stream": {
                "schema": {"type": "string"},
                "example": (
                    'data: {"event":"loading","message":"Thinking..."}\n\n'
                    'data: {"event":"delta","text":"Hi"}\n\n'
                    'data: {"event":"done","message":"Hi there","session_title":"Greeting"}\n\n'
                ),
            }
        },
    }
}


@router.post(
    "/sessions/{session_id}/messages/stream",
    responses=_CHAT_SSE_RESPONSES,
    response_class=StreamingResponse,
)
async def create_message_stream(
    session_id: SessionIdPath,
    payload: ChatMessageCreate,
    current_user: Annotated[User, Depends(get_current_user)],
) -> StreamingResponse:
    service = ChatService()
    session = await service.require_chat_session(str(current_user.id), session_id)

    async def byte_stream():
        async for event in service.stream_message_events(session, str(current_user.id), payload):
            yield event.encode("utf-8")

    return StreamingResponse(
        byte_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/sessions", response_model=ChatSessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    payload: ChatSessionCreate,
    current_user: Annotated[User, Depends(get_current_user)],
) -> ChatSessionResponse:
    return await ChatService().create_session(str(current_user.id), payload)


@router.get("/sessions", response_model=list[ChatSessionResponse])
async def list_sessions(
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[ChatSessionResponse]:
    return await ChatService().list_sessions(str(current_user.id))


@router.get("/sessions/{session_id}", response_model=ChatSessionDetailResponse)
async def get_session(
    session_id: SessionIdPath,
    current_user: Annotated[User, Depends(get_current_user)],
) -> ChatSessionDetailResponse:
    return await ChatService().get_session(str(current_user.id), session_id)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: SessionIdPath,
    current_user: Annotated[User, Depends(get_current_user)],
) -> None:
    await ChatService().delete_session(str(current_user.id), session_id)


@router.post(
    "/sessions/{session_id}/realtime/session",
    response_model=RealtimeSessionMintResponse,
    responses={
        429: {"description": "Per-user mint rate limit exceeded."},
        503: {"description": "OpenAI Realtime is unreachable after retries."},
    },
)
async def create_realtime_session(
    session_id: SessionIdPath,
    current_user: Annotated[User, Depends(get_current_user)],
) -> RealtimeSessionMintResponse:
    """Mint an ephemeral OpenAI Realtime session for the browser.

    Retries transient OpenAI 5xx errors automatically. Enforces a per-user
    sliding-window rate limit driven by the audit collection.
    """
    user_id = str(current_user.id)
    service = ChatService()
    await service.require_chat_session(user_id, session_id)

    limit = settings.OPENAI_REALTIME_MINT_LIMIT_PER_USER
    window = settings.OPENAI_REALTIME_MINT_LIMIT_WINDOW_SECONDS
    if limit > 0 and window > 0:
        recent = await count_recent_mints_for_user(user_id, window_seconds=window)
        if recent >= limit:
            await record_voice_event(
                user_id=user_id,
                session_id=session_id,
                event=VoiceEventType.MINT_FAILED,
                status_code=429,
                detail=f"rate_limit:{recent}/{limit} in {window}s",
            )
            raise TooManyRequestsException(
                f"Too many Realtime session mints: {recent}/{limit} in {window}s. "
                "Slow down or open fewer concurrent voice sessions.",
            )

    t0 = time.perf_counter()
    try:
        result = await mint_openai_realtime_session(chat_session_id=session_id, user_id=user_id)
    except AppException as exc:
        await record_voice_event(
            user_id=user_id,
            session_id=session_id,
            event=VoiceEventType.MINT_FAILED,
            elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
            status_code=503 if isinstance(exc, ServiceUnavailableException) else 400,
            detail=exc.message,
        )
        raise
    await record_voice_event(
        user_id=user_id,
        session_id=session_id,
        event=VoiceEventType.MINT,
        elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
        status_code=200,
        openai_session_id=result.openai_session_id,
    )
    return result


@router.post(
    "/sessions/{session_id}/realtime/tools/lookup_documentation",
    response_model=LookupDocumentationResponse,
)
async def realtime_lookup_documentation(
    session_id: SessionIdPath,
    payload: LookupDocumentationRequest,
    current_user: Annotated[User, Depends(get_current_user)],
) -> LookupDocumentationResponse:
    user_id = str(current_user.id)
    service = ChatService()
    await service.require_chat_session(user_id, session_id)
    t0 = time.perf_counter()
    result = await service.lookup_documentation_tool_result(
        user_id,
        payload.query,
        session_id=session_id,
    )
    await record_voice_event(
        user_id=user_id,
        session_id=session_id,
        event=VoiceEventType.TOOL_LOOKUP,
        elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
        status_code=200,
        detail=("no_relevant_info" if result.startswith("NO_RELEVANT_INFO:") else "ok"),
    )
    return LookupDocumentationResponse(result=result)


@router.post(
    "/sessions/{session_id}/voice/commit",
    response_model=list[ChatMessageResponse],
    responses={
        200: {
            "description": (
                "Two persisted messages in order `[user, assistant]`, same shape as text "
                "chat (`ChatMessageResponse[]`) with `source = \"voice\"`. Title generation "
                "for the first assistant message runs as a background task and is **not** "
                "returned in this response; the SPA will see the title on the next "
                "`GET .../chat/sessions` call."
            )
        },
        404: {"description": "Chat session not found or not owned by the caller."},
        422: {
            "description": (
                "Validation error (malformed session_id or transcripts exceeding length limits)."
            ),
        },
    },
)
async def commit_voice_transcripts(
    session_id: SessionIdPath,
    payload: VoiceCommitRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
) -> list[ChatMessageResponse]:
    """Persist a completed OpenAI Realtime voice turn into the chat session.

    Behaviour:
    - Both transcripts are inserted via a single ``insert_many`` round trip; the
      partial unique index on ``(session_id, client_turn_id, role)`` ensures retries
      cannot create duplicates.
    - Idempotent: replays with the same ``client_turn_id`` return the originally
      stored pair. Orphans from a previous crashed attempt are cleaned up first.
    - Title generation for the first assistant message is **fire-and-forget** so the
      response returns immediately and the SPA can render the assistant bubble
      without waiting on an LLM round trip.
    - Every commit is recorded in ``realtime_session_events`` for audit, abuse, and
      cost-reconciliation queries.
    """
    user_id = str(current_user.id)
    service = ChatService()
    session = await service.require_chat_session(user_id, session_id)
    t0 = time.perf_counter()
    try:
        messages, new_title = await service.commit_voice_turn(session, user_id, payload)
    except AppException as exc:
        await record_voice_event(
            user_id=user_id,
            session_id=session_id,
            event=VoiceEventType.COMMIT_FAILED,
            elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
            status_code=400 if isinstance(exc, BadRequestException) else 503,
            detail=exc.message,
            client_turn_id=payload.client_turn_id,
        )
        raise
    if new_title:
        response.headers["X-Chat-Session-Title"] = new_title
    await record_voice_event(
        user_id=user_id,
        session_id=session_id,
        event=VoiceEventType.COMMIT,
        elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
        status_code=200,
        client_turn_id=payload.client_turn_id,
        openai_response_id=payload.openai_response_id,
    )
    return messages


@router.post(
    "/sessions/{session_id}/messages",
    response_model=list[ChatMessageResponse],
)
async def create_message(
    session_id: SessionIdPath,
    payload: ChatMessageCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    response: Response,
) -> list[ChatMessageResponse]:
    messages, new_title = await ChatService().create_message(
        str(current_user.id),
        session_id,
        payload,
    )
    if new_title:
        response.headers["X-Chat-Session-Title"] = new_title
    return messages
