import os
from collections.abc import AsyncGenerator

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from motor.motor_asyncio import AsyncIOMotorClient

os.environ.setdefault("PROJECT_NAME", "rag_chatbot_backend")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("API_V1_PREFIX", "/api/v1")
if test_mongodb_uri := os.environ.get("TEST_MONGODB_URI"):
    os.environ["MONGODB_URI"] = test_mongodb_uri
os.environ["MONGODB_DB_NAME"] = "rag_chatbot_backend_test"
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-change-me")
os.environ.setdefault("JWT_ALGORITHM", "HS256")
os.environ.setdefault("ACCESS_TOKEN_EXPIRE_MINUTES", "1440")
os.environ.setdefault("BACKEND_CORS_ORIGINS", '["http://localhost:3000"]')
os.environ.setdefault("QDRANT_URL", "http://localhost:6333")
os.environ.setdefault("QDRANT_API_KEY", "")
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["DOCUMENT_CHUNK_STRATEGY"] = "auto"

from app.core.config import settings  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
async def clean_test_database() -> AsyncGenerator[None, None]:
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    await client.drop_database(settings.MONGODB_DB_NAME)
    yield
    await client.drop_database(settings.MONGODB_DB_NAME)
    client.close()


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as async_client:
            yield async_client
