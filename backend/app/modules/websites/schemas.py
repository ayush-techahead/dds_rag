from datetime import datetime

from beanie import PydanticObjectId
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from app.modules.websites.model import (
    CrawlFrequency,
    CrawlJobStatus,
    WebsiteCrawlJob,
    WebsiteSource,
    WebsiteStatus,
)


class WebsiteCreate(BaseModel):
    url: HttpUrl
    name: str | None = Field(default=None, max_length=120)
    frequency: CrawlFrequency = CrawlFrequency.NEVER


class WebsiteUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    frequency: CrawlFrequency | None = None
    status: WebsiteStatus | None = None


class WebsiteResponse(BaseModel):
    id: str
    user_id: str
    url: str
    name: str | None
    frequency: CrawlFrequency
    status: WebsiteStatus
    last_crawled_at: datetime | None
    next_crawl_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @field_validator("id", "user_id", mode="before")
    @classmethod
    def stringify_object_id(cls, value: PydanticObjectId | str) -> str:
        return str(value)

    @classmethod
    def from_document(cls, website: WebsiteSource) -> "WebsiteResponse":
        return cls.model_validate(website)


class CrawlJobResponse(BaseModel):
    id: str
    website_id: str
    user_id: str
    status: CrawlJobStatus
    started_at: datetime | None
    finished_at: datetime | None
    pages_crawled: int
    chunks_indexed: int
    error_message: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @field_validator("id", "website_id", "user_id", mode="before")
    @classmethod
    def stringify_object_id(cls, value: PydanticObjectId | str) -> str:
        return str(value)

    @classmethod
    def from_document(cls, job: WebsiteCrawlJob) -> "CrawlJobResponse":
        return cls.model_validate(job)
