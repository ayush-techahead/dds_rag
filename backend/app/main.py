import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request  # pyright: ignore[reportMissingImports]
from fastapi.exceptions import RequestValidationError  # pyright: ignore[reportMissingImports]
from fastapi.middleware.cors import CORSMiddleware  # pyright: ignore[reportMissingImports]
from fastapi.responses import JSONResponse, Response  # pyright: ignore[reportMissingImports]

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.exceptions import (
    BadRequestException,
    ForbiddenException,
    NotFoundException,
    ServiceUnavailableException,
    TooManyRequestsException,
    UnauthorizedException,
)
from app.core.logging import configure_logging, get_logger
from app.core.scheduler import start_scheduler, stop_scheduler
from app.db.mongodb import close_mongo_connection, connect_to_mongo

logger = get_logger(__name__)


def _json_default_for_validation(o: object) -> object:
    """Make Pydantic validation error payloads JSON-safe (e.g. raw body as bytes)."""
    if isinstance(o, bytes):
        return o.decode("utf-8", errors="replace")
    if isinstance(o, set):
        return list(o)
    return str(o)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    configure_logging()
    logger.info("Starting application", extra={"service": settings.PROJECT_NAME})
    await connect_to_mongo()
    start_scheduler()
    yield
    stop_scheduler()
    await close_mongo_connection()
    logger.info("Application shutdown complete", extra={"service": settings.PROJECT_NAME})


def create_application() -> FastAPI:
    app = FastAPI(
        title=settings.PROJECT_NAME,
        debug=settings.DEBUG,
        version="0.1.0",
        lifespan=lifespan,
    )

    # Starlette returns 400 on preflight when Origin fails CORS checks. In development and
    # tests, allow localhost and 127.0.0.1 with any port so typical dev setups match even if
    # BACKEND_CORS_ORIGINS only lists one loopback spelling.
    _cors_regex = None
    if settings.ENVIRONMENT in frozenset({"development", "test"}):
        _cors_regex = r"https?://(localhost|127\.0\.0\.1)(:\d+)?$"
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[str(origin) for origin in settings.BACKEND_CORS_ORIGINS],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        allow_origin_regex=_cors_regex,
    )

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.API_V1_PREFIX)
    return app


def register_exception_handlers(app: FastAPI) -> None:
    """Register handlers with distinct callables (avoid loop + duplicate ``handler`` name)."""

    @app.exception_handler(BadRequestException)
    async def bad_request_handler(request: Request, exc: BadRequestException) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": exc.message})

    @app.exception_handler(UnauthorizedException)
    async def unauthorized_handler(request: Request, exc: UnauthorizedException) -> JSONResponse:
        return JSONResponse(status_code=401, content={"detail": exc.message})

    @app.exception_handler(ForbiddenException)
    async def forbidden_handler(request: Request, exc: ForbiddenException) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": exc.message})

    @app.exception_handler(NotFoundException)
    async def not_found_handler(request: Request, exc: NotFoundException) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": exc.message})

    @app.exception_handler(TooManyRequestsException)
    async def too_many_requests_handler(
        request: Request,
        exc: TooManyRequestsException,
    ) -> JSONResponse:
        return JSONResponse(status_code=429, content={"detail": exc.message})

    @app.exception_handler(ServiceUnavailableException)
    async def service_unavailable_handler(
        request: Request,
        exc: ServiceUnavailableException,
    ) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": exc.message})

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> Response:
        # Single json.dumps with default — Starlette JSONResponse re-serializes ``content`` without
        # a custom encoder, which can raise again on bytes inside Pydantic error payloads.
        body = json.dumps(
            {"detail": "Validation error", "errors": exc.errors()},
            default=_json_default_for_validation,
        )
        return Response(status_code=422, content=body, media_type="application/json")

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "Unhandled application error",
            extra={"path": request.url.path, "method": request.method},
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )


app = create_application()
