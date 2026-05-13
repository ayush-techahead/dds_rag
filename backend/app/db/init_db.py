from beanie import init_beanie as beanie_init
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.modules.chat.audit import RealtimeSessionEvent
from app.modules.chat.model import ChatMessage, ChatSession
from app.modules.documents.model import SourceDocument
from app.modules.documents.zip_session_model import ZipIngestSession
from app.modules.users.model import User
from app.modules.websites.model import CrawledWebsitePage, WebsiteCrawlJob, WebsiteSource


async def init_beanie(database: AsyncIOMotorDatabase) -> None:
    await beanie_init(
        database=database,
        document_models=[
            User,
            ChatSession,
            ChatMessage,
            RealtimeSessionEvent,
            SourceDocument,
            ZipIngestSession,
            WebsiteSource,
            WebsiteCrawlJob,
            CrawledWebsitePage,
        ],
    )
