from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.deps import get_current_user
from app.modules.users.model import User
from app.modules.websites.schemas import (
    CrawlJobResponse,
    WebsiteCreate,
    WebsiteResponse,
    WebsiteUpdate,
)
from app.modules.websites.service import WebsiteService

router = APIRouter()


@router.post("", response_model=WebsiteResponse, status_code=status.HTTP_201_CREATED)
async def create_website(
    payload: WebsiteCreate,
    current_user: Annotated[User, Depends(get_current_user)],
) -> WebsiteResponse:
    return await WebsiteService().create_source(str(current_user.id), payload)


@router.get("", response_model=list[WebsiteResponse])
async def list_websites(
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[WebsiteResponse]:
    return await WebsiteService().list_sources(str(current_user.id))


@router.get("/{website_id}", response_model=WebsiteResponse)
async def get_website(
    website_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
) -> WebsiteResponse:
    return await WebsiteService().get_source(str(current_user.id), website_id)


@router.patch("/{website_id}", response_model=WebsiteResponse)
async def update_website(
    website_id: str,
    payload: WebsiteUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
) -> WebsiteResponse:
    return await WebsiteService().update_source(str(current_user.id), website_id, payload)


@router.post("/{website_id}/crawl", response_model=CrawlJobResponse)
async def crawl_website_now(
    website_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
) -> CrawlJobResponse:
    return await WebsiteService().crawl_now(str(current_user.id), website_id)


@router.get("/{website_id}/crawl-jobs", response_model=list[CrawlJobResponse])
async def list_website_crawl_jobs(
    website_id: str,
    current_user: Annotated[User, Depends(get_current_user)],
) -> list[CrawlJobResponse]:
    return await WebsiteService().list_crawl_jobs(str(current_user.id), website_id)
