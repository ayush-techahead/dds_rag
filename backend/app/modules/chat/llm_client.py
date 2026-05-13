import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.core.config import settings
from app.core.exceptions import BadRequestException, ServiceUnavailableException


class ChatCompletionClient:
    """OpenAI-compatible chat completions (POST /chat/completions)."""

    def __init__(self) -> None:
        self._api_key = settings.OPENAI_API_KEY
        self._base = settings.OPENAI_BASE_URL.rstrip("/")
        self._model = settings.LLM_MODEL
        self._timeout = httpx.Timeout(settings.LLM_REQUEST_TIMEOUT_SECONDS)

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        if not messages:
            raise BadRequestException("No messages to send to the language model")
        if not self._api_key.strip():
            raise ServiceUnavailableException(
                "LLM is not configured: set OPENAI_API_KEY "
                "(OpenAI-compatible providers supported)."
            )

        use_model = model if model is not None else self._model
        url = f"{self._base}/chat/completions"
        payload: dict[str, Any] = {
            "model": use_model,
            "messages": messages,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, json=payload, headers=headers)
        except httpx.RequestError as exc:
            raise ServiceUnavailableException("Could not reach the language model API") from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise ServiceUnavailableException("Invalid response from language model API") from exc

        if response.is_success:
            text = _extract_assistant_text(body)
            if not text:
                raise ServiceUnavailableException("Language model returned an empty response")
            return text

        message = _format_api_error(body, response.status_code)
        if response.status_code in (502, 503, 504):
            raise ServiceUnavailableException(message)
        raise BadRequestException(message)

    async def stream_complete(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        if not messages:
            raise BadRequestException("No messages to send to the language model")
        if not self._api_key.strip():
            raise ServiceUnavailableException(
                "LLM is not configured: set OPENAI_API_KEY "
                "(OpenAI-compatible providers supported)."
            )

        use_model = model if model is not None else self._model
        url = f"{self._base}/chat/completions"
        payload: dict[str, Any] = {
            "model": use_model,
            "messages": messages,
            "stream": True,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream("POST", url, json=payload, headers=headers) as response:
                    if response.status_code >= 400:
                        body = await response.aread()
                        try:
                            parsed = json.loads(body.decode("utf-8"))
                        except ValueError:
                            parsed = {}
                        message = _format_api_error(
                            parsed if isinstance(parsed, dict) else {},
                            response.status_code,
                        )
                        if response.status_code in (502, 503, 504):
                            raise ServiceUnavailableException(message)
                        raise BadRequestException(message)

                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        if line.startswith(":"):
                            continue
                        if not line.startswith("data: "):
                            continue
                        raw = line.removeprefix("data: ").strip()
                        if raw == "[DONE]":
                            break
                        try:
                            chunk = json.loads(raw)
                        except json.JSONDecodeError:
                            continue
                        piece = _extract_delta_content(chunk)
                        if piece:
                            yield piece
        except httpx.RequestError as exc:
            raise ServiceUnavailableException("Could not reach the language model API") from exc


def _extract_delta_content(chunk: dict[str, Any]) -> str:
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    delta = first.get("delta")
    if not isinstance(delta, dict):
        return ""
    content = delta.get("content")
    return content if isinstance(content, str) else ""


def _extract_assistant_text(body: dict[str, Any]) -> str:
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    msg = first.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
        return content.strip() if isinstance(content, str) else ""
    return ""


def _format_api_error(body: dict[str, Any], status_code: int) -> str:
    err = body.get("error")
    if isinstance(err, dict) and isinstance(err.get("message"), str):
        return f"Language model API error ({status_code}): {err['message']}"
    return f"Language model API error ({status_code})"
