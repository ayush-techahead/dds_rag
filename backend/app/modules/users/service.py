from app.core.exceptions import NotFoundException
from app.modules.users.repository import UserRepository
from app.modules.users.schemas import UserResponse


class UserService:
    def __init__(self, repository: UserRepository | None = None) -> None:
        self.repository = repository or UserRepository()

    async def get_user_response(self, user_id: str) -> UserResponse:
        user = await self.repository.get_by_id(user_id)
        if user is None:
            raise NotFoundException("User not found")
        return UserResponse.from_document(user)
