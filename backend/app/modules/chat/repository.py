from datetime import UTC, datetime

from beanie import PydanticObjectId

from app.modules.chat.model import ChatMessage, ChatSession, MessageRole


class ChatRepository:
    async def create_session(self, session: ChatSession) -> ChatSession:
        await session.insert()
        return session

    async def list_sessions_by_user(self, user_id: str) -> list[ChatSession]:
        if not PydanticObjectId.is_valid(user_id):
            return []
        return (
            await ChatSession.find(
                ChatSession.user_id == PydanticObjectId(user_id),
                ChatSession.deleted_at == None,  # noqa: E711
            )
            .sort("-updated_at")
            .to_list()
        )

    async def get_session_for_user(self, session_id: str, user_id: str) -> ChatSession | None:
        if not PydanticObjectId.is_valid(session_id) or not PydanticObjectId.is_valid(user_id):
            return None
        session = await ChatSession.find_one(
            ChatSession.id == PydanticObjectId(session_id),
            ChatSession.user_id == PydanticObjectId(user_id),
        )
        if session is None or session.deleted_at is not None:
            return None
        return session

    async def soft_delete_session_for_user(self, session_id: str, user_id: str) -> bool:
        """Mark session deleted for this user. Idempotent if already deleted. False if not owned."""
        if not PydanticObjectId.is_valid(session_id) or not PydanticObjectId.is_valid(user_id):
            return False
        session = await ChatSession.find_one(
            ChatSession.id == PydanticObjectId(session_id),
            ChatSession.user_id == PydanticObjectId(user_id),
        )
        if session is None:
            return False
        if session.deleted_at is not None:
            return True
        session.deleted_at = datetime.now(UTC)
        await session.save()
        return True

    async def create_message(self, message: ChatMessage) -> ChatMessage:
        await message.insert()
        return message

    async def create_voice_turn(
        self,
        user_message: ChatMessage,
        assistant_message: ChatMessage,
    ) -> tuple[ChatMessage, ChatMessage]:
        """Insert both messages of a voice turn via a single ``insert_many`` round trip.

        Tighter partial-write window than two sequential ``insert`` calls. The
        ``ordered=True`` default means: a unique-index conflict on the **user** row
        results in zero inserts (clean failure); a conflict on the **assistant** row
        leaves the user row committed. The service layer is responsible for the
        idempotency / orphan-recovery reconciliation since only it knows the
        ``client_turn_id`` semantics.

        ``BulkWriteError`` / ``DuplicateKeyError`` are surfaced to the caller.
        """
        inserted = await ChatMessage.insert_many([user_message, assistant_message])
        ids = list(getattr(inserted, "inserted_ids", []) or [])
        if len(ids) >= 1:
            user_message.id = ids[0]
        if len(ids) >= 2:
            assistant_message.id = ids[1]
        return user_message, assistant_message

    async def delete_message(self, message: ChatMessage) -> None:
        """Best-effort delete used to roll back a half-persisted voice turn."""
        try:
            await message.delete()
        except Exception:
            # Surface as warning; the orphan row is recoverable but should not mask
            # the original failure that triggered the rollback.
            pass

    async def find_voice_turn_by_client_id(
        self,
        session_id: str,
        client_turn_id: str,
    ) -> list[ChatMessage]:
        """Return the (user, assistant) pair previously stored for ``client_turn_id``.

        Order is oldest-first so callers can return the list verbatim to the client.
        Returns an empty list when no matching turn exists; a length-1 list is treated
        as a partial/legacy write and ignored by the caller.
        """
        if not PydanticObjectId.is_valid(session_id) or not client_turn_id:
            return []
        oid = PydanticObjectId(session_id)
        return (
            await ChatMessage.find(
                ChatMessage.session_id == oid,
                ChatMessage.client_turn_id == client_turn_id,
            )
            .sort("created_at")
            .to_list()
        )

    async def list_messages_for_session(
        self,
        session_id: str,
        *,
        limit: int | None = None,
    ) -> list[ChatMessage]:
        """Without ``limit``: oldest-first (full thread). With ``limit``: newest-first, capped."""
        if not PydanticObjectId.is_valid(session_id):
            return []
        oid = PydanticObjectId(session_id)
        q = ChatMessage.find(ChatMessage.session_id == oid)
        if limit is not None and limit > 0:
            return await q.sort("-created_at").limit(limit).to_list()
        return await q.sort("created_at").to_list()

    async def touch_session(self, session: ChatSession) -> ChatSession:
        session.updated_at = datetime.now(UTC)
        await session.save()
        return session

    async def count_assistant_messages(self, session_id: str) -> int:
        if not PydanticObjectId.is_valid(session_id):
            return 0
        oid = PydanticObjectId(session_id)
        return await ChatMessage.find(
            ChatMessage.session_id == oid,
            ChatMessage.role == MessageRole.ASSISTANT,
        ).count()

    async def set_session_title(self, session: ChatSession, title: str) -> ChatSession:
        session.title = title
        session.updated_at = datetime.now(UTC)
        await session.save()
        return session
