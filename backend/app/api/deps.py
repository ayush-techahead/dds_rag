from typing import Annotated

from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer

from app.core.config import settings
from app.core.exceptions import UnauthorizedException
from app.core.security import decode_access_token
from app.modules.users.model import User
from app.modules.users.repository import UserRepository

oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_V1_PREFIX}/auth/login")


async def get_current_user(token: Annotated[str, Depends(oauth2_scheme)]) -> User:
    payload = decode_access_token(token)
    user_id = payload.get("sub")
    if not user_id:
        raise UnauthorizedException("Invalid authentication credentials")

    user = await UserRepository().get_by_id(user_id)
    if user is None:
        raise UnauthorizedException("Invalid authentication credentials")
    if not user.is_active:
        raise UnauthorizedException("Inactive user")
    return user
