import json
from typing import Any

import httpx

from app.core.config import settings
from app.core.exceptions import BadRequestException, ServiceUnavailableException


class OpenAIEmbeddingProvider:
    """OpenAI-compatible text embeddings (POST /embeddings)."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        dimension: int | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self._api_key = (api_key if api_key is not None else settings.OPENAI_API_KEY).strip()
        self._base = (base_url or settings.OPENAI_BASE_URL).rstrip("/")
        self._model = model or settings.OPENAI_EMBEDDING_MODEL
        self._dimension = dimension or settings.EMBEDDING_DIMENSION
        self._timeout = httpx.Timeout(timeout_seconds or settings.OPENAI_EMBEDDING_TIMEOUT_SECONDS)

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self._api_key:
            raise ServiceUnavailableException(
                "OpenAI embeddings require OPENAI_API_KEY to be configured."
            )

        url = f"{self._base}/embeddings"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        batch_size = settings.OPENAI_EMBEDDING_BATCH_SIZE
        all_vectors: list[list[float]] = []

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                for start in range(0, len(texts), batch_size):
                    batch = texts[start : start + batch_size]
                    payload: dict[str, Any] = {
                        "model": self._model,
                        "input": batch,
                    }
                    if self._model.startswith("text-embedding-3"):
                        payload["dimensions"] = self._dimension

                    response = await client.post(url, json=payload, headers=headers)
                    body = response.json()

                    if not response.is_success:
                        msg = _format_error(body, response.status_code)
                        if response.status_code in (502, 503, 504):
                            raise ServiceUnavailableException(msg)
                        raise BadRequestException(msg)

                    data = body.get("data")
                    if not isinstance(data, list) or len(data) != len(batch):
                        raise ServiceUnavailableException(
                            "Unexpected OpenAI embeddings response shape"
                        )

                    for item in sorted(data, key=lambda x: x.get("index", 0)):
                        emb = item.get("embedding")
                        if not isinstance(emb, list) or not emb:
                            raise ServiceUnavailableException(
                                "Missing embedding in OpenAI response"
                            )
                        if len(emb) != self._dimension:
                            msg = (
                                f"Embedding length {len(emb)} does not match "
                                f"EMBEDDING_DIMENSION={self._dimension}"
                            )
                            raise ServiceUnavailableException(msg)
                        all_vectors.append(emb)
        except httpx.RequestError as exc:
            raise ServiceUnavailableException("Could not reach OpenAI embeddings API") from exc

        return all_vectors


def _format_error(body: dict[str, Any], status_code: int) -> str:
    err = body.get("error")
    if isinstance(err, dict) and isinstance(err.get("message"), str):
        return f"OpenAI embeddings error ({status_code}): {err['message']}"
    try:
        return f"OpenAI embeddings error ({status_code}): {json.dumps(body)[:500]}"
    except Exception:
        return f"OpenAI embeddings error ({status_code})"
