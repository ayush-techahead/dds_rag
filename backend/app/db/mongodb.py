from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.core.config import settings
from app.core.logging import get_logger
from app.db.init_db import init_beanie

logger = get_logger(__name__)

mongo_client: AsyncIOMotorClient | None = None


async def connect_to_mongo() -> None:
    global mongo_client
    mongo_client = AsyncIOMotorClient(settings.MONGODB_URI)
    database = mongo_client[settings.MONGODB_DB_NAME]
    await init_beanie(database)
    logger.info("Connected to MongoDB", extra={"database": settings.MONGODB_DB_NAME})


async def close_mongo_connection() -> None:
    global mongo_client
    if mongo_client is not None:
        mongo_client.close()
        mongo_client = None
        logger.info("Closed MongoDB connection")


def get_database() -> AsyncIOMotorDatabase:
    if mongo_client is None:
        raise RuntimeError("MongoDB client is not initialized")
    return mongo_client[settings.MONGODB_DB_NAME]
