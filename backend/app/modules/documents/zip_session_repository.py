from datetime import UTC, datetime
from pathlib import Path

from beanie import PydanticObjectId

from app.core.logging import get_logger
from app.modules.documents.zip_session_model import (
    ZipIngestSession,
    ZipIngestSessionStatus,
    ensure_utc,
)

logger = get_logger(__name__)


class ZipIngestSessionRepository:
    async def create(self, session: ZipIngestSession) -> ZipIngestSession:
        await session.insert()
        return session

    async def get_for_user(self, session_id: str, user_id: str) -> ZipIngestSession | None:
        if not PydanticObjectId.is_valid(session_id) or not PydanticObjectId.is_valid(user_id):
            return None
        return await ZipIngestSession.find_one(
            ZipIngestSession.id == PydanticObjectId(session_id),
            ZipIngestSession.user_id == PydanticObjectId(user_id),
        )

    async def save(self, session: ZipIngestSession) -> ZipIngestSession:
        session.updated_at = datetime.now(UTC)
        await session.save()
        return session

    async def delete(self, session: ZipIngestSession) -> None:
        await session.delete()

    async def purge_expired_open_sessions(self, now: datetime | None = None) -> int:
        """Delete expired OPEN sessions and their ZIP files from disk."""
        cutoff = ensure_utc(now or datetime.now(UTC))
        sessions = await ZipIngestSession.find(
            ZipIngestSession.status == ZipIngestSessionStatus.OPEN,
            ZipIngestSession.expires_at < cutoff,
        ).to_list()
        removed = 0
        for session in sessions:
            path = Path(session.storage_path)
            if path.is_file():
                try:
                    path.unlink()
                except OSError:
                    logger.warning(
                        "Could not delete expired ZIP session file",
                        extra={"path": str(path), "session_id": str(session.id)},
                    )
            await session.delete()
            removed += 1
        return removed
