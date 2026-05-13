import hashlib
from datetime import UTC, datetime, timedelta

import httpx
from beanie import PydanticObjectId

from app.core.config import settings
from app.core.exceptions import AppException, BadRequestException, NotFoundException
from app.core.logging import get_logger
from app.modules.embeddings.openai_embeddings import OpenAIEmbeddingProvider
from app.modules.ingestion.chunking import resolve_chunker_for_ingestion
from app.modules.vector_store.qdrant import QdrantVectorStore
from app.modules.websites.html import extract_html_text
from app.modules.websites.model import (
    CrawledWebsitePage,
    CrawlFrequency,
    WebsiteCrawlJob,
    WebsiteSource,
)
from app.modules.websites.repository import WebsiteRepository
from app.modules.websites.schemas import (
    CrawlJobResponse,
    WebsiteCreate,
    WebsiteResponse,
    WebsiteUpdate,
)

logger = get_logger(__name__)


class WebsiteService:
    def __init__(
        self,
        repository: WebsiteRepository | None = None,
        embedding_provider: OpenAIEmbeddingProvider | None = None,
        vector_store: QdrantVectorStore | None = None,
    ) -> None:
        self.repository = repository or WebsiteRepository()
        # Same embeddings as chat RAG queries / document ingest — required for comparable vectors.
        self.embedding_provider = embedding_provider or OpenAIEmbeddingProvider()
        self.vector_store = vector_store or QdrantVectorStore()

    async def create_source(self, user_id: str, payload: WebsiteCreate) -> WebsiteResponse:
        now = datetime.now(UTC)
        website = WebsiteSource(
            user_id=PydanticObjectId(user_id),
            url=str(payload.url),
            name=payload.name,
            frequency=payload.frequency,
            next_crawl_at=self._calculate_next_crawl_at(payload.frequency, now),
        )
        created = await self.repository.create_source(website)
        return WebsiteResponse.from_document(created)

    async def update_source(
        self,
        user_id: str,
        website_id: str,
        payload: WebsiteUpdate,
    ) -> WebsiteResponse:
        website = await self.repository.get_source_for_user(website_id, user_id)
        if website is None:
            raise NotFoundException("Website source not found")

        if payload.name is not None:
            website.name = payload.name
        if payload.status is not None:
            website.status = payload.status
        if payload.frequency is not None:
            website.frequency = payload.frequency
            website.next_crawl_at = self._calculate_next_crawl_at(
                payload.frequency,
                website.last_crawled_at or datetime.now(UTC),
            )

        saved = await self.repository.save_source(website)
        return WebsiteResponse.from_document(saved)

    async def list_sources(self, user_id: str) -> list[WebsiteResponse]:
        websites = await self.repository.list_sources_for_user(user_id)
        return [WebsiteResponse.from_document(website) for website in websites]

    async def get_source(self, user_id: str, website_id: str) -> WebsiteResponse:
        website = await self.repository.get_source_for_user(website_id, user_id)
        if website is None:
            raise NotFoundException("Website source not found")
        return WebsiteResponse.from_document(website)

    async def crawl_now(self, user_id: str, website_id: str) -> CrawlJobResponse:
        website = await self.repository.get_source_for_user(website_id, user_id)
        if website is None:
            raise NotFoundException("Website source not found")
        job = await self.crawl_website(website)
        return CrawlJobResponse.from_document(job)

    async def list_crawl_jobs(self, user_id: str, website_id: str) -> list[CrawlJobResponse]:
        jobs = await self.repository.list_jobs_for_source(website_id, user_id)
        return [CrawlJobResponse.from_document(job) for job in jobs]

    async def run_due_crawls(self, limit: int = 25) -> None:
        due_sources = await self.repository.list_due_sources(datetime.now(UTC), limit=limit)
        website_summaries = [
            {"website_id": str(w.id), "url": w.url, "name": w.name} for w in due_sources
        ]
        detail = (
            " — "
            + "; ".join(
                f"{s['website_id']} url={s['url']!r}"
                + (f" name={s['name']!r}" if s["name"] else "")
                for s in website_summaries
            )
            if website_summaries
            else ""
        )
        logger.info(
            "Scheduled website reindex scan: %d due source(s) (limit=%d)%s",
            len(due_sources),
            limit,
            detail,
            extra={"due_count": len(due_sources), "websites": website_summaries},
        )
        for website in due_sources:
            try:
                await self.crawl_website(website)
            except Exception:
                logger.exception(
                    "Scheduled website crawl failed",
                    extra={"website_id": str(website.id)},
                )

    async def crawl_website(self, website: WebsiteSource) -> WebsiteCrawlJob:
        job = await self.repository.create_job(
            WebsiteCrawlJob(website_id=website.id, user_id=website.user_id)
        )
        await self.repository.mark_job_running(job)

        try:
            title, text = await self._fetch_website_text(website.url)
            chunks = resolve_chunker_for_ingestion(text, "html").split(text)
            if not chunks:
                raise BadRequestException("Website page does not contain enough text to index")

            page = await self.repository.create_page(
                CrawledWebsitePage(
                    website_id=website.id,
                    crawl_job_id=job.id,
                    user_id=website.user_id,
                    url=website.url,
                    title=title,
                    content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    text_content=text,
                    chunk_count=len(chunks),
                )
            )

            chunk_texts = [chunk.text for chunk in chunks]
            vectors = await self.embedding_provider.embed_documents(chunk_texts)
            vector_count = await self.vector_store.upsert_website_chunks(
                website_id=str(website.id),
                page_id=str(page.id),
                crawl_job_id=str(job.id),
                user_id=str(website.user_id),
                url=website.url,
                title=title,
                chunks=chunks,
                vectors=vectors,
            )

            website.last_crawled_at = datetime.now(UTC)
            website.next_crawl_at = self._calculate_next_crawl_after_run(
                website.frequency,
                website.last_crawled_at,
            )
            website.last_error = None
            await self.repository.save_source(website)
            return await self.repository.mark_job_succeeded(
                job,
                pages_crawled=1,
                chunks_indexed=vector_count,
            )
        except AppException as exc:
            await self._mark_crawl_failed(website, job, exc.message)
            raise
        except Exception as exc:
            await self._mark_crawl_failed(website, job, "Unexpected website crawl failure")
            raise BadRequestException("Could not crawl website") from exc

    async def _fetch_website_text(self, url: str) -> tuple[str | None, str]:
        timeout = httpx.Timeout(settings.WEBSITE_CRAWL_TIMEOUT_SECONDS)
        max_bytes = settings.WEBSITE_MAX_HTML_BYTES

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(url, headers={"User-Agent": "rag-chatbot-backend/0.1"})

        if response.status_code >= 400:
            raise BadRequestException(f"Website returned HTTP {response.status_code}")

        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type and "text/plain" not in content_type:
            raise BadRequestException("Website URL did not return HTML or text content")

        content = response.content[: max_bytes + 1]
        if len(content) > max_bytes:
            raise BadRequestException("Website response is too large to crawl")

        html = content.decode(response.encoding or "utf-8", errors="replace")
        title, text = extract_html_text(html)
        if not text:
            raise BadRequestException("Website page does not contain extractable text")
        return title, text

    async def _mark_crawl_failed(
        self,
        website: WebsiteSource,
        job: WebsiteCrawlJob,
        error_message: str,
    ) -> None:
        website.last_error = error_message[:500]
        website.next_crawl_at = self._calculate_next_crawl_after_run(
            website.frequency,
            datetime.now(UTC),
        )
        await self.repository.save_source(website)
        await self.repository.mark_job_failed(job, error_message)

    def _calculate_next_crawl_at(
        self,
        frequency: CrawlFrequency,
        base_time: datetime,
    ) -> datetime | None:
        if frequency == CrawlFrequency.NEVER:
            return None
        if frequency == CrawlFrequency.ONLY_ONCE:
            return base_time
        if frequency == CrawlFrequency.EVERY_12_HOURS:
            return base_time + timedelta(hours=12)
        if frequency == CrawlFrequency.DAILY:
            return base_time + timedelta(days=1)
        raise BadRequestException("Unsupported crawl frequency")

    def _calculate_next_crawl_after_run(
        self,
        frequency: CrawlFrequency,
        base_time: datetime,
    ) -> datetime | None:
        if frequency == CrawlFrequency.ONLY_ONCE:
            return None
        return self._calculate_next_crawl_at(frequency, base_time)
