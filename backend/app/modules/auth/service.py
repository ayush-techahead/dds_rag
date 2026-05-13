from app.core.exceptions import BadRequestException, UnauthorizedException
from app.core.security import create_access_token, get_password_hash, verify_password
from app.modules.auth.schemas import Token
from app.modules.users.model import User
from app.modules.users.repository import UserRepository
from app.modules.users.schemas import UserCreate, UserLogin, UserResponse


class AuthService:
    def __init__(self, user_repository: UserRepository | None = None) -> None:
        self.user_repository = user_repository or UserRepository()

    async def register(self, payload: UserCreate) -> UserResponse:
        email = payload.email.lower()
        existing_user = await self.user_repository.get_by_email(email)
        if existing_user is not None:
            raise BadRequestException("A user with this email already exists")

        user = User(
            email=email,
            full_name=payload.full_name,
            hashed_password=get_password_hash(payload.password),
        )
        created_user = await self.user_repository.create(user)
        return UserResponse.from_document(created_user)

    async def login(self, payload: UserLogin) -> Token:
        user = await self.user_repository.get_by_email(payload.email.lower())
        if user is None or not verify_password(payload.password, user.hashed_password):
            raise UnauthorizedException("Incorrect email or password")
        if not user.is_active:
            raise UnauthorizedException("Inactive user")

        return Token(access_token=create_access_token(subject=str(user.id)))
