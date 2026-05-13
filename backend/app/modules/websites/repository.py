from datetime import UTC, datetime

from beanie import PydanticObjectId

from app.modules.websites.model import (
    CrawledWebsitePage,
    CrawlJobStatus,
    WebsiteCrawlJob,
    WebsiteSource,
    WebsiteStatus,
)


class WebsiteRepository:
    async def create_source(self, website: WebsiteSource) -> WebsiteSource:
        await website.insert()
        return website

    async def save_source(self, website: WebsiteSource) -> WebsiteSource:
        website.updated_at = datetime.now(UTC)
        await website.save()
        return website

    async def get_source_for_user(self, website_id: str, user_id: str) -> WebsiteSource | None:
        if not PydanticObjectId.is_valid(website_id) or not PydanticObjectId.is_valid(user_id):
            return None
        return await WebsiteSource.find_one(
            WebsiteSource.id == PydanticObjectId(website_id),
            WebsiteSource.user_id == PydanticObjectId(user_id),
        )

    async def list_sources_for_user(self, user_id: str) -> list[WebsiteSource]:
        if not PydanticObjectId.is_valid(user_id):
            return []
        return (
            await WebsiteSource.find(WebsiteSource.user_id == PydanticObjectId(user_id))
            .sort("-created_at")
            .to_list()
        )

    async def list_due_sources(self, now: datetime, limit: int = 25) -> list[WebsiteSource]:
        return (
            await WebsiteSource.find(
                WebsiteSource.status == WebsiteStatus.ACTIVE,
                WebsiteSource.next_crawl_at != None,  # noqa: E711
                WebsiteSource.next_crawl_at <= now,
            )
            .sort("next_crawl_at")
            .limit(limit)
            .to_list()
        )

    async def create_job(self, job: WebsiteCrawlJob) -> WebsiteCrawlJob:
        await job.insert()
        return job

    async def save_job(self, job: WebsiteCrawlJob) -> WebsiteCrawlJob:
        job.updated_at = datetime.now(UTC)
        await job.save()
        return job

    async def mark_job_running(self, job: WebsiteCrawlJob) -> WebsiteCrawlJob:
        job.status = CrawlJobStatus.RUNNING
        job.started_at = datetime.now(UTC)
        return await self.save_job(job)

    async def mark_job_succeeded(
        self,
        job: WebsiteCrawlJob,
        pages_crawled: int,
        chunks_indexed: int,
    ) -> WebsiteCrawlJob:
        job.status = CrawlJobStatus.SUCCEEDED
        job.finished_at = datetime.now(UTC)
        job.pages_crawled = pages_crawled
        job.chunks_indexed = chunks_indexed
        job.error_message = None
        return await self.save_job(job)

    async def mark_job_failed(self, job: WebsiteCrawlJob, error_message: str) -> WebsiteCrawlJob:
        job.status = CrawlJobStatus.FAILED
        job.finished_at = datetime.now(UTC)
        job.error_message = error_message[:500]
        return await self.save_job(job)

    async def create_page(self, page: CrawledWebsitePage) -> CrawledWebsitePage:
        await page.insert()
        return page

    async def list_jobs_for_source(self, website_id: str, user_id: str) -> list[WebsiteCrawlJob]:
        if not PydanticObjectId.is_valid(website_id) or not PydanticObjectId.is_valid(user_id):
            return []
        return (
            await WebsiteCrawlJob.find(
                WebsiteCrawlJob.website_id == PydanticObjectId(website_id),
                WebsiteCrawlJob.user_id == PydanticObjectId(user_id),
            )
            .sort("-created_at")
            .to_list()
        )
