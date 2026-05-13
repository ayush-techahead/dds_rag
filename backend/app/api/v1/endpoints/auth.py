import json

from fastapi import APIRouter, Request, status  # pyright: ignore[reportMissingImports]

from app.core.exceptions import BadRequestException
from app.modules.auth.schemas import Token
from app.modules.auth.service import AuthService
from app.modules.users.schemas import UserCreate, UserLogin, UserResponse

router = APIRouter()

_LOGIN_OPENAPI_EXTRA = {
    "requestBody": {
        "required": True,
        "description": (
            "Send **either** `application/json` **or** `application/x-www-form-urlencoded` "
            "(the handler inspects `Content-Type`)."
        ),
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["password"],
                    "properties": {
                        "email": {"type": "string", "format": "email"},
                        "username": {
                            "type": "string",
                            "description": "Alias for `email` in JSON bodies",
                        },
                        "password": {"type": "string", "minLength": 1, "maxLength": 72},
                    },
                    "description": "Must include `email` or `username` (email) plus `password`.",
                },
            },
            "application/x-www-form-urlencoded": {
                "schema": {
                    "type": "object",
                    "required": ["password"],
                    "properties": {
                        "username": {
                            "type": "string",
                            "description": "Account email (OAuth2 password flow field name)",
                        },
                        "email": {"type": "string", "format": "email"},
                        "password": {"type": "string"},
                    },
                    "description": (
                        "Must include `password` and `username` or `email` (account email)."
                    ),
                },
            },
        },
    },
}


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: UserCreate) -> UserResponse:
    return await AuthService().register(payload)


@router.post("/login", response_model=Token, openapi_extra=_LOGIN_OPENAPI_EXTRA)
async def login(request: Request) -> Token:
    """Accept JSON ``{email, password}`` (or ``username`` as email alias) or form-urlencoded.

    Form body: ``username`` + ``password`` (OAuth2) or ``email`` + ``password`` (HTML forms).
    Parsed manually so ``application/x-www-form-urlencoded`` is not mistaken for a JSON body.
    """
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type == "application/json":
        try:
            raw = await request.json()
        except json.JSONDecodeError as exc:
            raise BadRequestException("Invalid JSON body") from exc
        if not isinstance(raw, dict):
            raise BadRequestException("JSON body must be an object")
        data = dict(raw)
        if "email" not in data and data.get("username"):
            data["email"] = data["username"]
        payload = UserLogin.model_validate(data)
    else:
        form = await request.form()
        # Many browsers / SPAs post ``email``; OAuth2 uses ``username`` for the same value.
        identifier = form.get("username") or form.get("email")
        password = form.get("password")
        if identifier is None or password is None:
            raise BadRequestException(
                "Form body must include password and email or username (account email)",
            )
        email = str(identifier).strip()
        payload = UserLogin(email=email, password=str(password))
    return await AuthService().login(payload)
