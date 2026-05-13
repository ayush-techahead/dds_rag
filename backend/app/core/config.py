from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root: app/core/config.py -> parents: core, app, project
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DOTENV_PATH = _PROJECT_ROOT / ".env"
_ENV_FILE_ARG: str | Path = _DOTENV_PATH if _DOTENV_PATH.is_file() else ".env"


class Settings(BaseSettings):
    PROJECT_NAME: str = "rag_chatbot_backend"
    ENVIRONMENT: str = "development"
    DEBUG: bool = False
    API_V1_PREFIX: str = "/api/v1"

    MONGODB_URI: str = "mongodb://localhost:27017"
    MONGODB_DB_NAME: str = "rag_chatbot_backend"

    JWT_SECRET_KEY: str = Field(default="change-me-in-production", min_length=16)
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440

    BACKEND_CORS_ORIGINS: list[str] = [
        "http://localhost:3000",
        "http://localhost:8000",
        "http://localhost:5173",
        "*"
    ]

    QDRANT_URL: str = "http://localhost:6333"
    QDRANT_API_KEY: str = ""
    QDRANT_COLLECTION_NAME: str = "rag_documents"

    STORAGE_DIR: str = "storage"
    MAX_UPLOAD_SIZE_MB: int = 25
    EMBEDDING_DIMENSION: int = 384
    DOCUMENT_CHUNK_SIZE: int = 1200
    DOCUMENT_CHUNK_OVERLAP: int = 200
    DOCUMENT_CHUNK_STRATEGY: str = "auto"

    SCHEDULER_ENABLED: bool = True
    SCHEDULER_TICK_SECONDS: int = 60
    WEBSITE_CRAWL_TIMEOUT_SECONDS: int = 20
    WEBSITE_MAX_HTML_BYTES: int = 2_000_000

    OPENAI_API_KEY: str = ""
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    # Realtime (voice) uses the official OpenAI Realtime HTTP API. Third-party
    # OpenAI-compatible gateways often do not implement /v1/realtime/client_secrets;
    # keep a dedicated base for that path even when OPENAI_BASE_URL points elsewhere.
    OPENAI_REALTIME_API_BASE: str = "https://api.openai.com/v1"
    OPENAI_REALTIME_MODEL: str = "gpt-realtime"
    OPENAI_REALTIME_VOICE: str = "alloy"
    OPENAI_REALTIME_REQUEST_TIMEOUT_SECONDS: float = 60.0
    # Total *additional* attempts after the first request on transient 502/503/504
    # or network errors. 0 disables the retry loop entirely.
    OPENAI_REALTIME_MINT_MAX_RETRIES: int = 2
    OPENAI_REALTIME_MINT_BACKOFF_BASE_SECONDS: float = 0.25
    # Per-user mint quota driven by the audit collection. Set the limit to 0 to
    # disable. The window is sliding (counts events newer than now-window).
    OPENAI_REALTIME_MINT_LIMIT_PER_USER: int = 20
    OPENAI_REALTIME_MINT_LIMIT_WINDOW_SECONDS: int = 60
    # Output budget for one assistant response. Without this, OpenAI silently caps Realtime
    # responses (~1–4k tokens depending on the model), which is the typical cause of
    # "audio stops mid-sentence" on long answers that include tool-call excerpts.
    # Accepts an integer or the string "inf" (sent through verbatim).
    OPENAI_REALTIME_MAX_OUTPUT_TOKENS: str = "inf"
    # Historical server-VAD defaults. The Realtime mint path now pins a
    # non-interrupting VAD contract in code so voice follow-ups cannot cancel the
    # assistant audio currently playing in the browser.
    OPENAI_REALTIME_VAD_THRESHOLD: float = 0.78
    OPENAI_REALTIME_VAD_PREFIX_PADDING_MS: int = 350
    OPENAI_REALTIME_VAD_SILENCE_MS: int = 650
    LLM_MODEL: str = "gpt-4o-mini"
    LLM_REQUEST_TIMEOUT_SECONDS: float = 120.0
    LLM_MAX_MESSAGES: int = 40

    CHAT_ROUTER_MODEL: str = "gpt-4o-mini"
    CHAT_RESPONDER_MODEL: str = "gpt-4o-mini"
    CHAT_HISTORY_FETCH_LIMIT: int = 20
    CHAT_ROUTER_MAX_MESSAGES: int = 20
    CHAT_RAG_TOP_K: int = 5
    CHAT_RAG_MIN_SCORE: float = 0.25
    # Voice-specific RAG budget. Realtime model contexts are smaller and audio
    # synthesis cuts off on long token streams, so we ground voice answers with
    # fewer passages and tighter excerpts than the text path uses.
    CHAT_RAG_VOICE_TOP_K: int = 3
    CHAT_RAG_VOICE_EXCERPT_CHARS: int = 1500
    # Lower values reduce speculative phrasing; None uses provider default.
    CHAT_RESPONDER_TEMPERATURE: float | None = 0.2

    CHAT_SESSION_TITLE_MODEL: str = "gpt-4o-mini"
    CHAT_SESSION_TITLE_MAX_TOKENS: int = 64
    CHAT_SESSION_TITLE_TEMPERATURE: float = 0.2
    CHAT_SESSION_TITLE_PROMPT_MAX_CHARS: int = 4000

    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    OPENAI_EMBEDDING_BATCH_SIZE: int = 64
    OPENAI_EMBEDDING_TIMEOUT_SECONDS: float = 120.0

    ZIP_SESSION_TTL_HOURS: int = 24
    ZIP_INGEST_PATH_BATCH_DEFAULT: int = 50
    ZIP_INGEST_MAX_PATH_BATCH: int = 500
    ZIP_INGEST_MAX_PATH_INDICES: int = 500
    ZIP_INGEST_MAX_MARKDOWN_LISTED: int = 100_000
    ZIP_INGEST_MAX_UNCOMPRESSED_BYTES: int = 50 * 1024 * 1024
    ZIP_INGEST_MAX_ENTRY_BYTES: int = 8 * 1024 * 1024

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE_ARG,
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


_settings_cache: Settings | None = None
_dotenv_mtime: float | None = None


def get_settings() -> Settings:
    """Load Settings from project `.env`; refresh when that file's mtime changes."""
    global _settings_cache, _dotenv_mtime
    try:
        mtime = _DOTENV_PATH.stat().st_mtime
    except OSError:
        mtime = -1.0

    if _settings_cache is None or mtime != _dotenv_mtime:
        _settings_cache = Settings()
        _dotenv_mtime = mtime
    return _settings_cache


class _SettingsProxy:
    """Delegate attribute reads so `settings.X` tracks `.env` edits (see get_settings)."""

    def __getattr__(self, name: str):
        return getattr(get_settings(), name)

    def __repr__(self) -> str:
        return f"<settings proxy -> {get_settings()!r}>"


settings = _SettingsProxy()
