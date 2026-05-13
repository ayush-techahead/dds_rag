from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.config import settings
from app.core.logging import get_logger
from app.modules.websites.service import WebsiteService

logger = get_logger(__name__)

scheduler = AsyncIOScheduler(timezone="UTC")


async def run_scheduled_website_crawls() -> None:
    logger.debug("Scheduler tick: scanning for due website crawls")
    await WebsiteService().run_due_crawls()


async def run_scheduled_zip_session_purge() -> None:
    from app.modules.documents.zip_session_repository import ZipIngestSessionRepository

    removed = await ZipIngestSessionRepository().purge_expired_open_sessions()
    if removed:
        logger.info("Purged expired ZIP ingest sessions", extra={"count": removed})


def start_scheduler() -> None:
    if not settings.SCHEDULER_ENABLED:
        logger.info("Scheduler disabled")
        return
    if scheduler.running:
        return

    scheduler.add_job(
        run_scheduled_website_crawls,
        "interval",
        seconds=settings.SCHEDULER_TICK_SECONDS,
        id="website_due_crawl_scan",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        run_scheduled_zip_session_purge,
        "interval",
        seconds=settings.SCHEDULER_TICK_SECONDS,
        id="zip_session_purge",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    logger.info("Scheduler started", extra={"tick_seconds": settings.SCHEDULER_TICK_SECONDS})


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped")
