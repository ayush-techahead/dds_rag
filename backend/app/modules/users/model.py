from datetime import UTC, datetime

from beanie import Document, Indexed
from pydantic import EmailStr, Field


def utc_now() -> datetime:
    return datetime.now(UTC)


class User(Document):
    email: Indexed(EmailStr, unique=True)
    full_name: str | None = None
    hashed_password: str
    is_active: bool = True
    is_superuser: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    class Settings:
        name = "users"
