from datetime import UTC, datetime

from beanie import PydanticObjectId

from app.modules.users.model import User


class UserRepository:
    async def create(self, user: User) -> User:
        await user.insert()
        return user

    async def get_by_id(self, user_id: str) -> User | None:
        if not PydanticObjectId.is_valid(user_id):
            return None
        return await User.get(PydanticObjectId(user_id))

    async def get_by_email(self, email: str) -> User | None:
        return await User.find_one(User.email == email.lower())

    async def update(self, user: User) -> User:
        user.updated_at = datetime.now(UTC)
        await user.save()
        return user
