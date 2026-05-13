"""Mongo-backed audit log for voice session events.

Captures every mint and commit so we have an out-of-band record independent of the
``chat_messages`` collection. The collection is intentionally narrow — it should
not store transcripts (use ``chat_messages`` for that) — only the operational
breadcrumbs needed for abuse detection, rate-limit enforcement, and cost
reconciliation against the OpenAI bill.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from beanie import Document, Indexed, PydanticObjectId
from beanie.operators import GTE, In
from pydantic import Field

from app.core.logging import get_logger

logger = get_logger(__name__)


class VoiceEventType(StrEnum):
    MINT = "mint"
    MINT_FAILED = "mint_failed"
    COMMIT = "commit"
    COMMIT_IDEMPOTENT = "commit_idempotent"
    COMMIT_RACE_RESOLVED = "commit_race_resolved"
    COMMIT_FAILED = "commit_failed"
    TOOL_LOOKUP = "tool_lookup"


class RealtimeSessionEvent(Document):
    user_id: Indexed(PydanticObjectId)
    session_id: Indexed(PydanticObjectId) | None = None
    event: VoiceEventType
    created_at: Indexed(datetime) = Field(
        default_factory=lambda: datetime.now(UTC),
    )
    elapsed_ms: float | None = None
    status_code: int | None = None
    detail: str | None = None
    openai_session_id: str | None = None
    openai_response_id: str | None = None
    client_turn_id: str | None = None

    class Settings:
        name = "realtime_session_events"


async def record_voice_event(
    *,
    user_id: str,
    event: VoiceEventType,
    session_id: str | None = None,
    elapsed_ms: float | None = None,
    status_code: int | None = None,
    detail: str | None = None,
    openai_session_id: str | None = None,
    openai_response_id: str | None = None,
    client_turn_id: str | None = None,
) -> None:
    """Persist one audit row; swallow errors so audit failures never break the request.

    Audit writes are non-blocking from a correctness standpoint: losing one row
    is acceptable; failing the user's request because the audit insert hiccupped
    is not. Errors are still surfaced via the application logger.
    """
    if not PydanticObjectId.is_valid(user_id):
        return
    record = RealtimeSessionEvent(
        user_id=PydanticObjectId(user_id),
        session_id=(
            PydanticObjectId(session_id)
            if session_id and PydanticObjectId.is_valid(session_id)
            else None
        ),
        event=event,
        elapsed_ms=elapsed_ms,
        status_code=status_code,
        detail=detail,
        openai_session_id=openai_session_id,
        openai_response_id=openai_response_id,
        client_turn_id=client_turn_id,
    )
    try:
        await record.insert()
    except Exception:
        logger.exception(
            "chat.voice.audit.insert_failed",
            extra={
                "user_id": user_id,
                "session_id": session_id,
                "event": event.value,
            },
        )


async def count_recent_mints_for_user(user_id: str, *, window_seconds: int) -> int:
    """Count successful + failed mint attempts in the trailing window for rate-limiting."""
    if not PydanticObjectId.is_valid(user_id) or window_seconds <= 0:
        return 0
    from datetime import timedelta

    cutoff = datetime.now(UTC) - timedelta(seconds=window_seconds)
    return await RealtimeSessionEvent.find(
        RealtimeSessionEvent.user_id == PydanticObjectId(user_id),
        In(
            RealtimeSessionEvent.event,
            [VoiceEventType.MINT, VoiceEventType.MINT_FAILED],
        ),
        GTE(RealtimeSessionEvent.created_at, cutoff),
    ).count()
