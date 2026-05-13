from datetime import UTC, datetime
from enum import StrEnum

import pymongo
from beanie import Document, Indexed, PydanticObjectId
from pydantic import Field
from pymongo import IndexModel


def utc_now() -> datetime:
    return datetime.now(UTC)


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"


class MessageSource(StrEnum):
    """Where a stored message originated. Useful for analytics and debugging."""

    TEXT = "text"
    VOICE = "voice"


class ChatSession(Document):
    user_id: Indexed(PydanticObjectId)
    title: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    deleted_at: datetime | None = None

    class Settings:
        name = "chat_sessions"


class ChatMessage(Document):
    session_id: Indexed(PydanticObjectId)
    user_id: Indexed(PydanticObjectId)
    role: MessageRole
    content: str
    created_at: datetime = Field(default_factory=utc_now)
    # Stored on every message; legacy rows without this field decode as the default.
    source: MessageSource = MessageSource.TEXT
    # Set by the voice commit endpoint to dedupe retries of the same audio turn.
    client_turn_id: str | None = None
    # Optional debugging breadcrumb from OpenAI Realtime (response.id).
    openai_response_id: str | None = None

    class Settings:
        name = "chat_messages"
        # Partial unique index so two concurrent retries with the same client_turn_id
        # cannot both insert a row for the same role within the same session. The
        # second writer races on the index and gets DuplicateKeyError, which the
        # service catches and reconciles by reading the already-persisted pair.
        # The partial filter scopes uniqueness to rows that actually carry an id,
        # leaving legacy / text-chat rows (where client_turn_id is null) unaffected.
        indexes = [
            IndexModel(
                [
                    ("session_id", pymongo.ASCENDING),
                    ("client_turn_id", pymongo.ASCENDING),
                    ("role", pymongo.ASCENDING),
                ],
                name="uniq_session_voice_turn_role",
                unique=True,
                partialFilterExpression={"client_turn_id": {"$type": "string"}},
            ),
        ]
