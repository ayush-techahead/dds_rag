from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import get_current_user
from app.modules.users.model import User
from app.modules.users.schemas import UserResponse

router = APIRouter()


@router.get("/me", response_model=UserResponse)
async def read_current_user(current_user: Annotated[User, Depends(get_current_user)]) -> User:
    return current_user
