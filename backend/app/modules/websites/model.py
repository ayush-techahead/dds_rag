from datetime import UTC, datetime
from enum import StrEnum

from beanie import Document, Indexed, PydanticObjectId
from pydantic import Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class CrawlFrequency(StrEnum):
    NEVER = "never"
    ONLY_ONCE = "only_once"
    EVERY_12_HOURS = "12h"
    DAILY = "1d"


class WebsiteStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"


class CrawlJobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class WebsiteSource(Document):
    user_id: Indexed(PydanticObjectId)
    url: str
    name: str | None = None
    frequency: CrawlFrequency = CrawlFrequency.NEVER
    status: WebsiteStatus = WebsiteStatus.ACTIVE
    last_crawled_at: datetime | None = None
    next_crawl_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    class Settings:
        name = "website_sources"


class WebsiteCrawlJob(Document):
    website_id: Indexed(PydanticObjectId)
    user_id: Indexed(PydanticObjectId)
    status: CrawlJobStatus = CrawlJobStatus.PENDING
    started_at: datetime | None = None
    finished_at: datetime | None = None
    pages_crawled: int = 0
    chunks_indexed: int = 0
    error_message: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    class Settings:
        name = "website_crawl_jobs"


class CrawledWebsitePage(Document):
    website_id: Indexed(PydanticObjectId)
    crawl_job_id: Indexed(PydanticObjectId)
    user_id: Indexed(PydanticObjectId)
    url: str
    title: str | None = None
    content_hash: str
    text_content: str
    chunk_count: int = 0
    created_at: datetime = Field(default_factory=utc_now)

    class Settings:
        name = "website_pages"
