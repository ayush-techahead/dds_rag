from datetime import datetime

from beanie import PydanticObjectId
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.modules.chat.model import ChatMessage, ChatSession, MessageRole, MessageSource


class ChatSessionCreate(BaseModel):
    title: str | None = Field(default=None, max_length=120)


class ChatSessionResponse(BaseModel):
    id: str
    user_id: str
    title: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @field_validator("id", "user_id", mode="before")
    @classmethod
    def stringify_object_id(cls, value: PydanticObjectId | str) -> str:
        return str(value)

    @classmethod
    def from_document(cls, session: ChatSession) -> "ChatSessionResponse":
        return cls.model_validate(session)


class ChatMessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=8000)


class ChatMessageResponse(BaseModel):
    id: str
    session_id: str
    user_id: str
    role: MessageRole
    content: str
    created_at: datetime
    source: MessageSource = MessageSource.TEXT

    model_config = ConfigDict(from_attributes=True)

    @field_validator("id", "session_id", "user_id", mode="before")
    @classmethod
    def stringify_object_id(cls, value: PydanticObjectId | str) -> str:
        return str(value)

    @classmethod
    def from_document(cls, message: ChatMessage) -> "ChatMessageResponse":
        return cls.model_validate(message)


class ChatSessionDetailResponse(ChatSessionResponse):
    messages: list[ChatMessageResponse] = Field(default_factory=list)


class LookupDocumentationRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)


class LookupDocumentationResponse(BaseModel):
    """Pass verbatim to OpenAI Realtime as the function tool output (plain text)."""

    result: str


class VoiceCommitRequest(BaseModel):
    """Body for ``POST .../voice/commit``.

    ``client_turn_id`` makes retries idempotent: if the same id is replayed for the same
    chat session, the server returns the already-persisted user+assistant pair instead of
    inserting duplicates. ``openai_response_id`` is stored as a debug breadcrumb only.
    """

    user_transcript: str = Field(min_length=1, max_length=8000)
    assistant_transcript: str = Field(min_length=1, max_length=16000)
    client_turn_id: str | None = Field(default=None, min_length=1, max_length=128)
    openai_response_id: str | None = Field(default=None, min_length=1, max_length=128)


class RealtimeClientSecret(BaseModel):
    value: str
    expires_at: int


class RealtimeSessionMintResponse(BaseModel):
    """Ephemeral Realtime credentials for the browser; chat_session_id ties persistence to Mongo."""

    chat_session_id: str
    openai_session_id: str
    client_secret: RealtimeClientSecret
    model: str
    voice_instructions: str = ""
